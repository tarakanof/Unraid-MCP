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
# Read tools callable with no arguments (the rest need ids/paths). The stats
# tool streams over a subscription (5s timeout against a plain HTTP mock) and
# has its own error/empty coverage in test_tools_stats.py.
_SKIP_READS = {"get_docker_container_stats"}
READ_TOOLS_NO_ARGS = [
    t
    for t in _TOOLS
    if _is_read_only(t) and not _dummy_args(t.input_schema) and t.name not in _SKIP_READS
]


def test_discovery_is_not_vacuous():
    # Every non-read-only tool must expose `confirm`, otherwise it can't be gated.
    assert all("confirm" in t.input_schema["properties"] for t in MUTATING_TOOLS)
    assert len(MUTATING_TOOLS) == len([t for t in _TOOLS if not _is_read_only(t)]) >= 36
    assert len(READ_TOOLS_NO_ARGS) >= 15


@pytest.mark.parametrize("tool", MUTATING_TOOLS, ids=lambda t: t.name)
async def test_mutating_tool_refuses_without_confirm_no_http(settings_factory, tool):
    ok = httpx.Response(200, json={"data": {}})
    async with _session(settings_factory, ok, **_ALL_FLAGS) as (session, route):
        baseline = route.call_count  # lifespan version probe, if any
        result = await session.call_tool(tool.name, _dummy_args(tool.input_schema))
        assert result.is_error is True
        assert "Refusing to" in result.content[0].text
        assert route.call_count == baseline, "refusal made an HTTP request"


@pytest.mark.parametrize("tool", READ_TOOLS_NO_ARGS, ids=lambda t: t.name)
async def test_read_tool_maps_graphql_error_to_tool_error(settings_factory, tool):
    err = httpx.Response(200, json={"errors": [{"message": "Internal server error"}], "data": None})
    async with _session(settings_factory, err, **_ALL_FLAGS) as (session, _):
        result = await session.call_tool(tool.name, {})
        assert result.is_error is True
        assert KEY not in result.content[0].text


@pytest.mark.parametrize("tool", READ_TOOLS_NO_ARGS, ids=lambda t: t.name)
async def test_read_tool_survives_empty_data(settings_factory, tool):
    """`data: {}` (every field missing) must never surface an unhandled crash:
    either a shaped result or a clean tool error, never a raw exception text."""
    empty = httpx.Response(200, json={"data": {}})
    async with _session(settings_factory, empty, **_ALL_FLAGS) as (session, _):
        result = await session.call_tool(tool.name, {})
        if result.is_error:
            text = result.content[0].text
            assert "Traceback" not in text
            assert "NoneType" not in text
            assert "KeyError" not in text
