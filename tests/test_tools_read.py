"""Tests for read-only tool logic functions."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from tests.conftest import URL
from unraid_mcp import queries
from unraid_mcp.errors import UnraidAuthError, UnraidConnectionError, UnraidGraphQLError
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, docker, misc, notifications, shares, system, vm

from .test_formatting import assert_sizes_shaped


def _resp(data):
    return httpx.Response(200, json={"data": data})


_NO_ALERTS = _resp({"notifications": {"warningsAndAlerts": []}})


@pytest.fixture
def mocked_client(mocked_client, request):
    """For health tests, slot an empty warningsAndAlerts response in as the 4th
    request (array, ups, notifications, alerts run concurrently; any UPS
    configuration follow-up comes after) so their response lists stay readable."""
    name = request.function.__name__
    if not name.startswith("test_health") or name.startswith("test_health_summary"):
        return mocked_client

    def wrap(responses):
        if isinstance(responses, list) and len(responses) >= 3:
            responses = [*responses[:3], _NO_ALERTS, *responses[3:]]
        return mocked_client(responses)

    return wrap


def _sent_query(route):
    return json.loads(route.calls.last.request.content)["query"]


def _sent_vars(route):
    return json.loads(route.calls.last.request.content)["variables"]


async def test_system_info_with_flash(mocked_client):
    """The second (flash) call succeeds: info is enriched with flash identity."""
    info_resp = _resp({"info": {"os": {"hostname": "tower"}}})
    flash_resp = _resp({"flash": {"guid": "abc-123", "vendor": "SanDisk", "product": "Cruzer"}})
    async with mocked_client([info_resp, flash_resp]) as (client, route):
        out = await system.fetch_system_info(client)
    assert out["os"] == {"hostname": "tower"}
    assert out["flash"] == {"guid": "abc-123", "vendor": "SanDisk", "product": "Cruzer"}
    assert route.call_count == 2
    assert _sent_query(route) == queries.FLASH


async def test_system_info_degrades_when_flash_unavailable(mocked_client):
    """An older API build without `flash` still returns system info, just
    without the flash key — the second call fails and is swallowed."""
    info_resp = _resp({"info": {"os": {"hostname": "tower"}}})
    flash_err = httpx.Response(
        200,
        json={"errors": [{"message": 'Cannot query field "flash" on type "Query".'}], "data": None},
    )
    async with mocked_client([info_resp, flash_err]) as (client, route):
        out = await system.fetch_system_info(client)
    assert out == {"os": {"hostname": "tower"}}
    assert "flash" not in out


async def test_system_time(mocked_client):
    data = {
        "systemTime": {
            "currentTime": "2026-07-03T12:00:00Z",
            "timeZone": "UTC",
            "useNtp": True,
            "ntpServers": ["0.pool.ntp.org", "", ""],
        }
    }
    async with mocked_client(_resp(data)) as (client, route):
        out = await system.fetch_system_time(client)
    assert out == {
        "current_time": "2026-07-03T12:00:00Z",
        "time_zone": "UTC",
        "use_ntp": True,
        "ntp_servers": ["0.pool.ntp.org"],
    }
    assert _sent_query(route) == queries.SYSTEM_TIME


async def test_system_time_unsupported_raises_friendly_error(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "systemTime" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (client, route):
        with pytest.raises(ToolError, match="does not support"):
            await system.fetch_system_time(client, api_version="7.0.0")


_DEVICES = {
    "info": {
        "devices": {
            "gpu": [{"id": "g1", "type": "Nvidia", "blacklisted": False}],
            "pci": [{"id": "p1", "vendorname": "Intel", "blacklisted": "false"}],
            "usb": [{"id": "u1", "name": "Flash", "bus": "001", "device": "002"}],
            "network": [{"id": "n1", "iface": "eth0", "mac": "aa:bb"}],
        }
    }
}


async def test_hardware_inventory_all(mocked_client):
    async with mocked_client(_resp(_DEVICES)) as (client, route):
        out = await system.fetch_hardware_inventory(client)
    assert set(out) == {"gpu", "pci", "usb", "network"}
    assert out["gpu"][0]["id"] == "g1"
    assert out["pci"][0]["blacklisted"] is False
    assert _sent_query(route) == queries.HARDWARE_INVENTORY
    assert "machineId" not in _sent_query(route)


async def test_hardware_inventory_pci_blacklisted_string_to_bool(mocked_client):
    data = {"info": {"devices": {"pci": [{"id": "a", "blacklisted": "true"}, {"id": "b"}]}}}
    async with mocked_client(_resp(data)) as (client, route):
        out = await system.fetch_hardware_inventory(client, "pci")
    assert out["pci"][0]["blacklisted"] is True
    assert "blacklisted" not in out["pci"][1]


async def test_hardware_inventory_kind_filter(mocked_client):
    async with mocked_client(_resp(_DEVICES)) as (client, route):
        out = await system.fetch_hardware_inventory(client, "usb")
    assert out == {"usb": _DEVICES["info"]["devices"]["usb"]}


async def test_hardware_inventory_invalid_kind_makes_no_request(mocked_client):
    async with mocked_client(_resp(_DEVICES)) as (client, route):
        with pytest.raises(ToolError, match="Unknown kind"):
            await system.fetch_hardware_inventory(client, "sata")
    assert route.call_count == 0


async def test_hardware_inventory_null_lists(mocked_client):
    data = {"info": {"devices": {"gpu": None, "pci": [], "usb": None, "network": None}}}
    async with mocked_client(_resp(data)) as (client, route):
        out = await system.fetch_hardware_inventory(client)
    assert out == {"gpu": [], "pci": [], "usb": [], "network": []}


async def test_hardware_inventory_empty_response(mocked_client):
    async with mocked_client(_resp({"info": None})) as (client, route):
        out = await system.fetch_hardware_inventory(client, "gpu")
    assert out == {"gpu": []}


async def test_hardware_inventory_unsupported_raises_friendly_error(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "devices" on type "Info".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (client, route):
        with pytest.raises(ToolError, match="does not support"):
            await system.fetch_hardware_inventory(client, api_version="7.0.0")


async def test_hardware_inventory_other_error_propagates(mocked_client):
    resp = httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})
    async with mocked_client(resp) as (client, route):
        with pytest.raises(UnraidGraphQLError):
            await system.fetch_hardware_inventory(client)


async def test_system_metrics(mocked_client):
    data = {
        "metrics": {
            "cpu": {
                "percentTotal": 12.345,
                "cpus": [{"percentTotal": 5.05}, {"percentTotal": 19.999}],
            },
            "memory": {
                "total": 17179869184,
                "used": 8589934592,
                "free": 8589934592,
                "available": 8589934592,
                "percentTotal": 50.0,
                "swapTotal": 4294967296,
                "swapUsed": 0,
                "swapFree": 4294967296,
                "percentSwapTotal": 0.0,
            },
            "temperature": {
                "summary": {"average": 42.5, "warningCount": 0, "criticalCount": 0},
                "sensors": [{"name": "CPU", "current": {"value": 45.0, "unit": "C"}}],
            },
        }
    }
    async with mocked_client([_resp(data), _resp({"metrics": {"network": []}})]) as (client, route):
        out = await system.fetch_metrics(client)
    assert out["cpu"] == {"percent_total": 12.3, "per_core": [5.0, 20.0]}
    assert out["memory"]["total"] == {"bytes": 17179869184, "human": "16.0 GiB"}
    assert out["memory"]["percent_total"] == 50.0
    assert out["temperature"]["summary"]["warning_count"] == 0
    assert json.loads(route.calls[0].request.content)["query"] == queries.SYSTEM_METRICS
    assert out["network"] == []


async def test_system_metrics_partial_response_still_returns_cpu_memory(mocked_client):
    data = {
        "metrics": {
            "cpu": {"percentTotal": 1.0, "cpus": []},
            "memory": {"total": 1024, "used": 512, "free": 512, "available": 512},
            "temperature": None,
        }
    }
    async with mocked_client(_resp(data)) as (client, route):
        out = await system.fetch_metrics(client)
    assert "cpu" in out
    assert "memory" in out
    assert "temperature" not in out


async def test_system_metrics_unsupported_api_raises_friendly_error(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "metrics" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (client, route):
        with pytest.raises(ToolError, match="does not support"):
            await system.fetch_metrics(client, api_version="7.1.0")


_NET_ROW = {
    "name": "eth0",
    "operstate": "up",
    "rxSec": 12902400.0,
    "txSec": 512.0,
    "utilizationPercent": 10.3,
    "bytesReceived": "1073741824",
    "bytesSent": 2048,
    "receiveErrors": "1",
    "transmitErrors": "0",
    "receiveDropped": 2,
    "transmitDropped": "bogus",
    "lastUpdated": "2026-01-01T00:00:00Z",
}
_METRICS_OK = {"metrics": {"cpu": {"percentTotal": 1.0, "cpus": []}}}


async def test_system_metrics_includes_network(mocked_client):
    async with mocked_client([_resp(_METRICS_OK), _resp({"metrics": {"network": [_NET_ROW]}})]) as (
        client,
        route,
    ):
        out = await system.fetch_metrics(client)
    assert _sent_query(route) == queries.SYSTEM_METRICS_NETWORK
    (nic,) = out["network"]
    assert nic["name"] == "eth0"
    assert nic["rx"] == {"bytes_per_sec": 12902400.0, "human": "12.3 MiB/s"}
    assert nic["tx"] == {"bytes_per_sec": 512.0, "human": "512 B/s"}
    assert nic["bytes_received"] == {"bytes": 1073741824, "human": "1.0 GiB"}
    assert nic["bytes_sent"] == {"bytes": 2048, "human": "2.0 KiB"}
    assert nic["receive_errors"] == 1
    assert nic["receive_dropped"] == 2
    assert nic["transmit_errors"] == 0
    assert nic["transmit_dropped"] is None
    assert nic["utilization_percent"] == 10.3
    assert "cpu" in out


async def test_system_metrics_network_empty_list(mocked_client):
    async with mocked_client([_resp(_METRICS_OK), _resp({"metrics": {"network": []}})]) as (
        client,
        _route,
    ):
        out = await system.fetch_metrics(client)
    assert out["network"] == []


async def test_system_metrics_network_null_fields(mocked_client):
    row = {"name": "eth0", "rxSec": None, "txSec": None, "bytesReceived": None}
    async with mocked_client([_resp(_METRICS_OK), _resp({"metrics": {"network": [row]}})]) as (
        client,
        _route,
    ):
        out = await system.fetch_metrics(client)
    (nic,) = out["network"]
    assert nic["rx"] == {"bytes_per_sec": None, "human": None}
    assert nic["bytes_received"] == {"bytes": None, "human": None}
    assert nic["receive_errors"] is None
    assert nic["operstate"] is None


async def test_system_metrics_network_unsupported_omits_section(mocked_client):
    err = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "network" on type "Metrics".'}],
            "data": None,
        },
    )
    async with mocked_client([_resp(_METRICS_OK), err]) as (client, _route):
        out = await system.fetch_metrics(client)
    assert "network" not in out
    assert "cpu" in out


_UNKNOWN_FIELD = httpx.Response(
    200,
    json={
        "errors": [{"message": 'Cannot query field "hottest" on type "TemperatureSummary".'}],
        "data": None,
    },
)


async def test_system_metrics_extended_fields(mocked_client):
    data = {
        "metrics": {
            "temperature": {
                "summary": {
                    "average": 60.0,
                    "warningCount": 0,
                    "criticalCount": 1,
                    "hottest": {"name": "CPU", "current": {"value": 91.0, "unit": "CELSIUS"}},
                },
                "sensors": [
                    {
                        "name": "CPU",
                        "type": "CPU_PACKAGE",
                        "location": "cpu",
                        "current": {"value": 91.0, "unit": "CELSIUS"},
                        "warning": 80.0,
                        "critical": 90.0,
                    }
                ],
            }
        }
    }
    async with mocked_client(_resp(data)) as (client, route):
        out = await system.fetch_metrics(client)
    assert route.call_count == 1
    assert out["temperature"]["summary"]["hottest"]["name"] == "CPU"
    assert out["temperature"]["sensors"][0]["level"] == "critical"


async def test_system_metrics_older_api_falls_back_to_legacy(mocked_client):
    legacy = _resp(
        {
            "metrics": {
                "temperature": {
                    "summary": {"average": 40.0, "warningCount": 0, "criticalCount": 0},
                    "sensors": [{"name": "CPU", "current": {"value": 40.0, "unit": "C"}}],
                }
            }
        }
    )
    async with mocked_client([_UNKNOWN_FIELD, legacy]) as (client, route):
        out = await system.fetch_metrics(client)
    assert route.call_count == 2
    assert _sent_query(route) == queries.SYSTEM_METRICS_LEGACY
    assert out["temperature"]["sensors"][0]["level"] is None
    assert out["temperature"]["summary"]["hottest"] is None


async def test_services_happy_and_empty(mocked_client):
    data = {
        "services": [
            {
                "id": "svc:1",
                "name": "api",
                "online": True,
                "uptime": {"timestamp": "2026-07-01T00:00:00.000Z"},
                "version": "4.0.0",
            }
        ]
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await system.fetch_services(c)
    assert out == [
        {
            "name": "api",
            "online": True,
            "uptime": "2026-07-01T00:00:00.000Z",
            "version": "4.0.0",
        }
    ]
    assert _sent_query(r) == queries.SERVICES

    async with mocked_client(_resp({"services": []})) as (c, r):
        assert await system.fetch_services(c) == []


async def test_services_unsupported_api_degrades(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "services" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await system.fetch_services(c, api_version="7.1.0")


async def test_array_status(mocked_client):
    data = {
        "array": {
            "state": "STARTED",
            "capacity": {"kilobytes": {"total": "1", "used": "0", "free": "1"}},
            "disks": [],
        }
    }
    async with mocked_client(_resp(data)) as (client, route):
        out = await array.fetch_array_status(client)
    assert out["state"] == "STARTED"
    assert _sent_query(route) == queries.ARRAY_STATUS


async def test_array_status_extended_fields(mocked_client):
    data = {
        "array": {
            "state": "STARTED",
            "bootDevices": [{"name": "boot1", "type": "BOOT"}, {"name": "boot2", "type": "BOOT"}],
            "disks": [{"name": "disk1", "isSpinning": False, "transport": "ata"}],
        }
    }
    async with mocked_client(_resp(data)) as (client, route):
        out = await array.fetch_array_status(client)
    assert [b["name"] for b in out["boot_devices"]] == ["boot1", "boot2"]
    assert out["data_disks"][0]["spinning"] is False
    assert "bootDevices" in _sent_query(route)


async def test_array_status_older_api_falls_back_to_legacy(mocked_client):
    err = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "bootDevices" on type "UnraidArray".'}],
            "data": None,
        },
    )
    legacy = _resp({"array": {"state": "STARTED", "boot": {"name": "flash"}, "disks": []}})
    async with mocked_client([err, legacy]) as (client, route):
        out = await array.fetch_array_status(client)
    assert route.call_count == 2
    assert _sent_query(route) == queries.ARRAY_STATUS_LEGACY
    assert out["boot"]["name"] == "flash"
    assert out["boot_devices"] is None


async def test_array_status_other_graphql_error_not_retried(mocked_client):
    err = httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})
    async with mocked_client(err) as (client, route):
        with pytest.raises(UnraidGraphQLError):
            await array.fetch_array_status(client)
    assert route.call_count == 1


async def test_parity_status_and_history(mocked_client):
    async with mocked_client(_resp({"array": {"parityCheckStatus": {"status": "COMPLETED"}}})) as (
        c,
        r,
    ):
        assert (await array.fetch_parity_status(c))["status"] == "COMPLETED"
    async with mocked_client(_resp({"parityHistory": [{"status": "OK"}]})) as (c, r):
        assert await array.fetch_parity_history(c) == [{"status": "OK"}]


@pytest.mark.parametrize(
    "data",
    [{}, {"array": None}, {"array": {}}, {"array": {"parityCheckStatus": None}}],
    ids=["empty-data", "null-array", "empty-array", "null-status"],
)
async def test_parity_status_empty_or_null_fields(mocked_client, data):
    async with mocked_client(_resp(data)) as (c, r):
        assert await array.fetch_parity_status(c) == {}


@pytest.mark.parametrize(
    "data", [{}, {"parityHistory": None}, {"parityHistory": []}], ids=["empty", "null", "list"]
)
async def test_parity_history_empty_or_null(mocked_client, data):
    async with mocked_client(_resp(data)) as (c, r):
        assert await array.fetch_parity_history(c) == []


_GQL_ERROR = httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})


@pytest.mark.parametrize("fetch", [array.fetch_parity_status, array.fetch_parity_history])
async def test_parity_graphql_error_raises(mocked_client, fetch):
    async with mocked_client(_GQL_ERROR) as (c, r):
        with pytest.raises(UnraidGraphQLError):
            await fetch(c)


@pytest.mark.parametrize("tool", ["get_parity_status", "get_parity_history"])
async def test_parity_tool_maps_graphql_error_to_tool_error(settings_factory, tool):
    with respx.mock(assert_all_called=False) as router:
        router.post(URL).mock(return_value=_GQL_ERROR)
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=False) as session:
            result = await session.call_tool(tool, {})
    assert result.is_error is True
    assert "boom" in result.content[0].text


async def test_disks_and_disk_details(mocked_client):
    async with mocked_client(_resp({"disks": [{"id": "1:a", "size": 1024**4}]})) as (c, r):
        disks = await array.fetch_disks(c)
    assert disks[0]["size"]["bytes"] == 1024**4
    async with mocked_client(_resp({"disk": {"id": "1:a", "smartStatus": "OK", "size": 1024}})) as (
        c,
        r,
    ):
        out = await array.fetch_disk(c, "1:a")
        assert out["smart_status"] == "OK"
        assert _sent_vars(r) == {"id": "1:a"}


async def test_disk_details_partition_sizes_shaped(mocked_client):
    raw = {
        "id": "1:a",
        "size": 1024**4,
        "partitions": [
            {"name": "sda1", "fsType": "XFS", "size": 1024**3},
            {"name": "sda2", "fsType": "VFAT", "size": None},
        ],
    }
    async with mocked_client(_resp({"disk": raw})) as (c, r):
        out = await array.fetch_disk(c, "1:a")
    assert out["partitions"][0]["size"] == {"bytes": 1024**3, "human": "1.0 GiB"}
    assert out["partitions"][1]["size"] == {"bytes": None, "human": None}
    assert_sizes_shaped(out)


async def test_disk_details_null_raises_friendly_error(mocked_client):
    async with mocked_client(_resp({"disk": None})) as (c, r):
        with pytest.raises(ToolError, match="No disk matching"):
            await array.fetch_disk(c, "1:nope")


async def test_disk_details_graphql_not_found_raises_friendly_error(mocked_client):
    resp = httpx.Response(
        200,
        json={"errors": [{"message": "Disk not found for id 1:nope"}], "data": None},
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="No disk matching") as exc:
            await array.fetch_disk(c, "1:nope")
    assert "Disk not found" not in str(exc.value)


async def test_disk_details_unrelated_graphql_error_passes_through(mocked_client):
    resp = httpx.Response(
        200,
        json={"errors": [{"message": "Authentication required"}], "data": None},
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(UnraidGraphQLError, match="Authentication required"):
            await array.fetch_disk(c, "1:whatever")


async def test_docker_list_and_resolve(mocked_client):
    data = {
        "docker": {
            "containers": [
                {"id": "1:abcdef", "names": ["/plex"], "state": "RUNNING"},
                {"id": "1:123456", "names": ["/sonarr"], "state": "EXITED"},
            ]
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await docker.fetch_containers(c)
        assert out[0]["name"] == "plex"
    async with mocked_client(_resp(data)) as (c, r):
        assert (await docker.fetch_container(c, "sonarr"))["id"] == "1:123456"
    async with mocked_client(_resp(data)) as (c, r):
        assert (await docker.fetch_container(c, "1:abcdef"))["name"] == "plex"
    async with mocked_client(_resp(data)) as (c, r):
        with pytest.raises(ToolError):
            await docker.fetch_container(c, "nope")


async def test_docker_networks(mocked_client):
    async with mocked_client(_resp({"docker": {"networks": [{"name": "bridge"}]}})) as (c, r):
        assert await docker.fetch_docker_networks(c) == [{"name": "bridge"}]


async def test_container_logs_happy_path(mocked_client):
    data = {
        "docker": {
            "logs": {
                "containerId": "1:abcdef",
                "lines": [
                    {"timestamp": "2024-01-01T00:00:00Z", "message": "starting up"},
                    {"timestamp": "2024-01-01T00:00:01Z", "message": "ready"},
                ],
                "cursor": "2024-01-01T00:00:01Z",
            }
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await docker.fetch_container_logs(c, "1:abcdef", tail=10)
    assert out["container_id"] == "1:abcdef"
    assert out["lines"] == [
        {"timestamp": "2024-01-01T00:00:00Z", "message": "starting up", "truncated": False},
        {"timestamp": "2024-01-01T00:00:01Z", "message": "ready", "truncated": False},
    ]
    assert out["cursor"] == "2024-01-01T00:00:01Z"
    assert out["truncated"] is False
    assert _sent_vars(r) == {"id": "1:abcdef", "since": None, "tail": 10}


async def test_container_logs_long_line_truncated(mocked_client):
    long_message = "x" * 2500
    data = {
        "docker": {
            "logs": {
                "containerId": "1:abcdef",
                "lines": [{"timestamp": "2024-01-01T00:00:00Z", "message": long_message}],
                "cursor": None,
            }
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await docker.fetch_container_logs(c, "1:abcdef")
    line = out["lines"][0]
    assert line["truncated"] is True
    assert len(line["message"]) < len(long_message)
    assert line["message"].endswith("[truncated]")
    assert out["truncated"] is True


async def test_container_logs_tail_clamp_no_http_call(mocked_client):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="exceeds the maximum"):
            await docker.fetch_container_logs(c, "1:abcdef", tail=5000)
    assert r.call_count == 0


async def test_container_logs_non_positive_tail_no_http_call(mocked_client):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError):
            await docker.fetch_container_logs(c, "1:abcdef", tail=0)
    assert r.call_count == 0


async def test_container_logs_bad_since_no_http_call(mocked_client):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="Invalid 'since'"):
            await docker.fetch_container_logs(c, "1:abcdef", since="not-a-date")
    assert r.call_count == 0


async def test_container_logs_unknown_id(mocked_client):
    err = httpx.Response(
        200, json={"errors": [{"message": "No container with id 1:nope"}], "data": None}
    )
    async with mocked_client(err) as (c, r):
        # fetch_* propagates non-"unsupported field" GraphQL errors untouched;
        # `_base.guarded` (the @mcp.tool boundary) is what turns it into a
        # friendly ToolError for the client, and does not misclassify it as
        # an unsupported-API error.
        with pytest.raises(UnraidGraphQLError) as ei:
            await docker.fetch_container_logs(c, "1:nope")
        assert "does not support" not in str(ei.value)


async def test_container_logs_unsupported_api(mocked_client):
    err = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "logs" on type "Docker".'}],
            "data": None,
        },
    )
    async with mocked_client(err) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await docker.fetch_container_logs(c, "1:abcdef", api_version="7.1.0")


async def test_docker_updates_happy_and_empty(mocked_client):
    data = {
        "docker": {
            "containerUpdateStatuses": [
                {"name": "plex", "updateStatus": "UP_TO_DATE"},
                {"name": "sonarr", "updateStatus": "UPDATE_AVAILABLE"},
            ]
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await docker.fetch_docker_updates(c)
    assert out == [
        {"name": "plex", "update_status": "UP_TO_DATE"},
        {"name": "sonarr", "update_status": "UPDATE_AVAILABLE"},
    ]
    assert _sent_query(r) == queries.DOCKER_UPDATE_STATUSES

    async with mocked_client(_resp({"docker": {"containerUpdateStatuses": []}})) as (c, r):
        assert await docker.fetch_docker_updates(c) == []


async def test_docker_updates_unsupported_api_degrades(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [
                {"message": 'Cannot query field "containerUpdateStatuses" on type "Docker".'}
            ],
            "data": None,
        },
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await docker.fetch_docker_updates(c, api_version="7.1.0")


async def test_container_native_path_hit(mocked_client):
    """A colon-bearing identifier (PrefixedID shape) uses the native single
    -container query, not the list+filter fallback."""
    data = {
        "docker": {
            "container": {
                "id": "1:abcdef",
                "names": ["/plex"],
                "image": "plexinc/pms",
                "state": "RUNNING",
                "status": "Up 2 hours",
                "autoStart": True,
                "ports": [],
            }
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await docker.fetch_container(c, "1:abcdef")
    assert out["id"] == "1:abcdef"
    assert out["name"] == "plex"
    assert r.call_count == 1
    assert _sent_query(r) == queries.DOCKER_CONTAINER
    assert _sent_vars(r) == {"id": "1:abcdef"}


async def test_container_falls_back_on_old_api(mocked_client):
    """Old API build lacks `docker.container`; the id lookup falls back to the
    list+filter path, using exactly two HTTP calls in the expected order."""
    missing_field_error = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "container" on type "Docker".'}],
            "data": None,
        },
    )
    list_data = {
        "docker": {
            "containers": [
                {"id": "1:abcdef", "names": ["/plex"], "state": "RUNNING"},
            ]
        }
    }
    async with mocked_client([missing_field_error, _resp(list_data)]) as (c, r):
        out = await docker.fetch_container(c, "1:abcdef")
    assert out["id"] == "1:abcdef"
    assert r.call_count == 2
    calls = r.calls
    assert json.loads(calls[0].request.content)["query"] == queries.DOCKER_CONTAINER
    assert json.loads(calls[1].request.content)["query"] == queries.LIST_CONTAINERS


async def test_container_null_native_result_falls_back(mocked_client):
    """A stale/unknown id resolves native to null `container`; falls back to
    the list+filter path (still 404s if not found there either)."""
    native_null = _resp({"docker": {"container": None}})
    list_data = {"docker": {"containers": []}}
    async with mocked_client([native_null, _resp(list_data)]) as (c, r):
        with pytest.raises(ToolError, match="No Docker container matching"):
            await docker.fetch_container(c, "1:ghost")
    assert r.call_count == 2


async def test_container_name_lookup_stays_list_based(mocked_client):
    """A plain name (no colon) never triggers the native id query."""
    data = {
        "docker": {
            "containers": [
                {"id": "1:abcdef", "names": ["/plex"], "state": "RUNNING"},
            ]
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await docker.fetch_container(c, "plex")
    assert out["id"] == "1:abcdef"
    assert r.call_count == 1
    assert _sent_query(r) == queries.LIST_CONTAINERS


async def test_vms_with_domains_and_fallback(mocked_client):
    async with mocked_client(
        _resp({"vms": {"domains": [{"id": "u1", "name": "win", "state": "RUNNING"}]}})
    ) as (c, r):
        assert (await vm.fetch_vms(c))[0]["name"] == "win"
    async with mocked_client(
        _resp({"vms": {"domain": [{"id": "u2", "name": "lin", "state": "SHUTOFF"}]}})
    ) as (c, r):
        assert (await vm.fetch_vms(c))[0]["state"] == "SHUTOFF"


async def test_vms_modern_schema_single_request(mocked_client):
    """LIST_VMS succeeds on the first try: no retry, exactly one HTTP call."""
    async with mocked_client(
        _resp({"vms": {"domains": [{"id": "u1", "name": "win", "state": "RUNNING"}]}})
    ) as (c, r):
        out = await vm.fetch_vms(c)
    assert out == [{"id": "u1", "name": "win", "state": "RUNNING"}]
    assert r.call_count == 1
    assert _sent_query(r) == queries.LIST_VMS


async def test_vms_legacy_schema_retries_once(mocked_client):
    """LIST_VMS fails because `domains` doesn't exist on this build; the
    retry with LIST_VMS_LEGACY succeeds, using exactly two HTTP calls."""
    missing_field_error = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "domains" on type "Vms".'}],
            "data": None,
        },
    )
    legacy_success = _resp({"vms": {"domain": [{"id": "u2", "name": "lin", "state": "SHUTOFF"}]}})
    async with mocked_client([missing_field_error, legacy_success]) as (c, r):
        out = await vm.fetch_vms(c)
    assert out == [{"id": "u2", "name": "lin", "state": "SHUTOFF"}]
    assert r.call_count == 2
    assert _sent_query(r) == queries.LIST_VMS_LEGACY


async def test_vms_unrelated_graphql_error_not_retried(mocked_client):
    """A GraphQL error unrelated to the `domains` field must not trigger a
    retry; it propagates as a ToolError after a single HTTP call."""
    unrelated_error = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "foo" on type "Vms".'}],
            "data": None,
        },
    )
    async with mocked_client(unrelated_error) as (c, r):
        with pytest.raises(UnraidGraphQLError):
            await vm.fetch_vms(c)
    assert r.call_count == 1


async def test_shares(mocked_client):
    async with mocked_client(_resp({"shares": [{"name": "appdata", "size": "1048576"}]})) as (c, r):
        out = await shares.fetch_shares(c)
    assert out[0]["name"] == "appdata"
    assert out[0]["size"]["human"] == "1.0 GiB"


async def test_shares_enriched_fields(mocked_client):
    data = {
        "shares": [
            {
                "name": "secure",
                "include": ["disk1", "disk2"],
                "exclude": ["disk3"],
                "splitLevel": "2",
                "floor": "10G",
                "luksStatus": "ENCRYPTED",
            }
        ]
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await shares.fetch_shares(c)
    share = out[0]
    assert share["include"] == ["disk1", "disk2"]
    assert share["exclude"] == ["disk3"]
    assert share["split_level"] == "2"
    assert share["floor"] == "10G"
    assert share["encryption_status"] == "ENCRYPTED"


async def test_shares_omits_empty_extra_fields(mocked_client):
    data = {
        "shares": [
            {
                "name": "plain",
                "include": [],
                "exclude": [],
                "splitLevel": None,
                "floor": "",
                "luksStatus": None,
            }
        ]
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await shares.fetch_shares(c)
    share = out[0]
    for key in ("include", "exclude", "split_level", "floor", "encryption_status"):
        assert key not in share


async def test_notifications_overview_and_list(mocked_client):
    async with mocked_client(_resp({"notifications": {"overview": {"unread": {"total": 2}}}})) as (
        c,
        r,
    ):
        assert (await notifications.fetch_overview(c))["unread"]["total"] == 2
    async with mocked_client(_resp({"notifications": {"list": [{"id": "n1"}]}})) as (c, r):
        out = await notifications.fetch_notifications(c, "ARCHIVE", "WARNING", 10, 5)
        assert out == [{"id": "n1"}]
        assert _sent_vars(r)["filter"] == {
            "type": "ARCHIVE",
            "offset": 5,
            "limit": 10,
            "importance": "WARNING",
        }


async def test_ups_power_watts_extended(mocked_client):
    data = {
        "upsDevices": [
            {
                "name": "ups0",
                "power": {"loadPercentage": 35, "nominalPower": 1000, "currentPower": 350.0},
            }
        ]
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await misc.fetch_ups(c)
    assert out[0]["power"]["nominalPower"] == 1000
    assert out[0]["power"]["currentPower"] == 350.0
    assert "nominalPower" in _sent_query(r)


async def test_ups_empty_and_null(mocked_client):
    async with mocked_client(_resp({"upsDevices": []})) as (c, r):
        assert await misc.fetch_ups(c) == []
    async with mocked_client(_resp({"upsDevices": None})) as (c, r):
        assert await misc.fetch_ups(c) == []


async def test_ups_older_api_falls_back_to_legacy(mocked_client):
    err = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "nominalPower" on type "UPSPower".'}],
            "data": None,
        },
    )
    legacy = _resp({"upsDevices": [{"name": "ups0", "power": {"loadPercentage": 20}}]})
    async with mocked_client([err, legacy]) as (c, r):
        out = await misc.fetch_ups(c)
    assert r.call_count == 2
    assert _sent_query(r) == queries.UPS_DEVICES_LEGACY
    assert out[0]["name"] == "ups0"


async def test_health_summary_keeps_ups_on_older_api(mocked_client):
    array_resp = _resp({"array": {"state": "STARTED", "disks": []}})
    err = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "nominalPower" on type "UPSPower".'}],
            "data": None,
        },
    )
    ups_legacy = _resp({"upsDevices": [{"name": "ups0", "battery": {"chargeLevel": 90}}]})
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}})
    async with mocked_client([array_resp, err, ups_legacy, notif_resp]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["ups"][0]["battery_pct"] == 90


async def test_ups_network_me_connect(mocked_client):
    async with mocked_client(_resp({"upsDevices": [{"name": "ups0"}]})) as (c, r):
        assert (await misc.fetch_ups(c))[0]["name"] == "ups0"
    async with mocked_client(_resp({"networkInterfaces": [{"name": "eth0"}]})) as (c, r):
        assert (await misc.fetch_network_interfaces(c))[0]["name"] == "eth0"
    async with mocked_client(_resp({"me": {"name": "root", "roles": ["admin"]}})) as (c, r):
        assert (await misc.fetch_me(c))["roles"] == ["admin"]
    async with mocked_client(
        _resp({"registration": {"type": "PRO"}, "remoteAccess": {"accessType": "DISABLED"}})
    ) as (c, r):
        out = await misc.fetch_connect_status(c)
        assert out["registration"]["type"] == "PRO"
        assert out["remote_access"]["accessType"] == "DISABLED"


async def test_plugins_union_happy_path(mocked_client):
    plugins_resp = _resp(
        {
            "plugins": [
                {
                    "name": "dynamix.docker.manager",
                    "version": "2024.01.01",
                    "hasApiModule": True,
                    "hasCliModule": False,
                }
            ]
        }
    )
    installed_resp = _resp(
        {"installedUnraidPlugins": ["dynamix.docker.manager.plg", "community.applications.plg"]}
    )
    async with mocked_client([plugins_resp, installed_resp]) as (c, r):
        out = await misc.fetch_plugins(c)
    assert out == [
        {
            "name": "dynamix.docker.manager",
            "version": "2024.01.01",
            "has_api_module": True,
            "has_cli_module": False,
            "source": "plugins",
        },
        {
            "name": "community.applications.plg",
            "version": None,
            "has_api_module": None,
            "has_cli_module": None,
            "source": "installed_unraid_plugins",
        },
    ]
    assert _sent_query(r) == queries.INSTALLED_UNRAID_PLUGINS


async def test_plugins_empty_and_none_fields(mocked_client):
    async with mocked_client(_resp({"plugins": [], "installedUnraidPlugins": []})) as (c, r):
        assert await misc.fetch_plugins(c) == []
    async with mocked_client(_resp({"plugins": None, "installedUnraidPlugins": None})) as (c, r):
        assert await misc.fetch_plugins(c) == []


async def test_plugins_degrades_when_installed_unraid_plugins_unavailable(mocked_client):
    plugins_resp = _resp(
        {"plugins": [{"name": "p1", "version": "1", "hasApiModule": None, "hasCliModule": None}]}
    )
    installed_err = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "installedUnraidPlugins" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client([plugins_resp, installed_err]) as (c, r):
        out = await misc.fetch_plugins(c)
    assert out == [
        {
            "name": "p1",
            "version": "1",
            "has_api_module": None,
            "has_cli_module": None,
            "source": "plugins",
        }
    ]


async def test_plugins_unsupported_raises_friendly_error(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "plugins" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await misc.fetch_plugins(c, api_version="7.0.0")


async def test_health_summary_composes(mocked_client):
    array_resp = _resp(
        {
            "array": {
                "state": "STARTED",
                "capacity": {"kilobytes": {"total": "1", "used": "0", "free": "1"}},
                "disks": [{"name": "disk1", "status": "DISK_DSBL"}],
            }
        }
    )
    ups_resp = _resp(
        {"upsDevices": [{"name": "ups0", "status": "ONLINE", "battery": {"chargeLevel": 100}}]}
    )
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 1, "warning": 0}}}})
    async with mocked_client([array_resp, ups_resp, notif_resp, _NO_ALERTS]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "critical"  # a disabled disk takes precedence over an alert
    assert out["array_state"] == "STARTED"
    assert out["unhealthy_disks"][0]["health"] == "failed"
    assert out["ups"][0]["battery_pct"] == 100


async def test_health_summary_degrades_when_ups_unavailable(mocked_client):
    array_resp = _resp({"array": {"state": "STARTED", "disks": []}})
    ups_err = httpx.Response(200, json={"errors": [{"message": "no ups"}], "data": None})
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}})
    async with mocked_client(
        [
            array_resp,
            ups_err,
            notif_resp,
            _NO_ALERTS,
            _resp({"upsConfiguration": {"service": "enable"}}),
        ]
    ) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "degraded"
    assert out["checks"]["ups"] == "failed"
    assert out["reasons"] == ["Ups check failed or is unsupported"]
    assert out["ups"] == []


async def test_health_summary_ignores_empty_array_slots(mocked_client):
    array_resp = _resp(
        {
            "array": {
                "state": "STARTED",
                "capacity": {"kilobytes": {"total": "1", "used": "0", "free": "1"}},
                "disks": [
                    {"name": "disk1", "status": "DISK_OK"},
                    {"name": "disk2", "status": "DISK_NP"},
                ],
            }
        }
    )
    ups_resp = _resp({"upsDevices": []})
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}})
    async with mocked_client([array_resp, ups_resp, notif_resp, _NO_ALERTS]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "ok"
    assert out["unhealthy_disks"] == []
    assert out["disk_count"] == 1


async def test_health_summary_flags_missing_assigned_disk(mocked_client):
    array_resp = _resp(
        {
            "array": {
                "state": "STARTED",
                "capacity": {"kilobytes": {"total": "1", "used": "0", "free": "1"}},
                "disks": [
                    {"name": "disk1", "status": "DISK_OK"},
                    {"name": "disk2", "status": "DISK_NP_MISSING"},
                ],
            }
        }
    )
    ups_resp = _resp({"upsDevices": []})
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}})
    async with mocked_client([array_resp, ups_resp, notif_resp, _NO_ALERTS]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "critical"
    assert out["unhealthy_disks"][0]["health"] == "missing"
    assert out["disk_count"] == 2


async def test_list_log_files(mocked_client):
    data = {
        "logFiles": [
            {
                "name": "syslog",
                "path": "/var/log/syslog",
                "size": 4096,
                "modifiedAt": "2026-07-01T00:00:00Z",
            }
        ]
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await misc.fetch_log_files(c)
    assert out == [
        {
            "name": "syslog",
            "path": "/var/log/syslog",
            "size": {"bytes": 4096, "human": "4.0 KiB"},
            "modified_at": "2026-07-01T00:00:00Z",
        }
    ]
    assert _sent_query(r) == queries.LOG_FILES


async def test_list_log_files_empty(mocked_client):
    async with mocked_client(_resp({"logFiles": []})) as (c, r):
        assert await misc.fetch_log_files(c) == []


async def test_list_log_files_unsupported_api(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "logFiles" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await misc.fetch_log_files(c, api_version="7.1.0")


async def test_read_log_file_happy_path(mocked_client):
    data = {
        "logFile": {
            "path": "/var/log/syslog",
            "content": "line1\nline2\n",
            "totalLines": 500,
            "startLine": 400,
        }
    }
    async with mocked_client(_resp(data)) as (c, r):
        out = await misc.fetch_log_file(c, "/var/log/syslog", lines=100, start_line=400)
    assert out == {
        "path": "/var/log/syslog",
        "content": "line1\nline2\n",
        "total_lines": 500,
        "start_line": 400,
    }
    assert _sent_vars(r) == {"path": "/var/log/syslog", "lines": 100, "startLine": 400}


async def test_read_log_file_omits_start_line_when_none(mocked_client):
    data = {"logFile": {"path": "/var/log/syslog", "content": "x", "totalLines": 1, "startLine": 1}}
    async with mocked_client(_resp(data)) as (c, r):
        await misc.fetch_log_file(c, "/var/log/syslog")
    assert _sent_vars(r) == {"path": "/var/log/syslog", "lines": 100}


async def test_read_log_file_lines_clamp_no_http(mocked_client):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="500"):
            await misc.fetch_log_file(c, "/var/log/syslog", lines=5000)
    assert r.call_count == 0


async def test_read_log_file_rejects_path_outside_var_log_no_http(mocked_client):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="list_log_files"):
            await misc.fetch_log_file(c, "/etc/shadow")
    assert r.call_count == 0


@pytest.mark.parametrize(
    "path",
    [
        "/var/log/../../etc/passwd",
        "/var/log/../etc/passwd",
        "/var/logevil/x",
        "var/log/syslog",
        "syslog",
        "/var/log/sys\x00log",
        "/var/log/a/../../../etc/shadow",
    ],
)
async def test_read_log_file_rejects_bad_paths_no_http(mocked_client, path):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="list_log_files"):
            await misc.fetch_log_file(c, path)
    assert r.call_count == 0


@pytest.mark.parametrize("lines", [0, -1])
async def test_read_log_file_rejects_nonpositive_lines_no_http(mocked_client, lines):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="lines"):
            await misc.fetch_log_file(c, "/var/log/syslog", lines=lines)
    assert r.call_count == 0


@pytest.mark.parametrize("start_line", [0, -1])
async def test_read_log_file_rejects_start_line_below_one_no_http(mocked_client, start_line):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="start_line"):
            await misc.fetch_log_file(c, "/var/log/syslog", start_line=start_line)
    assert r.call_count == 0


async def test_read_log_file_accepts_var_log_root_and_nested(mocked_client):
    data = {"logFile": {"path": "/var/log/a/b", "content": "x", "totalLines": 1, "startLine": 1}}
    async with mocked_client(_resp(data)) as (c, r):
        await misc.fetch_log_file(c, "/var/log/a/b", start_line=1)
    assert r.call_count == 1


async def test_read_log_file_rejects_empty_path_no_http(mocked_client):
    async with mocked_client(_resp({})) as (c, r):
        with pytest.raises(ToolError, match="list_log_files"):
            await misc.fetch_log_file(c, "")
    assert r.call_count == 0


async def test_read_log_file_unsupported_api(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "logFile" on type "Query".'}],
            "data": None,
        },
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await misc.fetch_log_file(c, "/var/log/syslog", api_version="7.1.0")


@pytest.mark.parametrize(
    ("status", "charge", "runtime", "expected", "reason"),
    [
        ("ONBATT", 1, 600, "critical", "battery charge is 1%"),
        ("ONBATT", 80, 600, "attention", "is on battery"),
        ("ONBATT", 80, 299, "critical", "runtime is 299 seconds"),
        ("ONBATT", 20, 300, "attention", "is on battery"),
        ("ONBATT LOWBATT", None, None, "critical", "low battery"),
        ("ONBATT LOWBATT", 100, 600, "critical", "low battery"),
        ("LOWBATT", 100, 600, "critical", "low battery"),
        (" onbatt ", 0, 0, "critical", "battery charge is 0%"),
        ("COMMLOST", None, None, "ok", None),
        (" on battery ", 0, 0, "critical", "battery charge is 0%"),
        ("ONBATT", None, None, "attention", "is on battery"),
        ("ONLINE", 1, 1, "ok", None),
    ],
)
async def test_health_ups_verdict(mocked_client, status, charge, runtime, expected, reason):
    async with mocked_client(
        [
            _resp({"array": {"state": "STARTED", "disks": []}}),
            _resp(
                {
                    "upsDevices": [
                        {
                            "name": "ups0",
                            "status": status,
                            "battery": {
                                "chargeLevel": charge,
                                "estimatedRuntime": runtime,
                            },
                        }
                    ]
                }
            ),
            _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}}),
        ]
    ) as (client, route):
        out = await misc.fetch_health(client)
    assert route.call_count == 4
    assert out["overall"] == expected
    assert out["checks"] == dict.fromkeys(("array", "ups", "notifications"), "ok")
    if reason:
        assert any(reason in item for item in out["reasons"])
    else:
        assert out["reasons"] == []


@pytest.mark.parametrize("running", [False, True])
async def test_health_parity_errors(mocked_client, running):
    async with mocked_client(
        [
            _resp({"array": {"parityCheckStatus": {"errors": 3, "running": running}}}),
            _resp({"upsDevices": []}),
            _resp({"notifications": None}),
        ]
    ) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["overall"] == "attention"
    assert out["reasons"] == ["Parity check reported 3 errors"]
    assert out["parity_check"] == {"errors": 3, "running": running}


@pytest.mark.parametrize("check", ["array", "ups", "notifications"])
@pytest.mark.parametrize("failure", ["forbidden", "unsupported"])
async def test_health_failed_checks(mocked_client, check, failure):
    index = ("array", "ups", "notifications").index(check)
    responses = [_resp({"array": {}}), _resp({"upsDevices": []}), _resp({})]
    responses[index] = httpx.Response(
        200,
        json={
            "errors": [
                {
                    "message": "FORBIDDEN"
                    if failure == "forbidden"
                    else f'Cannot query field "{check}" on type "Query".'
                }
            ],
            "data": None,
        },
    )
    config_queried = check == "ups" and failure == "forbidden"
    if config_queried:
        responses.append(_resp({"upsConfiguration": {"service": "enable"}}))
    async with mocked_client(responses) as (client, route):
        out = await misc.fetch_health(client)
    assert route.call_count == (5 if config_queried else 4)
    assert out["overall"] == "degraded"
    assert out["checks"] == {
        name: "failed" if name == check else "ok" for name in ("array", "ups", "notifications")
    }
    assert out["reasons"] == [f"{check.capitalize()} check failed or is unsupported"]


@pytest.mark.parametrize("check", ["array", "ups", "notifications"])
@pytest.mark.parametrize("failure", ["auth", "connection"])
async def test_health_actionable_errors_propagate(mocked_client, check, failure):
    index = ("array", "ups", "notifications").index(check)
    responses = [_resp({"array": {}}), _resp({"upsDevices": []}), _resp({})]
    responses[index] = httpx.Response(401) if failure == "auth" else httpx.ConnectError("refused")
    if check != "array" and failure == "auth":
        # Sub-checks run after the array query proved the key valid: auth -> failed.
        if check == "ups":
            responses.append(_resp({"upsConfiguration": {"service": None}}))
        async with mocked_client(responses) as (client, _route):
            out = await misc.fetch_health(client)
        assert out["checks"][check] == "failed"
        assert out["overall"] == "degraded"
        return
    async with mocked_client(responses) as (client, route):
        with pytest.raises(UnraidAuthError if failure == "auth" else UnraidConnectionError):
            await misc.fetch_health(client)
    # The three checks run concurrently, so all were issued before the error surfaced.
    assert route.call_count == 4


@pytest.mark.parametrize(
    "data", [None, {}, {"array": None, "upsDevices": None, "notifications": None}]
)
async def test_health_empty_success(mocked_client, data):
    async with mocked_client(_resp(data)) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["overall"] == "degraded"
    assert out["reasons"] == ["Array check failed or is unsupported"]
    assert out["checks"] == {"array": "failed", "ups": "ok", "notifications": "ok"}
    assert out["disk_count"] == 0
    assert out["ups"] == []


@pytest.mark.parametrize(
    ("disk", "expected"),
    [
        ({"status": "DISK_OK", "color": "RED_ON"}, "critical"),
        ({"status": "DISK_OK", "color": "RED_OFF"}, "critical"),
        ({"status": "DISK_OK", "color": "YELLOW_ON"}, "attention"),
        ({"status": "DISK_OK", "color": "YELLOW_BLINK"}, "attention"),
        ({"status": "DISK_DSBL"}, "critical"),
        ({"status": "DISK_NP_MISSING"}, "critical"),
        ({"status": "DISK_NEW"}, "attention"),
        ({"status": "DISK_WRONG"}, "critical"),
        ({"status": "DISK_DSBL_NEW"}, "critical"),
        ({"status": "DISK_INVALID"}, "critical"),
        ({"status": "DISK_NP_DSBL"}, "critical"),
    ],
)
async def test_health_disk_signal_precedes_failed_check(mocked_client, disk, expected):
    async with mocked_client(
        [
            _resp({"array": {"disks": [{"name": "disk1", **disk}]}}),
            httpx.Response(200, json={"errors": [{"message": "FORBIDDEN"}]}),
            _resp({}),
            _resp({"upsConfiguration": {"service": "enable"}}),
        ]
    ) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["overall"] == expected
    assert out["checks"]["ups"] == "failed"
    assert len(out["reasons"]) == 2
    assert out["reasons"][0].startswith("Disk disk1 is ")


@pytest.mark.parametrize("severity", ["alert", "warning"])
async def test_health_unread_notifications(mocked_client, severity):
    async with mocked_client(
        [
            _resp({"array": {"state": "STOPPED"}}),
            _resp({"upsDevices": []}),
            _resp({"notifications": {"overview": {"unread": {severity: 2}}}}),
        ]
    ) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["overall"] == "attention"
    assert out["reasons"] == [f"Unread {severity} notifications: 2"]


async def test_health_all_healthy(mocked_client):
    async with mocked_client(
        [
            _resp(
                {
                    "array": {
                        "state": "STOPPED",
                        "disks": [{"status": "DISK_OK"}],
                        "parityCheckStatus": {"errors": 0},
                    }
                }
            ),
            _resp({"upsDevices": [{"status": "ONLINE", "battery": {"chargeLevel": 100}}]}),
            _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}}),
        ]
    ) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["overall"] == "ok"
    assert out["reasons"] == []
    assert out["checks"] == dict.fromkeys(("array", "ups", "notifications"), "ok")
    assert out["array_state"] == "STOPPED"


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (_resp({"upsConfiguration": {"service": "disable"}}), "not_configured"),
        (_resp({"upsConfiguration": {"service": "enable"}}), "failed"),
        (_resp({"upsConfiguration": {"service": None}}), "not_configured"),
        (_resp({"upsConfiguration": None}), "failed"),
        (httpx.Response(200, json={"errors": [{"message": "unsupported"}]}), "failed"),
        (
            httpx.Response(
                200,
                json={
                    "data": {"upsConfiguration": {"service": "disable"}},
                    "errors": [{"message": "partial failure"}],
                },
            ),
            "failed",
        ),
    ],
)
async def test_health_ups_configuration_fallback(mocked_client, config, expected):
    async with mocked_client(
        [
            _resp({"array": {"state": "STARTED"}}),
            httpx.Response(200, json={"errors": [{"message": "arbitrary UPS failure"}]}),
            _resp({"notifications": None}),
            config,
        ]
    ) as (client, route):
        out = await misc.fetch_health(client)
    assert route.call_count == 5
    assert _sent_query(route) == queries.UPS_CONFIGURATION
    assert out["checks"] == {"array": "ok", "ups": expected, "notifications": "ok"}
    assert out["overall"] == ("ok" if expected == "not_configured" else "degraded")
    assert out["reasons"] == (
        [] if expected == "not_configured" else ["Ups check failed or is unsupported"]
    )


@pytest.mark.parametrize("disk_status", ["DISK_OK", "DISK_DSBL"])
async def test_health_partial_errors_keep_data(mocked_client, disk_status):
    async with mocked_client(
        [
            httpx.Response(
                200,
                json={
                    "data": {
                        "array": {
                            "state": "STARTED",
                            "parityCheckStatus": None,
                            "disks": [{"name": "disk1", "status": disk_status}],
                        }
                    },
                    "errors": [{"message": "FORBIDDEN", "path": ["array", "parityCheckStatus"]}],
                },
            ),
            _resp({"upsDevices": []}),
            _resp({}),
        ]
    ) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["checks"]["array"] == "failed"
    assert out["array_state"] == "STARTED"
    assert out["disk_count"] == 1
    assert out["overall"] == ("degraded" if disk_status == "DISK_OK" else "critical")
    assert "Array check failed or is unsupported" in out["reasons"]


async def test_array_space_thresholds_are_not_health_flags(mocked_client):
    data = {
        "array": {
            "disks": [{"status": "DISK_OK", "warning": 80, "critical": 90}],
            "caches": [{"status": "DISK_OK", "critical": 90}],
        }
    }
    async with mocked_client(_resp(data)) as (client, _route):
        status = await array.fetch_array_status(client)
        health = await misc.fetch_health(client)
    assert status["data_disks"][0]["health"] == "healthy"
    assert status["caches"][0]["health"] == "healthy"
    assert health["overall"] == "ok"
    assert health["unhealthy_disks"] == []


_NO_DATA = {"errors": [{"message": "No UPS data returned from apcaccess"}], "data": None}
_FORBIDDEN = {
    "errors": [{"message": "denied", "extensions": {"code": "FORBIDDEN"}}],
    "data": None,
}
_UNSUPPORTED = {
    "errors": [{"message": 'Cannot query field "upsDevices" on type "Query".'}],
    "data": None,
}


@pytest.mark.parametrize(
    ("ups", "service", "expected", "calls"),
    [
        (httpx.Response(200, json=_NO_DATA), None, "not_configured", 5),
        (httpx.Response(200, json=_NO_DATA), "DISABLE", "not_configured", 5),
        (httpx.Response(200, json=_FORBIDDEN), "DISABLE", "failed", 4),
        (httpx.Response(403), "DISABLE", "failed", 4),
        (httpx.Response(200, json=_UNSUPPORTED), None, "failed", 4),
    ],
)
async def test_health_not_configured_branches(mocked_client, ups, service, expected, calls):
    responses = [
        _resp({"array": {"state": "STARTED"}}),
        ups,
        _resp({"notifications": None}),
        _resp({"upsConfiguration": {"service": service}}),
    ]
    async with mocked_client(responses[: calls - 1] if calls == 4 else responses) as (
        client,
        route,
    ):
        out = await misc.fetch_health(client)
    assert route.call_count == calls
    assert out["checks"]["ups"] == expected
    assert out["overall"] == ("degraded" if expected == "failed" else "ok")


async def test_health_notifications_http_403_is_failed(mocked_client):
    async with mocked_client(
        [_resp({"array": {"state": "STARTED"}}), _resp({"upsDevices": []}), httpx.Response(403)]
    ) as (client, _route):
        out = await misc.fetch_health(client)
    assert out["checks"] == {"array": "ok", "ups": "ok", "notifications": "failed"}
    assert out["overall"] == "degraded"


_ALERT_ITEM = {
    "id": "n1",
    "title": "Disk 2 SMART",
    "subject": "s",
    "description": "d",
    "importance": "ALERT",
    "link": None,
    "type": "UNREAD",
    "timestamp": "2026-07-01T00:00:00Z",
    "formattedTimestamp": "x",
}


async def test_warnings_and_alerts_happy(mocked_client):
    resp = _resp({"notifications": {"warningsAndAlerts": [_ALERT_ITEM]}})
    async with mocked_client(resp) as (c, r):
        out = await notifications.fetch_warnings_and_alerts(c)
    assert out == [_ALERT_ITEM]
    assert _sent_query(r) == queries.WARNINGS_AND_ALERTS


@pytest.mark.parametrize(
    "data",
    [
        {"notifications": {"warningsAndAlerts": []}},
        {"notifications": {"warningsAndAlerts": None}},
        {"notifications": None},
        None,
    ],
)
async def test_warnings_and_alerts_empty_or_null(mocked_client, data):
    async with mocked_client(_resp(data)) as (c, r):
        assert await notifications.fetch_warnings_and_alerts(c) == []


async def test_warnings_and_alerts_unsupported_raises_friendly_error(mocked_client):
    resp = httpx.Response(
        200,
        json={
            "errors": [
                {"message": 'Cannot query field "warningsAndAlerts" on type "Notifications".'}
            ],
            "data": None,
        },
    )
    async with mocked_client(resp) as (c, r):
        with pytest.raises(ToolError, match="does not support"):
            await notifications.fetch_warnings_and_alerts(c, api_version="7.0.0")


async def test_warnings_and_alerts_other_error_propagates(mocked_client):
    resp = httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})
    async with mocked_client(resp) as (c, r):
        with pytest.raises(UnraidGraphQLError):
            await notifications.fetch_warnings_and_alerts(c)


async def test_health_summary_top_alerts_capped_at_five(mocked_client):
    items = [{**_ALERT_ITEM, "id": f"n{i}", "title": f"t{i}"} for i in range(7)]
    responses = [
        _resp({"array": {"state": "STARTED", "disks": []}}),
        _resp({"upsDevices": []}),
        _resp({"notifications": {"overview": {"unread": {"alert": 7, "warning": 0}}}}),
        _resp({"notifications": {"warningsAndAlerts": items}}),
    ]
    async with mocked_client(responses) as (c, r):
        out = await misc.fetch_health(c)
    assert len(out["top_alerts"]) == 5
    assert out["top_alerts"][0] == {"title": "t0", "importance": "ALERT"}


async def test_health_summary_top_alerts_empty(mocked_client):
    responses = [
        _resp({"array": {"state": "STARTED", "disks": []}}),
        _resp({"upsDevices": []}),
        _resp({"notifications": {"overview": {"unread": {}}}}),
        _NO_ALERTS,
    ]
    async with mocked_client(responses) as (c, r):
        out = await misc.fetch_health(c)
    assert out["top_alerts"] == []


async def test_health_summary_omits_top_alerts_when_unsupported(mocked_client):
    unsupported = httpx.Response(
        200,
        json={
            "errors": [{"message": 'Cannot query field "warningsAndAlerts" on type "X".'}],
            "data": None,
        },
    )
    responses = [
        _resp({"array": {"state": "STARTED", "disks": []}}),
        _resp({"upsDevices": []}),
        _resp({"notifications": {"overview": {"unread": {}}}}),
        unsupported,
    ]
    async with mocked_client(responses) as (c, r):
        out = await misc.fetch_health(c)
    assert "top_alerts" not in out


async def test_warnings_and_alerts_truncates_long_description_and_keeps_null(mocked_client):
    long_item = {**_ALERT_ITEM, "description": "x" * 5000}
    null_item = {**_ALERT_ITEM, "id": "n2", "description": None}
    resp = _resp({"notifications": {"warningsAndAlerts": [long_item, null_item]}})
    async with mocked_client(resp) as (c, r):
        out = await notifications.fetch_warnings_and_alerts(c)
    assert len(out[0]["description"]) < 600
    assert out[0]["description"].endswith("[truncated]")
    assert out[1]["description"] is None


async def test_list_notifications_truncates_long_description(mocked_client):
    resp = _resp({"notifications": {"list": [{"id": "n1", "description": "y" * 5000}]}})
    async with mocked_client(resp) as (c, r):
        out = await notifications.fetch_notifications(c)
    assert out[0]["description"].endswith("[truncated]")


async def test_warnings_and_alerts_limit(mocked_client):
    items = [{**_ALERT_ITEM, "id": f"n{i}"} for i in range(30)]
    async with mocked_client(_resp({"notifications": {"warningsAndAlerts": items}})) as (c, r):
        assert len(await notifications.fetch_warnings_and_alerts(c)) == 20
        assert len(await notifications.fetch_warnings_and_alerts(c, 3)) == 3
    for bad in (0, 101):
        with pytest.raises(ToolError, match="limit"):
            await notifications.fetch_warnings_and_alerts(c, bad)


async def test_top_alerts_force_attention_when_overview_fails(mocked_client):
    overview_err = httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})
    responses = [
        _resp({"array": {"state": "STARTED", "disks": []}}),
        _resp({"upsDevices": []}),
        overview_err,
        _resp({"notifications": {"warningsAndAlerts": [_ALERT_ITEM]}}),
    ]
    async with mocked_client(responses) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "attention"
    assert out["top_alerts"][0]["importance"] == "ALERT"
