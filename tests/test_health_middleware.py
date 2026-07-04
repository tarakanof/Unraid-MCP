"""Tests for the unauthenticated /health liveness middleware (pure ASGI)."""

from __future__ import annotations

import json

from unraid_mcp.health import HealthCheckMiddleware


def _inner_factory():
    state = {"called": False}

    async def inner(scope, receive, send):
        state["called"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    return inner, state


async def _call(mw, method="GET", path="/health", scope_type="http"):
    scope = {"type": scope_type, "headers": [], "method": method, "path": path}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    await mw(scope, receive, send)
    return sent


async def test_health_returns_200_without_calling_inner_app():
    inner, state = _inner_factory()
    mw = HealthCheckMiddleware(inner)
    sent = await _call(mw)
    assert state["called"] is False
    assert sent[0]["status"] == 200
    assert json.loads(sent[1]["body"]) == {"status": "ok"}


async def test_health_response_has_no_extra_fields():
    inner, _ = _inner_factory()
    mw = HealthCheckMiddleware(inner)
    sent = await _call(mw)
    assert set(json.loads(sent[1]["body"]).keys()) == {"status"}


async def test_post_to_health_falls_through_to_inner_app():
    inner, state = _inner_factory()
    mw = HealthCheckMiddleware(inner)
    await _call(mw, method="POST")
    assert state["called"] is True


async def test_other_paths_fall_through_to_inner_app():
    inner, state = _inner_factory()
    mw = HealthCheckMiddleware(inner)
    await _call(mw, path="/mcp")
    assert state["called"] is True


async def test_lifespan_scope_falls_through_to_inner_app():
    inner, state = _inner_factory()
    mw = HealthCheckMiddleware(inner)
    await _call(mw, scope_type="lifespan")
    assert state["called"] is True
