"""Origin validation for the streamable-HTTP transport.

The MCP spec requires servers to answer 403 for an invalid ``Origin``. The SDK
only checks it together with ``Host`` (DNS-rebinding protection), which we
leave off on non-localhost binds without ``UNRAID_MCP_ALLOWED_HOSTS`` because
the real hostname is unknown. ``Origin`` needs no hostname, so this pure-ASGI
middleware enforces it on every bind, independently of ``Host``.

Requests without an ``Origin`` header (non-browser clients), or with an empty
one (as the SDK treats it), pass. It runs before the bearer gate on purpose:
a hostile page is refused before any token or MCP handling is involved.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

from .logging import get_logger

ASGIApp = Callable[
    [dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]
]

log = get_logger(__name__)

_BODY = b'{"error":"forbidden","detail":"invalid Origin header"}'


class OriginValidationMiddleware:
    """Reject requests whose ``Origin`` header is not on the allow-list."""

    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self._app = app
        self._allowed = frozenset(allowed_origins)
        # "scheme://host:*" matches any port, mirroring the SDK's pattern.
        self._wildcards = tuple(o[:-2] + ":" for o in self._allowed if o.endswith(":*"))

    def _origin_ok(self, origin: str) -> bool:
        return origin in self._allowed or any(origin.startswith(w) for w in self._wildcards)

    def _rejected(self, scope: dict) -> bool:
        values = [v for k, v in (scope.get("headers") or []) if k.lower() == b"origin"]
        values = [v for v in values if v]  # empty value == absent, like the SDK
        if not values:
            return False
        # Duplicate headers are rejected: no proxy can validate one yet pass another.
        if len(values) == 1 and self._origin_ok(values[0].decode("latin-1")):
            return False
        # The value is attacker-controlled: escape and truncate before logging.
        shown = repr(values[0].decode("latin-1"))[:120]
        log.warning(
            "Rejected request with Origin %s (%d header(s)); add it to "
            "UNRAID_MCP_ALLOWED_ORIGINS if it is a legitimate browser client.",
            shown,
            len(values),
        )
        return True

    async def __call__(self, scope: dict, receive, send) -> None:
        scope_type = scope.get("type")
        if scope_type in ("http", "websocket") and self._rejected(scope):
            if scope_type == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
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
