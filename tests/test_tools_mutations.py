"""Tests for mutation gating: registration behind a flag + confirm requirement."""

from __future__ import annotations

import json

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from unraid_mcp import queries
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, docker, notifications, vm

READ_TOOLS = {"get_system_info", "get_array_status", "list_docker_containers", "get_health_summary"}
# The complete set of mutating tools — kept exhaustive so the registration
# tests catch any tool that fails to register or leaks into read-only mode.
MUTATION_TOOLS = {
    "start_array",
    "stop_array",
    "start_parity_check",
    "pause_parity_check",
    "resume_parity_check",
    "cancel_parity_check",
    "start_docker_container",
    "stop_docker_container",
    "restart_docker_container",
    "start_vm",
    "stop_vm",
    "pause_vm",
    "resume_vm",
    "reboot_vm",
    "force_stop_vm",
    "archive_notification",
    "archive_all_notifications",
    "mark_notification_unread",
    "delete_notification",
}


async def _tool_names(settings) -> set[str]:
    mcp = build_server(settings)
    return {t.name for t in await mcp.list_tools()}


async def test_mutations_absent_by_default(settings_factory):
    names = await _tool_names(settings_factory(allow_mutations=False))
    assert names >= READ_TOOLS
    assert names.isdisjoint(MUTATION_TOOLS)


async def test_mutations_present_when_enabled(settings_factory):
    read_only = await _tool_names(settings_factory(allow_mutations=False))
    with_mutations = await _tool_names(settings_factory(allow_mutations=True))
    # Exactly the mutation set appears, nothing more, nothing fewer.
    assert with_mutations - read_only == MUTATION_TOOLS


async def test_correcting_parity_check_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_start_parity(client, correct=True, confirm=False)
        assert route.call_count == 0


async def test_non_correcting_parity_check_needs_no_confirm(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"parityCheck": {"start": True}}})
    ) as (
        client,
        route,
    ):
        await array.do_start_parity(client, correct=False)
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content)["variables"] == {"correct": False}


async def test_raw_query_gated(settings_factory):
    assert "run_graphql_query" not in await _tool_names(settings_factory(allow_raw_query=False))
    assert "run_graphql_query" in await _tool_names(settings_factory(allow_raw_query=True))


async def test_stop_array_requires_confirm_no_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await array.do_stop_array(client, confirm=False)
        assert route.call_count == 0


async def test_stop_array_with_confirm_sends_mutation(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"array": {"setState": {"state": "STOPPED"}}}})
    ) as (client, route):
        await array.do_stop_array(client, confirm=True)
        assert route.call_count == 1
        assert json.loads(route.calls.last.request.content)["query"] == queries.STOP_ARRAY


async def test_start_container_sends_id_variable(mocked_client):
    async with mocked_client(
        httpx.Response(200, json={"data": {"docker": {"start": {"id": "1:a"}}}})
    ) as (client, route):
        await docker.do_start_container(client, "1:abc")
        body = json.loads(route.calls.last.request.content)
        assert body["query"] == queries.START_CONTAINER
        assert body["variables"] == {"id": "1:abc"}


async def test_stop_container_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_stop_container(client, "1:abc", confirm=False)
        assert route.call_count == 0


async def test_restart_container_stops_then_starts(mocked_client):
    resp = httpx.Response(200, json={"data": {"docker": {}}})
    async with mocked_client([resp, resp]) as (client, route):
        await docker.do_restart_container(client, "1:abc", confirm=True)
        assert route.call_count == 2
        queries_sent = [json.loads(call.request.content)["query"] for call in route.calls]
        assert queries_sent == [queries.STOP_CONTAINER, queries.START_CONTAINER]


async def test_force_stop_vm_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await vm.do_force_stop_vm(client, "uuid-1", confirm=False)
        assert route.call_count == 0


async def test_delete_notification_requires_confirm(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await notifications.do_delete_notification(client, "n1", "ARCHIVE", confirm=False)
        assert route.call_count == 0


async def test_delete_notification_with_confirm_sends_type(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {"deleteNotification": {}}})) as (
        client,
        route,
    ):
        await notifications.do_delete_notification(client, "n1", "ARCHIVE", confirm=True)
        body = json.loads(route.calls.last.request.content)
        assert body["variables"] == {"id": "n1", "type": "ARCHIVE"}
