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
