"""Unit tests for OriginValidationMiddleware and the Origin allow-list parsing."""

from __future__ import annotations

import httpx
import respx

from unraid_mcp import cli
from unraid_mcp.origin import OriginValidationMiddleware
from unraid_mcp.server import build_server

from .conftest import make_settings
from .test_health_endpoint_integration import _AUTH, INITIALIZE, TOKEN, _drive_lifespan


async def _run(app, scope):
    sent: list[dict] = []
    reached: list[bool] = []

    async def inner(scope, receive, send):
        reached.append(True)

    mw = OriginValidationMiddleware(inner, app)

    async def send(message):
        sent.append(message)

    await mw(scope, None, send)
    return sent, bool(reached)


def _http(*origins: bytes) -> dict:
    return {"type": "http", "headers": [(b"origin", o) for o in origins]}


async def test_absent_and_empty_origin_pass():
    for scope in ({"type": "http", "headers": []}, _http(b"")):
        sent, reached = await _run(["https://ui.example"], scope)
        assert reached and not sent


async def test_null_origin_rejected_unless_listed():
    sent, reached = await _run(["https://ui.example"], _http(b"null"))
    assert not reached and sent[0]["status"] == 403
    sent, reached = await _run(["null"], _http(b"null"))
    assert reached


async def test_wildcard_matches_port_not_other_host():
    allowed = ["https://a.example:*"]
    assert (await _run(allowed, _http(b"https://a.example:8443")))[1]
    assert not (await _run(allowed, _http(b"https://a.example.evil")))[1]
    assert not (await _run(allowed, _http(b"https://a.example")))[1]


async def test_websocket_bad_origin_closes_1008_and_lifespan_passes():
    sent, reached = await _run(
        ["https://ui.example"], {"type": "websocket", "headers": [(b"origin", b"https://x")]}
    )
    assert not reached and sent == [{"type": "websocket.close", "code": 1008}]
    sent, reached = await _run(["https://ui.example"], {"type": "lifespan"})
    assert reached


async def test_rejection_is_logged_escaped_and_without_secrets(caplog):
    with caplog.at_level("WARNING"):
        await _run(["https://ui.example"], _http(b"https://evil.example\nforged"))
    assert "https://evil.example" in caplog.text
    assert "\nforged" not in caplog.text


def test_allowed_origins_are_normalised():
    s = make_settings(allowed_origins=" https://UI.example/ , https://b.example:8443 ")
    assert s.http_allowed_origins() == ["https://ui.example", "https://b.example:8443"]


def test_separator_only_value_falls_back_to_defaults():
    origins = make_settings(allowed_origins=" , ", port=6750).http_allowed_origins()
    assert "https://localhost:6750" in origins
    assert "http://[::1]:6750" in origins


async def test_bad_origin_rejected_on_allowed_hosts_path(settings_factory):
    with respx.mock:
        settings = settings_factory(
            transport="streamable-http",
            host="0.0.0.0",
            port=6750,
            allowed_hosts="tower.example:6750",
            allowed_origins="https://ui.example",
        )
        app = cli._build_http_app(build_server(settings), settings, TOKEN)
        stop = await _drive_lifespan(app)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
                host = {"host": "tower.example:6750"}
                bad = await client.post(
                    "/mcp",
                    headers={**_AUTH, **host, "origin": "https://evil.example"},
                    json=INITIALIZE,
                )
                ok = await client.post(
                    "/mcp",
                    headers={**_AUTH, **host, "origin": "https://ui.example"},
                    json=INITIALIZE,
                )
                bad_host = await client.post(
                    "/mcp", headers={**_AUTH, "host": "evil.example:1"}, json=INITIALIZE
                )
        finally:
            await stop()
    assert bad.status_code == 403
    assert ok.status_code == 200
    assert bad_host.status_code == 421  # SDK Host check unchanged


async def test_sdk_origin_check_still_fires_without_our_middleware(settings_factory):
    """The SDK's own Origin check (localhost bind) is untouched by the new middleware."""
    from unraid_mcp.server import http_app

    with respx.mock:
        settings = settings_factory(transport="streamable-http", port=6750)
        sdk_app = http_app(build_server(settings), settings)
        stop = await _drive_lifespan(sdk_app)
        try:
            transport = httpx.ASGITransport(app=sdk_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://x") as client:
                resp = await client.post(
                    "/mcp",
                    headers={**_AUTH, "host": "localhost:6750", "origin": "https://evil.example"},
                    json=INITIALIZE,
                )
        finally:
            await stop()
    assert resp.status_code == 403
