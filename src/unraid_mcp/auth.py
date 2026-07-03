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
        if not token:
            raise ValueError("StaticBearerAuthMiddleware requires a non-empty token")
        self._app = app
        self._expected: bytes = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive, send) -> None:
        scope_type = scope.get("type")
        # The lifespan protocol carries no client request to authenticate.
        if scope_type == "lifespan":
            await self._app(scope, receive, send)
            return
        # Websockets are never authenticated here; refuse them with a proper
        # close frame (sending HTTP frames on a websocket violates ASGI).
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        # Authenticate HTTP; anything else falls through to the 401 below
        # rather than slipping past the gate.
        if scope_type == "http":
            # Collect every Authorization header: 0 or >1 is rejected, so a
            # smuggled duplicate can't be validated by a proxy yet bypass us.
            values = [v for k, v in (scope.get("headers") or []) if k.lower() == b"authorization"]
            provided = values[0] if len(values) == 1 else b""
            # Constant-time comparison on bytes to avoid leaking the token via
            # timing; bytes never raise for non-ASCII/invalid-UTF-8 content,
            # unlike str-vs-str hmac.compare_digest.
            if hmac.compare_digest(provided, self._expected):
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
