"""End-to-end test through the MCP protocol with a mocked Unraid backend.

Uses the SDK in-memory client/server pair. respx intercepts the httpx client
created inside the server lifespan, so this exercises the full path:
session → tool → client → (mocked) GraphQL → shaped result.
"""

from __future__ import annotations

import httpx
import respx
from mcp.shared.memory import create_connected_server_and_client_session

from unraid_mcp.server import build_server

URL = "https://tower.local/graphql"


async def test_list_and_call_through_protocol(settings_factory):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "tower"}}}})
        )
        mcp = build_server(settings_factory(allow_mutations=False))
        async with create_connected_server_and_client_session(
            mcp, raise_exceptions=True
        ) as session:
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "get_system_info" in tools
            assert "get_health_summary" in tools
            assert "stop_array" not in tools  # mutations disabled by default

            result = await session.call_tool("get_system_info", {})
            assert result.isError is False
            assert result.structuredContent["os"]["hostname"] == "tower"


async def test_mutations_callable_when_enabled(settings_factory):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"data": {"array": {"setState": {"state": "STARTED"}}}}
            )
        )
        mcp = build_server(settings_factory(allow_mutations=True))
        async with create_connected_server_and_client_session(
            mcp, raise_exceptions=True
        ) as session:
            tools = {t.name for t in (await session.list_tools()).tools}
            assert "start_array" in tools
            # Confirm-gated stop_array refuses without confirm and makes no change.
            refused = await session.call_tool("stop_array", {"confirm": False})
            assert refused.isError is True
