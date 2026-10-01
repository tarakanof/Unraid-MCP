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
from mcp.client import Client
from mcp.shared.exceptions import MCPError

from unraid_mcp import resources
from unraid_mcp.client import UnraidClient
from unraid_mcp.errors import UnraidConfigError
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
        async with Client(server) as session:
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
        result = await session.read_resource(uri)
    assert len(result.contents) == 1
    content = result.contents[0]
    assert content.mime_type == "application/json"
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
        with pytest.raises(MCPError) as excinfo:
            await session.read_resource(resources.SYSTEM_INFO_URI)
    msg = str(excinfo.value)
    assert resources.SYSTEM_INFO_URI in msg
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


async def test_health_resource_when_box_unreachable():
    """Connection failures retain their actionable message at the resource boundary."""
    async with _session(httpx.ConnectError("connection refused")) as (session, _route):
        with pytest.raises(MCPError) as excinfo:
            await session.read_resource(resources.HEALTH_URI)
    assert "connect" in str(excinfo.value).lower()
    assert KEY not in str(excinfo.value)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("expected", ["ok", "attention", "critical", "degraded"])
async def test_health_tool_and_resource_match(mode, expected):
    def respond(request):
        query = json.loads(request.content)["query"]
        if "upsDevices" in query and expected == "degraded":
            return httpx.Response(200, json={"errors": [{"message": "FORBIDDEN"}]})
        data = {
            "array": {"state": "STARTED", "disks": [{"status": "DISK_OK"}]},
            "upsDevices": [
                {
                    "name": "ups0",
                    "status": "ONLINE" if expected in ("ok", "degraded") else "ONBATT",
                    "battery": {"chargeLevel": 1 if expected == "critical" else 80},
                }
            ],
            "notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}},
        }
        return httpx.Response(200, json={"data": data})

    with respx.mock:
        respx.post(URL).mock(side_effect=respond)
        async with Client(build_server(make_settings()), mode=mode) as session:
            tool = await session.call_tool("get_health_summary", {})
            resource = await session.read_resource(resources.HEALTH_URI)
    assert tool.is_error is False
    out = tool.structured_content
    assert out == json.loads(resource.contents[0].text)
    assert out["overall"] == expected
    assert out["checks"]["ups"] == ("failed" if expected == "degraded" else "ok")
    assert bool(out["reasons"]) == (expected != "ok")


@pytest.mark.parametrize("failure", ["auth", "connection", "configuration"])
async def test_health_tool_and_resource_actionable_errors(failure, monkeypatch):
    response = httpx.Response(401) if failure == "auth" else httpx.ConnectError("refused")
    if failure == "configuration":
        execute = UnraidClient.execute_with_errors

        async def bad_config(self, query, variables=None):
            if "parityCheckStatus" in query:
                raise UnraidConfigError("Check UNRAID_API_URL configuration")
            return await execute(self, query, variables)

        monkeypatch.setattr(UnraidClient, "execute_with_errors", bad_config)
        response = httpx.Response(200, json=_CANNED)
    async with _session(response) as (session, _route):
        result = await session.call_tool("get_health_summary", {})
        assert result.is_error
        text = " ".join(item.text for item in result.content if item.type == "text")
        assert "UNRAID_API_" in text
        assert KEY not in text
        with pytest.raises(MCPError) as excinfo:
            await session.read_resource(resources.HEALTH_URI)
        assert "UNRAID_API_" in str(excinfo.value)
        assert KEY not in str(excinfo.value)
