"""Minimal bearer-token gate for the streamable-HTTP transport.

The stdio transport (the default) has no network surface and needs no auth.
When serving over HTTP we require a static bearer token from clients. This is
deliberately simple — a pure-ASGI middleware comparing one shared secret — and
is unit-testable without a running server. For exposure beyond localhost, run
behind a reverse proxy that terminates TLS.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]


class StaticBearerAuthMiddleware:
    """Reject HTTP requests whose ``Authorization`` header isn't ``Bearer <token>``."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self._token}"
        # Constant-time comparison to avoid leaking the token via timing.
        if provided and hmac.compare_digest(provided, expected):
            await self._app(scope, receive, send)
            return

        body = b'{"error":"unauthorized","detail":"missing or invalid bearer token"}'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b'Bearer realm="unraid-mcp"'),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
