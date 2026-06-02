"""Tests for the streamable-HTTP bearer-auth middleware (pure ASGI)."""

from __future__ import annotations

from unraid_mcp.auth import StaticBearerAuthMiddleware

TOKEN = "tok-abcdef123456"


def _inner_factory():
    state = {"called": False}

    async def inner(scope, receive, send):
        state["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return inner, state


async def _call(mw, auth_header=None, scope_type="http"):
    headers = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode()))
    scope = {"type": scope_type, "headers": headers, "method": "POST", "path": "/mcp"}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


async def test_valid_token_passes_through():
    inner, state = _inner_factory()
    mw = StaticBearerAuthMiddleware(inner, TOKEN)
    sent = await _call(mw, f"Bearer {TOKEN}")
    assert state["called"] is True
    assert sent[0]["status"] == 200


async def test_missing_token_rejected():
    inner, state = _inner_factory()
    mw = StaticBearerAuthMiddleware(inner, TOKEN)
    sent = await _call(mw, None)
    assert state["called"] is False
    assert sent[0]["status"] == 401


async def test_wrong_token_rejected():
    inner, state = _inner_factory()
    mw = StaticBearerAuthMiddleware(inner, TOKEN)
    sent = await _call(mw, "Bearer not-the-token")
    assert state["called"] is False
    assert sent[0]["status"] == 401


async def test_non_http_scope_passes_through():
    inner, state = _inner_factory()
    mw = StaticBearerAuthMiddleware(inner, TOKEN)
    await _call(mw, scope_type="lifespan")
    assert state["called"] is True
