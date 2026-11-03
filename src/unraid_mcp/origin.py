"""Origin validation for the streamable-HTTP transport.

The MCP spec requires servers to answer 403 for an invalid ``Origin``. The SDK
only checks it together with ``Host`` (DNS-rebinding protection), which we
leave off on non-localhost binds without ``UNRAID_MCP_ALLOWED_HOSTS`` because
the real hostname is unknown. ``Origin`` needs no hostname, so this pure-ASGI
middleware enforces it on every bind, independently of ``Host``.

Requests without an ``Origin`` header (non-browser clients) pass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]

_BODY = b'{"error":"forbidden","detail":"invalid Origin header"}'


class OriginValidationMiddleware:
    """Reject HTTP requests whose ``Origin`` header is not on the allow-list."""

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self._app = app
        self._allowed = frozenset(allowed_origins)
        # "scheme://host:*" matches any port, mirroring the SDK's pattern.
        self._wildcards = tuple(o[:-2] + ":" for o in self._allowed if o.endswith(":*"))

    def _origin_ok(self, origin: str) -> bool:
        return origin in self._allowed or any(origin.startswith(w) for w in self._wildcards)

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") == "http":
            values = [v for k, v in (scope.get("headers") or []) if k.lower() == b"origin"]
            # Duplicate headers are rejected: no proxy can validate one yet pass another.
            if values and (len(values) > 1 or not self._origin_ok(values[0].decode("latin-1"))):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(_BODY)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": _BODY})
                return
        await self._app(scope, receive, send)
