"""Integration tests for the composed streamable-HTTP ASGI stack.

Exercises the real app returned by ``cli._build_http_app`` (health check in
front of the bearer gate in front of the actual MCP app) over an in-process
ASGI transport, with respx mocking the upstream Unraid GraphQL endpoint.
"""

from __future__ import annotations

import asyncio
import json

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


async def _drive_lifespan(app):
    """Start ``app``'s ASGI lifespan by hand; return (stop, queues) to shut it down.

    Mirrors ``test_lifespan_scope_passes_through_middlewares_and_starts_session_manager``:
    ``httpx.ASGITransport`` never runs ``lifespan``, so tests that need the
    session manager's ``run()`` context open (stateless mode still enters it
    exactly once, see ``streamable_http_manager.py:145-149``) must drive it
    themselves.
    """
    to_app: asyncio.Queue = asyncio.Queue()
    from_app: asyncio.Queue = asyncio.Queue()

    async def receive():
        return await to_app.get()

    async def send(message):
        await from_app.put(message)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await to_app.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(from_app.get(), timeout=10)
    assert started["type"] == "lifespan.startup.complete"

    async def stop():
        await to_app.put({"type": "lifespan.shutdown"})
        stopped = await asyncio.wait_for(from_app.get(), timeout=10)
        assert stopped["type"] == "lifespan.shutdown.complete"
        await asyncio.wait_for(task, timeout=10)

    return stop


def _rpc_body(resp: httpx.Response) -> dict:
    """Decode a streamable-HTTP JSON-RPC response, JSON or SSE alike.

    Stateless mode (``json_response`` left at its default ``False``) still
    answers each single-exchange request over ``text/event-stream``; pull the
    ``data:`` line's JSON payload out of the SSE frame either way.
    """
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise AssertionError(f"no data: line in SSE body: {resp.text!r}")
    return resp.json()


TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
TOOLS_CALL = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "get_system_info", "arguments": {}},
}


async def test_stateless_serves_tools_list_and_tools_call_without_any_session(settings_factory):
    """Two consecutive requests succeed with no ``initialize`` and no session ID.

    Proves the MCP spec 2026-07-28 stateless model end to end: ``tools/list``
    then ``tools/call`` both land on brand-new, unrelated transports
    (``streamable_http_manager.py::_handle_stateless_request`` builds a fresh
    ``StreamableHTTPServerTransport`` with ``mcp_session_id=None`` per
    request), yet each succeeds immediately because
    ``Connection.from_envelope`` is always born pre-initialized
    (``connection.py:296,333-337``).
    """
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "tower"}}}})
        )
        settings = settings_factory(transport="streamable-http", port=6800)
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)
        stop = await _drive_lifespan(app)
        try:
            transport = httpx.ASGITransport(app=app)
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            }
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:6800"
            ) as client:
                first = await client.post("/mcp", json=TOOLS_LIST, headers=headers)
                assert first.status_code == 200
                assert "mcp-session-id" not in first.headers
                names = {t["name"] for t in _rpc_body(first)["result"]["tools"]}
                assert "get_system_info" in names

                second = await client.post("/mcp", json=TOOLS_CALL, headers=headers)
                assert second.status_code == 200
                assert "mcp-session-id" not in second.headers
                # Tool failures come back as HTTP 200 with result.isError=true,
                # not a JSON-RPC error member — assert the actual payload.
                result = _rpc_body(second)["result"]
                assert result.get("isError") is not True
                assert result["structuredContent"]["os"]["hostname"] == "tower"
        finally:
            await stop()


