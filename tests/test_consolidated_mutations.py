"""Consolidated ``action``-dispatch mutation tools (#160).

Each merged tool joins actions from one module and one annotation tier only.
For every ``action`` value: refused without ``confirm=true`` with no HTTP
request, happy path sends the right mutation, and an invalid ``action`` is a
tool error with no HTTP request.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from tests.conftest import URL
from unraid_mcp import queries
from unraid_mcp.server import build_server
from unraid_mcp.tools import array, docker, notifications, vm
from unraid_mcp.tools._base import MUTATING_IDEMPOTENT

_CONTAINER = {"id": "1:a", "names": ["/plex"], "state": "RUNNING", "status": "Up"}
_OVERVIEW = {
    "unread": {"info": 0, "warning": 0, "alert": 0, "total": 0},
    "archive": {"info": 1, "warning": 0, "alert": 0, "total": 1},
}
_NOTE = {"id": "n1", "title": "t", "importance": "INFO", "type": "ARCHIVE"}

# tool -> (base args, {action: (expected query, response data, expected variables)})
CASES: dict[str, tuple[dict[str, Any], dict[str, tuple[str, dict[str, Any], dict[str, Any]]]]] = {
    "docker_container_power": (
        {"container_id": "1:a"},
        {
            "start": (queries.START_CONTAINER, {"docker": {"start": _CONTAINER}}, {"id": "1:a"}),
            "pause": (queries.PAUSE_CONTAINER, {"docker": {"pause": _CONTAINER}}, {"id": "1:a"}),
            "unpause": (
                queries.UNPAUSE_CONTAINER,
                {"docker": {"unpause": _CONTAINER}},
                {"id": "1:a"},
            ),
        },
    ),
    "vm_power": (
        {"vm_id": "vm1"},
        {
            "start": (queries.VM_START, {"vm": {"start": True}}, {"id": "vm1"}),
            "pause": (queries.VM_PAUSE, {"vm": {"pause": True}}, {"id": "vm1"}),
            "resume": (queries.VM_RESUME, {"vm": {"resume": True}}, {"id": "vm1"}),
        },
    ),
    "parity_check_control": (
        {},
        {
            "pause": (queries.PAUSE_PARITY, {"parityCheck": {"pause": []}}, {}),
            "resume": (queries.RESUME_PARITY, {"parityCheck": {"resume": []}}, {}),
            "cancel": (queries.CANCEL_PARITY, {"parityCheck": {"cancel": []}}, {}),
        },
    ),
    "notification_archive": (
        {"notification_id": "n1"},
        {
            "archive": (
                queries.ARCHIVE_NOTIFICATION,
                {"archiveNotification": _NOTE},
                {"id": "n1"},
            ),
            "unarchive": (
                queries.UNREAD_NOTIFICATION,
                {"unreadNotification": _NOTE},
                {"id": "n1"},
            ),
        },
    ),
    "notification_archive_bulk": (
        {"ids": ["n1", "n2"]},
        {
            "archive": (
                queries.ARCHIVE_NOTIFICATIONS,
                {"archiveNotifications": _OVERVIEW},
                {"ids": ["n1", "n2"]},
            ),
            "unarchive": (
                queries.UNARCHIVE_NOTIFICATIONS,
                {"unarchiveNotifications": _OVERVIEW},
                {"ids": ["n1", "n2"]},
            ),
        },
    ),
}

# Exact refusal consequence per (tool, action) — unchanged from the old tools.
CONSEQUENCES = {
    ("docker_container_power", "start"): "start container '1:a'",
    ("docker_container_power", "pause"): "pause container '1:a'",
    ("docker_container_power", "unpause"): "unpause container '1:a'",
    ("vm_power", "start"): "start VM 'vm1'",
    ("vm_power", "pause"): "pause VM 'vm1'",
    ("vm_power", "resume"): "resume VM 'vm1'",
    ("parity_check_control", "pause"): "pause the parity check",
    ("parity_check_control", "resume"): "resume the parity check",
    ("parity_check_control", "cancel"): "cancel the parity check",
    ("notification_archive", "archive"): "archive notification 'n1'",
    ("notification_archive", "unarchive"): "mark notification 'n1' unread",
    ("notification_archive_bulk", "archive"): "archive 2 notification(s)",
    ("notification_archive_bulk", "unarchive"): "unarchive 2 notification(s)",
}

ACTION_CASES = [(tool, action) for tool, (_, acts) in CASES.items() for action in acts]
_IDS = [f"{t}-{a}" for t, a in ACTION_CASES]


async def _call(settings_factory, tool: str, args: dict[str, Any], data: dict[str, Any]):
    async def no_elicit(context, params):
        pytest.fail("non-destructive consolidated tools must not elicit")

    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": data}))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, raise_exceptions=False, elicitation_callback=no_elicit) as s:
            route.calls.clear()  # ignore the lifespan version probe
            result = await s.call_tool(tool, args)
            return result, route


async def test_schema_and_tier(settings_factory):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        mcp = build_server(settings_factory(allow_mutations=True, allow_dangerous=True))
        async with Client(mcp) as s:
            tools = {t.name: t for t in (await s.list_tools()).tools}
    for name, (_, acts) in CASES.items():
        tool = tools[name]
        assert tool.input_schema["properties"]["action"]["enum"] == list(acts), name
        assert "action" in tool.input_schema["required"]
        ann = tool.annotations
        assert (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint) == (
            MUTATING_IDEMPOTENT.read_only_hint,
            MUTATING_IDEMPOTENT.destructive_hint,
            MUTATING_IDEMPOTENT.idempotent_hint,
        ), name
    # Policy: an action-dispatch tool never mixes in a destructive action.
    for tool in tools.values():
        if "action" in tool.input_schema.get("properties", {}):
            assert not tool.annotations.destructive_hint, tool.name


@pytest.mark.parametrize("tool,action", ACTION_CASES, ids=_IDS)
async def test_refused_without_confirm_no_http(settings_factory, tool, action):
    base, acts = CASES[tool]
    _, data, _ = acts[action]
    result, route = await _call(settings_factory, tool, {**base, "action": action}, data)
    assert result.is_error is True
    text = result.content[0].text
    assert f"Refusing to {CONSEQUENCES[(tool, action)]} without explicit confirmation" in text
    assert route.call_count == 0


@pytest.mark.parametrize("tool,action", ACTION_CASES, ids=_IDS)
async def test_happy_path(settings_factory, tool, action):
    base, acts = CASES[tool]
    query, data, variables = acts[action]
    result, route = await _call(
        settings_factory, tool, {**base, "action": action, "confirm": True}, data
    )
    assert result.is_error is False, result.content[0].text
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["query"] == query
    assert body.get("variables") == variables
    assert isinstance(result.structured_content, dict)


@pytest.mark.parametrize("tool", list(CASES))
@pytest.mark.parametrize("confirm", [False, True])
async def test_invalid_action_no_http(settings_factory, tool, confirm):
    base, _ = CASES[tool]
    args = {**base, "action": "stop", "confirm": confirm}
    result, route = await _call(settings_factory, tool, args, {})
    assert result.is_error is True
    assert route.call_count == 0


@pytest.mark.parametrize(
    "fn,args",
    [
        (docker.do_container_power, ("1:a", "stop", True)),
        (vm.do_vm_power, ("vm1", "reset", True)),
        (array.do_parity_check_control, ("start", True)),
        (notifications.do_notification_archive, ("n1", "delete", True)),
        (notifications.do_notification_archive_bulk, (["n1"], "delete", True)),
    ],
)
async def test_logic_rejects_invalid_action_pre_network(mocked_client, fn, args):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError, match="Invalid action"):
            await fn(client, *args)
        assert route.call_count == 0


async def test_bulk_rejects_empty_ids_pre_network(settings_factory):
    args = {"ids": [], "action": "unarchive", "confirm": True}
    result, route = await _call(settings_factory, "notification_archive_bulk", args, {})
    assert result.is_error is True
    assert "non-empty" in result.content[0].text
    assert route.call_count == 0


@pytest.mark.parametrize("action", ["pause", "unpause"])
async def test_container_power_threads_api_version(mocked_client, action):
    msg = f'Cannot query field "{action}" on type "DockerMutations".'
    unknown = httpx.Response(200, json={"errors": [{"message": msg}]})
    async with mocked_client(unknown) as (client, route):
        with pytest.raises(ToolError) as exc:
            await docker.do_container_power(client, "1:a", action, True, api_version="2.100.0")
        text = str(exc.value)
        assert "does not support" in text
        assert "Server reports API 2.100.0" in text
        assert route.call_count == 1
