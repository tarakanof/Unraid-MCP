"""Tests for MCP resources and the triage prompt.

Resources and prompts are exercised end-to-end through an in-memory client
session so the real lifespan (shared UnraidClient) runs, exactly as a live MCP
host would drive them. respx mocks the Unraid GraphQL endpoint.
"""

from __future__ import annotations

import contextlib
import json

import httpx
import pytest
import respx
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from unraid_mcp import resources
from unraid_mcp.client import UnraidClient
from unraid_mcp.server import build_server
from unraid_mcp.tools.misc import fetch_health
from unraid_mcp.tools.system import fetch_system_info

from .conftest import KEY, URL, make_settings

# A single canned GraphQL payload served for every POST (probe + every fetch).
# Every shaper degrades gracefully on missing keys, so this yields a valid,
# deterministic response for both resources.
_CANNED = {
    "data": {
        "array": {
            "state": "STARTED",
            "capacity": {"kilobytes": {"total": "1000", "used": "400", "free": "600"}},
            "disks": [{"name": "disk1", "status": "DISK_OK", "size": "500"}],
        },
        "info": {"os": {"hostname": "tower"}, "cpu": {"cores": 8}},
    }
}


@contextlib.asynccontextmanager
async def _session(responses):
    """Build the real server and yield an initialized in-memory client session,
    with the Unraid endpoint mocked by respx."""
    with respx.mock:
        route = respx.post(URL)
        if isinstance(responses, (list, Exception)):
            route.mock(side_effect=responses)
        else:
            route.mock(return_value=responses)
        server = build_server(make_settings())
        async with create_connected_server_and_client_session(server) as session:
            yield session, route


async def _direct_fetch(fetch):
    """Compute a fetch_* result directly against the same canned response,
    to compare a resource read against the tool's output shape."""
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=_CANNED))
        async with httpx.AsyncClient() as http:
            client = UnraidClient(URL, KEY, http, host_label="tower.local")
            return await fetch(client)


@pytest.mark.parametrize(
    ("uri", "fetch"),
    [(resources.HEALTH_URI, fetch_health), (resources.SYSTEM_INFO_URI, fetch_system_info)],
)
async def test_resource_matches_tool_shape(uri, fetch):
    """Reading a resource returns exactly the corresponding tool's fetch output."""
    expected = await _direct_fetch(fetch)
    async with _session(httpx.Response(200, json=_CANNED)) as (session, _route):
        result = await session.read_resource(AnyUrl(uri))
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mimeType == "application/json"
    assert json.loads(content.text) == expected


async def test_resources_are_listed():
    """Both resources are advertised to clients."""
    async with _session(httpx.Response(200, json=_CANNED)) as (session, _route):
        listed = await session.list_resources()
    uris = {str(r.uri) for r in listed.resources}
    assert resources.HEALTH_URI in uris
    assert resources.SYSTEM_INFO_URI in uris


async def test_resource_error_when_box_unreachable():
    """Box down -> a clean resource error reaches the client, not a raw crash."""
    down = httpx.ConnectError("connection refused")
    async with _session(down) as (session, _route):
        with pytest.raises(McpError) as excinfo:
            await session.read_resource(AnyUrl(resources.HEALTH_URI))
    msg = str(excinfo.value)
    assert "unraid://health" in msg
    # The secret-free connection hint from UnraidConnectionError is surfaced.
    assert "connect" in msg.lower()
    assert KEY not in msg


async def test_triage_prompt_registers_and_renders():
    """The triage prompt is advertised and renders instructions that name the
    entry-point tool and the optional focus."""
    async with _session(httpx.Response(200, json=_CANNED)) as (session, _route):
        listed = await session.list_prompts()
        names = {p.name for p in listed.prompts}
        assert "triage" in names

        result = await session.get_prompt("triage", {"focus": "disks"})
    assert result.messages
    text = " ".join(
        m.content.text for m in result.messages if getattr(m.content, "type", None) == "text"
    )
    assert "get_health_summary" in text
    assert "disks" in text


async def test_triage_prompt_renders_without_focus():
    """The focus argument is optional; the prompt still renders."""
    async with _session(httpx.Response(200, json=_CANNED)) as (session, _route):
        result = await session.get_prompt("triage", {})
    text = " ".join(
        m.content.text for m in result.messages if getattr(m.content, "type", None) == "text"
    )
    assert "get_health_summary" in text
