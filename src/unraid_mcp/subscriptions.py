"""MCP-free ``graphql-transport-ws`` subscription sampler.

Subscriptions cannot reuse the lifespan's HTTP-only ``httpx.AsyncClient`` — each
sample opens a **fresh, short-lived** websocket, runs the graphql-transport-ws
handshake (``connection_init`` → ``connection_ack`` → ``subscribe`` → ``next``* →
``complete``), collects one payload per key until a caller predicate is satisfied
or a deadline elapses, then unsubscribes and closes.

The protocol state machine (:func:`sample_subscription`) operates on an injected
:class:`WSTransport`, so it is fully unit-testable without a live server — the
production transport is a thin adapter over ``websockets`` (:func:`open_ws`).

Secrets: the API key travels **only** inside the ``connection_init`` payload. It is
never placed in the handshake URL/headers and never appears in any raised error
message or log line (server-supplied error text is redacted defensively).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from .errors import UnraidAuthError, UnraidConnectionError, UnraidGraphQLError
from .logging import get_logger

log = get_logger(__name__)

SUBPROTOCOL = "graphql-transport-ws"
_SUB_ID = "1"
# graphql-transport-ws close codes that mean "auth rejected" (vs. a generic drop).
_AUTH_CLOSE_CODES = {4401, 4403}


class WSTransport(Protocol):
    """The minimal async websocket surface the sampler needs.

    ``recv`` must raise :class:`WSClosed` (never hang) once the peer has closed.
    """

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...


class WSClosed(Exception):
    """A transport's ``recv``/``send`` observed the connection closed.

    ``code`` is the websocket close code when known (used to distinguish an
    auth rejection from an ordinary drop). ``reason`` text is never trusted into
    a user-facing message without redaction.
    """

    def __init__(self, message: str = "", *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _redact(text: str, api_key: str) -> str:
    """Defensively scrub the API key from any server-echoed text."""
    return text.replace(api_key, "***REDACTED***") if api_key and api_key in text else text


async def sample_subscription(
    transport: WSTransport,
    *,
    api_key: str,
    query: str,
    deadline_s: float,
    key: Callable[[dict[str, Any]], str | None],
    is_complete: Callable[[dict[str, dict[str, Any]], bool], bool],
) -> tuple[list[dict[str, Any]], bool]:
    """Drive one graphql-transport-ws sample and return ``(payloads, deadline_hit)``.

    Every ``recv`` is bounded by the overall ``deadline_s`` (wall clock), so this
    never hangs. ``next`` payloads (the GraphQL ``data`` object) are deduped into an
    insertion-ordered dict keyed by ``key(data)``; ``is_complete(collected, was_new)``
    decides when a full cycle has been captured. Returns the collected payloads and
    whether collection stopped because the deadline was hit (a partial result).

    Raises (all secret-free):

    * :class:`UnraidAuthError` — server rejected ``connection_init`` (auth close code
      or an error before ack);
    * :class:`UnraidConnectionError` — no ``connection_ack`` within the deadline, an
      unexpected pre-ack frame, or the socket closed before any payload arrived;
    * :class:`UnraidGraphQLError` — the subscription emitted an ``error`` frame (used
      upstream to detect an unsupported field on old API builds).
    """
    deadline_ts = time.monotonic() + deadline_s

    async def _recv() -> dict[str, Any]:
        remaining = deadline_ts - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        raw = await asyncio.wait_for(transport.recv(), timeout=remaining)
        return json.loads(raw)

    # 1. connection_init — the ONLY place the API key is sent.
    await transport.send(json.dumps({"type": "connection_init", "payload": {"x-api-key": api_key}}))
    try:
        ack = await _recv()
    except TimeoutError:
        raise UnraidConnectionError(
            f"No connection_ack from the Unraid stats subscription within {deadline_s:.0f}s."
        ) from None
    except WSClosed as exc:
        if exc.code in _AUTH_CLOSE_CODES:
            raise UnraidAuthError(
                "Websocket subscription auth failed. Check UNRAID_API_KEY and its roles."
            ) from None
        raise UnraidConnectionError(
            "Unraid closed the stats subscription before acknowledging the connection."
        ) from None

    ack_type = ack.get("type")
    if ack_type in ("connection_error", "error"):
        raise UnraidAuthError(
            "Websocket subscription auth failed. Check UNRAID_API_KEY and its roles."
        )
    if ack_type != "connection_ack":
        raise UnraidConnectionError(
            f"Unexpected first frame from the stats subscription (type={ack_type!r})."
        )

    # 2. subscribe.
    await transport.send(
        json.dumps({"id": _SUB_ID, "type": "subscribe", "payload": {"query": query}})
    )

    collected: dict[str, dict[str, Any]] = {}
    deadline_hit = False
    try:
        while True:
            try:
                msg = await _recv()
            except TimeoutError:
                deadline_hit = True
                break
            except WSClosed:
                if collected:
                    deadline_hit = True
                    break
                raise UnraidConnectionError(
                    "Unraid closed the stats subscription before sending any data."
                ) from None

            mtype = msg.get("type")
            if mtype == "next":
                data = (msg.get("payload") or {}).get("data") or {}
                k = key(data)
                was_new = k is not None and k not in collected
                if was_new:
                    # Keep the first reading per key; a later repeat (the next
                    # cycle starting) signals completeness but must not overwrite it.
                    collected[k] = data
                if is_complete(collected, was_new):
                    break
            elif mtype == "error":
                payload = msg.get("payload")
                errors = payload if isinstance(payload, list) else [{"message": str(payload)}]
                messages = "; ".join(
                    _redact(str(e.get("message", "unknown error")), api_key) for e in errors
                )
                raise UnraidGraphQLError(f"Subscription error: {messages}", errors=errors)
            elif mtype == "complete":
                break
            elif mtype == "ping":
                await transport.send(json.dumps({"type": "pong"}))
            # connection_ack duplicates / unknown frames are ignored.
    finally:
        # Best-effort unsubscribe; the socket is closed by the caller's context.
        with contextlib.suppress(WSClosed, OSError):
            await transport.send(json.dumps({"id": _SUB_ID, "type": "complete"}))

    log.debug("subscription sample: %d payload(s), deadline_hit=%s", len(collected), deadline_hit)
    return list(collected.values()), deadline_hit


class _WebsocketsTransport:
    """Adapter wrapping a live ``websockets`` connection as a :class:`WSTransport`."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws

    async def send(self, message: str) -> None:
        import websockets

        try:
            await self._ws.send(message)
        except websockets.ConnectionClosed as exc:
            raise WSClosed(code=getattr(exc, "code", None)) from exc

    async def recv(self) -> str:
        import websockets

        try:
            return await self._ws.recv()
        except websockets.ConnectionClosed as exc:
            raise WSClosed(code=getattr(exc, "code", None)) from exc


