"""Registry-wide contract tests driven through the in-memory MCP client.

Enumerates tools via ``tools/list`` + annotations so newly added tools are
covered automatically: every mutating/destructive tool must refuse without
``confirm`` and make zero HTTP requests; every read tool must map GraphQL
errors to a tool error and survive an empty ``data`` payload.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx
import pytest
import respx
from mcp.client import Client

from tests.conftest import KEY, URL
from unraid_mcp.server import build_server

# Free-form string params the tool validates against a fixed vocabulary.
_VALID_STRINGS = {"importance": "INFO", "type": "UNREAD"}


def _dummy_args(schema: dict[str, Any]) -> dict[str, Any]:
    """Valid-shaped args for every required property except ``confirm``."""
    props = schema.get("properties", {})
    args: dict[str, Any] = {}
    for name in schema.get("required", []):
        if name == "confirm":
            continue
        prop = props[name]
        if name in _VALID_STRINGS:
            args[name] = _VALID_STRINGS[name]
        elif "enum" in prop:
            args[name] = prop["enum"][0]
        elif prop.get("type") == "array":
            args[name] = ["x"]
        elif prop.get("type") == "integer":
            args[name] = 1
        elif prop.get("type") == "boolean":
            args[name] = False
        elif prop.get("type") == "object":
            args[name] = {}
        else:
            args[name] = "x"
    return args


@contextlib.asynccontextmanager
async def _session(settings_factory, response: httpx.Response, **flags):
    with respx.mock(assert_all_called=False) as router:
        route = router.post(URL).mock(return_value=response)
        mcp = build_server(settings_factory(**flags))
        async with Client(mcp, raise_exceptions=False) as session:
            yield session, route


_ALL_FLAGS = {"allow_mutations": True, "allow_dangerous": True, "allow_raw_query": True}


def _discover() -> list[Any]:
    """Tools registered with every flag on, via a real ``tools/list``.

    Done at import time so each tool becomes its own parametrized test case.
    """
    from tests.conftest import make_settings

    async def go() -> list[Any]:
        mcp = build_server(make_settings(**_ALL_FLAGS))
        with respx.mock(assert_all_called=False) as router:
            router.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
            async with Client(mcp) as session:
                return list((await session.list_tools()).tools)

    return asyncio.run(go())


def _is_read_only(tool) -> bool:
    return bool(tool.annotations and tool.annotations.read_only_hint)


_TOOLS = _discover()
MUTATING_TOOLS = [t for t in _TOOLS if not _is_read_only(t)]
# Hard-coded so a mutation mis-annotated READ_ONLY (and thus dropped from the
# discovered set) fails the test instead of silently losing refusal coverage.
EXPECTED_MUTATING = {
    "start_array", "stop_array", "start_parity_check", "pause_parity_check",
    "resume_parity_check", "cancel_parity_check", "start_docker_container",
    "stop_docker_container", "restart_docker_container", "pause_docker_container",
    "unpause_docker_container", "update_docker_container", "update_docker_containers",
    "start_vm", "stop_vm", "pause_vm", "resume_vm", "reboot_vm", "force_stop_vm", "reset_vm",
    "archive_notification", "archive_all_notifications", "mark_notification_unread",
    "delete_notification", "archive_notifications", "unarchive_notifications",
    "unarchive_all_notifications", "delete_archived_notifications", "create_notification",
    "mount_array_disk", "unmount_array_disk", "clear_disk_statistics", "add_disk_to_array",
    "remove_disk_from_array", "remove_docker_container", "update_all_docker_containers",
}  # fmt: skip

# Every read tool with valid args and its expected result on `data: {}`:
#   "dict"  -> success, dict result       "list" -> success, `{"result": []}`
#   other   -> intentional ToolError containing that friendly text
# The stats tool streams over a subscription (5s timeout against a plain HTTP
# mock) and has its own error/empty coverage in test_tools_stats.py.
READ_CASES: dict[str, tuple[dict[str, Any], str]] = {
    "get_system_info": ({}, "dict"),
    "get_system_metrics": ({}, "dict"),
    "get_services": ({}, "list"),
    "get_system_time": ({}, "dict"),
    "get_array_status": ({}, "dict"),
    "get_parity_status": ({}, "dict"),
    "get_parity_history": ({}, "list"),
    "list_disks": ({}, "list"),
    "get_disk": ({"disk_id": "x"}, "No disk matching 'x'"),
    "list_docker_containers": ({}, "list"),
    "get_docker_container": ({"identifier": "x"}, "No Docker container matching 'x'"),
    "list_docker_networks": ({}, "list"),
    "get_docker_container_logs": ({"container_id": "x"}, "dict"),
    "check_docker_updates": ({}, "list"),
    "list_vms": ({}, "list"),
    "list_shares": ({}, "list"),
    "get_notifications_overview": ({}, "dict"),
    "list_notifications": ({}, "list"),
    "get_ups_status": ({}, "list"),
    "list_network_interfaces": ({}, "list"),
    "whoami": ({}, "dict"),
    "get_connect_status": ({}, "dict"),
    "list_plugins": ({}, "list"),
    "get_health_summary": ({}, "dict"),
    "list_log_files": ({}, "list"),
    "read_log_file": ({"path": "/var/log/syslog"}, "dict"),
    "run_graphql_query": ({"query": "query { __typename }"}, "dict"),
}
_STATS_TOOL = "get_docker_container_stats"


def test_discovery_is_not_vacuous():
    discovered = {t.name for t in MUTATING_TOOLS}
    assert discovered == EXPECTED_MUTATING
    assert all("confirm" in t.input_schema["properties"] for t in MUTATING_TOOLS)
    read = {t.name for t in _TOOLS if _is_read_only(t)}
    assert read == set(READ_CASES) | {_STATS_TOOL}
    # Every required read arg is supplied by READ_CASES.
    for t in _TOOLS:
        if t.name in READ_CASES:
            assert set(t.input_schema.get("required", [])) <= set(READ_CASES[t.name][0])


@pytest.mark.parametrize("tool", MUTATING_TOOLS, ids=lambda t: t.name)
async def test_mutating_tool_refuses_without_confirm_no_http(settings_factory, tool):
    ok = httpx.Response(200, json={"data": {}})
    async with _session(settings_factory, ok, **_ALL_FLAGS) as (session, route):
        baseline = route.call_count  # lifespan version probe, if any
        result = await session.call_tool(tool.name, _dummy_args(tool.input_schema))
        assert result.is_error is True
        assert "Refusing to" in result.content[0].text
        assert route.call_count == baseline, "refusal made an HTTP request"


@pytest.mark.parametrize("name", sorted(READ_CASES))
async def test_read_tool_maps_graphql_error_to_tool_error(settings_factory, name):
    err = httpx.Response(200, json={"errors": [{"message": "boom-upstream"}], "data": None})
    args, _ = READ_CASES[name]
    async with _session(settings_factory, err, **_ALL_FLAGS) as (session, _):
        result = await session.call_tool(name, args)
    text = result.content[0].text
    assert result.is_error is True
    assert "boom-upstream" in text
    assert "GraphQL error" in text
    assert KEY not in text


@pytest.mark.parametrize("name", sorted(READ_CASES))
async def test_read_tool_empty_data(settings_factory, name):
    empty = httpx.Response(200, json={"data": {}})
    args, expected = READ_CASES[name]
    async with _session(settings_factory, empty, **_ALL_FLAGS) as (session, _):
        result = await session.call_tool(name, args)
    if expected in {"dict", "list"}:
        assert result.is_error is False, result.content[0].text
        sc = result.structured_content
        assert isinstance(sc, dict)
        if expected == "list":
            assert sc == {"result": []}
    else:
        assert result.is_error is True
        assert expected in result.content[0].text
