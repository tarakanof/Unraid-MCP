"""End-to-end test through the MCP protocol with a mocked Unraid backend.

Uses the SDK in-memory client/server pair. respx intercepts the httpx client
created inside the server lifespan, so this exercises the full path:
session → tool → client → (mocked) GraphQL → shaped result.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp.client import Client

from unraid_mcp.server import build_server

URL = "https://tower.local/graphql"


# "auto" takes the SDK's direct-dispatch fast path (no JSON-RPC framing);
# "legacy" runs real memory streams and a real initialize handshake, like
# v1's create_connected_server_and_client_session did. Cover both so result
# (de)serialization and the handshake stay exercised.
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_list_and_call_through_protocol(settings_factory, mode):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "tower"}}}})
        )
        mcp = build_server(settings_factory(allow_mutations=False))
        async with Client(mcp, raise_exceptions=True, mode=mode) as session:
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "get_system_info" in tools
            assert "get_health_summary" in tools
            assert "stop_array" not in tools  # mutations disabled by default

            result = await session.call_tool("get_system_info", {})
            assert result.is_error is False
            assert result.structured_content["os"]["hostname"] == "tower"


async def test_mutations_callable_when_enabled(settings_factory):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"data": {"array": {"setState": {"state": "STARTED"}}}}
            )
        )
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, raise_exceptions=True) as session:
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "start_array" in tools
            # Confirm-gated stop_array refuses without confirm and makes no change.
            refused = await session.call_tool("stop_array", {"confirm": False})
            assert refused.is_error is True


async def test_probe_failure_does_not_block_startup(settings_factory):
    """A failed version probe must never prevent the server from serving tools.

    The probe (queries.API_PROBE) is the first request in the lifespan; we make
    every GraphQL call return a validation error. list_tools + get_system_info
    must still succeed, with probed versions left None.
    """
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"errors": [{"message": "Something broke"}], "data": None}
            )
        )
        mcp = build_server(settings_factory(allow_mutations=False))
        async with Client(mcp, raise_exceptions=True) as session:
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "get_system_info" in tools


async def test_system_info_exposes_probed_versions(settings_factory):
    """get_system_info merges the startup-probed versions at the top level."""
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "info": {
                            "os": {"hostname": "tower"},
                            "versions": {"core": {"api": "7.2.0", "unraid": "7.2.0"}},
                        }
                    }
                },
            )
        )
        mcp = build_server(settings_factory(allow_mutations=False))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool("get_system_info", {})
            assert result.is_error is False
            assert result.structured_content["api_version"] == "7.2.0"
            assert result.structured_content["unraid_version"] == "7.2.0"


async def test_app_context_carries_probed_versions(settings_factory):
    """get_app_context surfaces the versions stored on AppContext at startup."""
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"info": {"versions": {"core": {"api": "7.1.0", "unraid": "7.1.5"}}}}
                },
            )
        )
        mcp = build_server(settings_factory())
        async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
            assert ctx.api_version == "7.1.0"
            assert ctx.unraid_version == "7.1.5"


async def test_second_concurrent_lifespan_fails_loud(settings_factory):
    """The resource-context holder admits one lifespan at a time.

    A second concurrent lifespan on the same server would make resource reads
    silently use the wrong httpx client; the server must refuse it instead.
    """
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        mcp = build_server(settings_factory())
        server = mcp._lowlevel_server
        async with server.lifespan(server):
            with pytest.raises(RuntimeError, match="lifespan entered twice"):
                async with server.lifespan(server):
                    pass  # pragma: no cover - must not be reached


async def test_unraid_http_client_ignores_proxy_environment(settings_factory, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:8080")
    mcp = build_server(settings_factory())
    async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
        assert ctx.client._http.trust_env is False


async def test_list_responses_carry_long_cache_hints(settings_factory):
    """tools/list, prompts/list, resources/list are static per process; the
    2026-07-28 cache hints (SEP-2549) should advertise the long TTL so
    clients/gateways skip redundant round-trips."""
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=True) as session:
            tools = await session.list_tools()
            prompts = await session.list_prompts()
            resources_result = await session.list_resources()

    for result in (tools, prompts, resources_result):
        assert result.ttl_ms == 60 * 60 * 1000
        assert result.cache_scope == "public"


async def test_resource_read_carries_short_cache_hint(settings_factory):
    """resources/read serves point-in-time health/system-info snapshots, so
    its cache hint TTL must stay short (seconds, not the list TTL)."""
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "tower"}}}})
        )
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=True) as session:
            listed = await session.list_resources()
            uri = str(listed.resources[0].uri)
            result = await session.read_resource(uri)

    assert result.ttl_ms == 10_000
    assert result.cache_scope == "private"
