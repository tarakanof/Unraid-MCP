"""Integration tests for the composed streamable-HTTP ASGI stack.

Exercises the real app returned by ``cli._build_http_app`` (health check in
front of the bearer gate in front of the actual MCP app) over an in-process
ASGI transport, with respx mocking the upstream Unraid GraphQL endpoint.
"""

from __future__ import annotations

import asyncio

import httpx
import respx

from unraid_mcp import cli
from unraid_mcp.server import build_server

URL = "https://tower.local/graphql"
TOKEN = "tok-abcdef123456"

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def test_health_returns_200_without_auth_and_makes_no_upstream_call(settings_factory):
    with respx.mock:
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "t"}}}})
        )
        settings = settings_factory()
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert route.called is False


async def test_other_paths_still_401_without_bearer_token(settings_factory):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        settings = settings_factory()
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/mcp")
        assert resp.status_code == 401


async def test_lifespan_scope_passes_through_middlewares_and_starts_session_manager(
    settings_factory,
):
    """The health and bearer middlewares must forward ``lifespan`` scopes.

    ``httpx.ASGITransport`` never runs the ASGI lifespan, so the other tests
    here would stay green even if a middleware swallowed it — which in
    production means the session manager never starts and every /mcp request
    fails. Drive the lifespan by hand and prove the MCP app serves an
    ``initialize`` while it is open.
    """
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        settings = settings_factory(transport="streamable-http", port=6799)
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)

        to_app: asyncio.Queue = asyncio.Queue()
        from_app: asyncio.Queue = asyncio.Queue()

        async def receive():
            return await to_app.get()

        async def send(message):
            await from_app.put(message)

        lifespan_task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
        await to_app.put({"type": "lifespan.startup"})
        started = await asyncio.wait_for(from_app.get(), timeout=10)
        assert started["type"] == "lifespan.startup.complete"

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:6799"
            ) as client:
                resp = await client.post(
                    "/mcp",
                    json=INITIALIZE,
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Accept": "application/json, text/event-stream",
                    },
                )
            assert resp.status_code == 200
        finally:
            await to_app.put({"type": "lifespan.shutdown"})
            stopped = await asyncio.wait_for(from_app.get(), timeout=10)
            assert stopped["type"] == "lifespan.shutdown.complete"
            await asyncio.wait_for(lifespan_task, timeout=10)


async def test_health_path_ignores_bearer_token_requirement_even_with_bad_header(settings_factory):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        settings = settings_factory()
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