@asynccontextmanager
async def open_ws(
    ws_url: str,
    ssl_context: ssl.SSLContext | None,
    *,
    open_timeout: float,
) -> AsyncIterator[WSTransport]:
    """Open a short-lived ``graphql-transport-ws`` connection (production transport).

    Mirrors the HTTP client's TLS/proxy discipline: the caller-built ``ssl_context``
    honors ``UNRAID_VERIFY_SSL`` / ``UNRAID_CA_BUNDLE``, and proxy env vars are ignored
    (``websockets`` is told not to consult them). The API key is NOT sent on the
    handshake — only in ``connection_init`` — so a handshake error cannot leak it.
    """
    import websockets
    from websockets.asyncio.client import connect

    try:
        async with connect(
            ws_url,
            subprotocols=[SUBPROTOCOL],  # type: ignore[list-item]
            ssl=ssl_context,
            open_timeout=open_timeout,
            proxy=None,  # parity with httpx trust_env=False: ignore proxy env vars
        ) as ws:
            yield _WebsocketsTransport(ws)
    except (OSError, websockets.WebSocketException) as exc:
        # Never include ``exc`` text verbatim — keep the message static and secret-free.
        raise UnraidConnectionError(
            f"Could not open a stats websocket to {_host(ws_url)}. "
            "Check UNRAID_API_URL, the network, and TLS settings."
        ) from exc


def _host(ws_url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(ws_url).netloc or ws_url
