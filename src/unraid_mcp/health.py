"""Unauthenticated liveness endpoint for the streamable-HTTP transport.

Wraps in front of ``StaticBearerAuthMiddleware`` so ``GET /health`` never
reaches the bearer gate (and therefore never reaches the Unraid API): it is
liveness-only, answering "the process is up" and nothing more.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]

_BODY = b'{"status":"ok"}'


class HealthCheckMiddleware:
    """Serve a static 200 for ``GET /health``; delegate everything else."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive, send) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/health"
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_BODY)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _BODY})
            return
        await self._app(scope, receive, send)
