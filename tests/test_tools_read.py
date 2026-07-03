"""Tests for read-only tool logic functions."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from unraid_mcp import queries
from unraid_mcp.errors import UnraidGraphQLError
from unraid_mcp.tools import array, docker, misc, notifications, shares, system, vm


def _resp(data):
    return httpx.Response(200, json={"data": data})


def _sent_query(route):
    return json.loads(route.calls.last.request.content)["query"]


def _sent_vars(route):
    return json.loads(route.calls.last.request.content)["variables"]


async def test_system_info(mocked_client):
    async with mocked_client(_resp({"info": {"os": {"hostname": "tower"}}})) as (client, route):
        out = await system.fetch_system_info(client)
    assert out == {"os": {"hostname": "tower"}}
    assert _sent_query(route) == queries.SYSTEM_INFO


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
    async with mocked_client(_resp(data)) as (client, route):
        out = await system.fetch_metrics(client)
    assert out["cpu"] == {"percent_total": 12.3, "per_core": [5.0, 20.0]}
    assert out["memory"]["total"] == {"bytes": 17179869184, "human": "16.0 GiB"}
    assert out["memory"]["percent_total"] == 50.0
    assert out["temperature"]["summary"]["warning_count"] == 0
    assert _sent_query(route) == queries.SYSTEM_METRICS


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


async def test_parity_status_and_history(mocked_client):
    async with mocked_client(_resp({"array": {"parityCheckStatus": {"status": "COMPLETED"}}})) as (
        c,
        r,
    ):
        assert (await array.fetch_parity_status(c))["status"] == "COMPLETED"
    async with mocked_client(_resp({"parityHistory": [{"status": "OK"}]})) as (c, r):
        assert await array.fetch_parity_history(c) == [{"status": "OK"}]


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
        {"upsDevices": [{"name": "ups0", "status": "Online", "battery": {"chargeLevel": 100}}]}
    )
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 1, "warning": 0}}}})
    async with mocked_client([array_resp, ups_resp, notif_resp]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "attention"  # a failed disk + an alert
    assert out["array_state"] == "STARTED"
    assert out["unhealthy_disks"][0]["health"] == "failed"
    assert out["ups"][0]["battery_pct"] == 100


async def test_health_summary_degrades_when_ups_unavailable(mocked_client):
    array_resp = _resp({"array": {"state": "STARTED", "disks": []}})
    ups_err = httpx.Response(200, json={"errors": [{"message": "no ups"}], "data": None})
    notif_resp = _resp({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}})
    async with mocked_client([array_resp, ups_err, notif_resp]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "ok"
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
    async with mocked_client([array_resp, ups_resp, notif_resp]) as (c, r):
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
    async with mocked_client([array_resp, ups_resp, notif_resp]) as (c, r):
        out = await misc.fetch_health(c)
    assert out["overall"] == "attention"
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
    data = {"logFile": {"path": "/var/log/syslog", "content": "x", "totalLines": 1, "startLine": 0}}
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