async def test_stateless_legacy_initialize_flow_still_works(settings_factory):
    """A pre-2026 client that still calls ``initialize`` first is served too.

    ``initialize`` is dispatched inline in stateless mode
    (``streamable_http_manager.py:216``, ``runner.py:201-202``), and no
    session ID is ever handed back (``mcp_session_id=None``), so the client's
    follow-up ``tools/list`` — sent with no ``Mcp-Session-Id`` header, since
    none was ever issued — must still succeed on its own fresh transport.
    """
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        settings = settings_factory(transport="streamable-http", port=6801)
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)
        stop = await _drive_lifespan(app)
        try:
            transport = httpx.ASGITransport(app=app)
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            }
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:6801"
            ) as client:
                init_resp = await client.post("/mcp", json=INITIALIZE, headers=headers)
                assert init_resp.status_code == 200
                assert _rpc_body(init_resp)["result"]["protocolVersion"] == "2025-11-25"
                assert "mcp-session-id" not in init_resp.headers

                list_resp = await client.post("/mcp", json=TOOLS_LIST, headers=headers)
                assert list_resp.status_code == 200
                names = {t["name"] for t in _rpc_body(list_resp)["result"]["tools"]}
                assert "get_health_summary" in names
        finally:
            await stop()


async def test_stateless_survives_a_full_server_restart_between_requests(settings_factory):
    """A second, wholly separate app/lifespan instance serves the next request.

    Simulates a process restart: no state built for the first app (or its
    lifespan) is reused for the second, and both requests still succeed —
    the property that makes the stateless HTTP deployment restart-safe.
    """
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        settings = settings_factory(transport="streamable-http", port=6802)
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        }

        first_mcp = build_server(settings)
        first_app = cli._build_http_app(first_mcp, settings, TOKEN)
        stop_first = await _drive_lifespan(first_app)
        try:
            transport = httpx.ASGITransport(app=first_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:6802"
            ) as client:
                resp = await client.post("/mcp", json=TOOLS_LIST, headers=headers)
                assert resp.status_code == 200
        finally:
            await stop_first()  # process "restarts" here

        second_mcp = build_server(settings)
        second_app = cli._build_http_app(second_mcp, settings, TOKEN)
        stop_second = await _drive_lifespan(second_app)
        try:
            transport = httpx.ASGITransport(app=second_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:6802"
            ) as client:
                resp = await client.post("/mcp", json=TOOLS_LIST, headers=headers)
                assert resp.status_code == 200
                names = {t["name"] for t in _rpc_body(resp)["result"]["tools"]}
                assert "get_system_info" in names
        finally:
            await stop_second()


async def test_mcp_method_and_name_headers_pass_through_middlewares_untouched(settings_factory):
    """The 2026-07-28 per-request envelope's routing headers survive the stack.

    ``Mcp-Method``/``Mcp-Name`` (and ``MCP-Protocol-Version``) must reach the
    SDK's own header/body consistency check
    (``mcp/shared/inbound.py::classify_inbound_request``, rung 2) unmodified.
    ``HealthCheckMiddleware`` and ``StaticBearerAuthMiddleware`` are read
    confirmed pure pass-through ASGI apps for any non-``/health`` HTTP scope
    (they forward ``scope`` — headers included — verbatim); this drives an
    actual modern per-request call through the full composed stack as the
    end-to-end proof: a middleware that mangled a header would surface as a
    ``HEADER_MISMATCH`` JSON-RPC error here instead of a clean result.
    """
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"info": {"os": {"hostname": "tower"}}}})
        )
        settings = settings_factory(transport="streamable-http", port=6803)
        mcp = build_server(settings)
        app = cli._build_http_app(mcp, settings, TOKEN)
        stop = await _drive_lifespan(app)
        try:
            transport = httpx.ASGITransport(app=app)
            call_body = {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "get_system_info",
                    "arguments": {},
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    },
                },
            }
            headers = {
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "get_system_info",
            }
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:6803"
            ) as client:
                resp = await client.post("/mcp", json=call_body, headers=headers)
                assert resp.status_code == 200
                body = _rpc_body(resp)
                assert "error" not in body
                assert body["result"]["structuredContent"]["os"]["hostname"] == "tower"
        finally:
            await stop()


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
