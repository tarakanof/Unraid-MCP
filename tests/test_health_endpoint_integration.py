"""Integration tests for the composed streamable-HTTP ASGI stack.

Exercises the real app returned by ``cli._build_http_app`` (health check in
front of the bearer gate in front of the actual MCP app) over an in-process
ASGI transport, with respx mocking the upstream Unraid GraphQL endpoint.
"""

from __future__ import annotations

import httpx
import respx

from unraid_mcp import cli
from unraid_mcp.server import build_server

URL = "https://tower.local/graphql"
TOKEN = "tok-abcdef123456"


async def test_health_returns_200_without_auth_and_makes_no_upstream_call(settings_factory):
    with respx.mock:
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "t"}}}})
        )
        mcp = build_server(settings_factory())
        app = cli._build_http_app(mcp, TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
        assert route.called is False


async def test_other_paths_still_401_without_bearer_token(settings_factory):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        mcp = build_server(settings_factory())
        app = cli._build_http_app(mcp, TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/mcp")
        assert resp.status_code == 401


async def test_health_path_ignores_bearer_token_requirement_even_with_bad_header(settings_factory):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        mcp = build_server(settings_factory())
        app = cli._build_http_app(mcp, TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
