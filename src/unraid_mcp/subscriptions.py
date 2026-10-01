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
import json
import ssl
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol

from .errors import UnraidAuthError, UnraidConnectionError, UnraidGraphQLError
from .logging import get_logger, redact

log = get_logger(__name__)

SUBPROTOCOL = "graphql-transport-ws"
_SUB_ID = "1"
CLEANUP_GRACE_S = 2.0
# graphql-transport-ws close codes that mean "auth rejected" (vs. a generic drop).
_AUTH_CLOSE_CODES = {4401, 4403}


class WSTransport(Protocol):
    """The minimal async websocket surface the sampler needs.

    ``recv`` must raise :class:`WSClosed` (never hang) once the peer has closed.
    """

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


class WSClosed(Exception):
    """A transport's ``recv``/``send`` observed the connection closed.

    ``code`` is the websocket close code when known (used to distinguish an
    auth rejection from an ordinary drop). ``reason`` text is never trusted into
    a user-facing message without redaction.
    """

    def __init__(self, message: str = "", *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


async def sample_subscription(
    transport: WSTransport,
    *,
    api_key: str,
    bearer_token: str | None = None,
    query: str,
    deadline_s: float,
    deadline_ts: float | None = None,
    key: Callable[[dict[str, Any]], str | None],
    is_complete: Callable[[dict[str, dict[str, Any]], bool], bool],
) -> tuple[list[dict[str, Any]], bool]:
    """Drive one graphql-transport-ws sample and return ``(payloads, deadline_hit)``.

    Every send and receive shares ``deadline_s``. Unsubscribe and close share a
    fixed cleanup grace budget, including when the sample deadline is spent.
    ``deadline_ts`` lets the caller include connection setup in the same deadline.
    ``next`` payloads (the GraphQL ``data`` object) are deduped into an
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
    secrets = (api_key, bearer_token)
    loop = asyncio.get_running_loop()
    deadline_ts = (
        min(deadline_ts, loop.time() + deadline_s)
        if deadline_ts is not None
        else (loop.time() + deadline_s)
    )
    operation_deadline = deadline_ts + CLEANUP_GRACE_S
    subscribed = False

    async def _send(message: str) -> None:
        await asyncio.wait_for(transport.send(message), timeout=max(0, deadline_ts - loop.time()))

    async def _recv() -> dict[str, Any]:
        raw = await asyncio.wait_for(transport.recv(), timeout=max(0, deadline_ts - loop.time()))
        try:
            return redact(json.loads(raw), secrets)
        except ValueError:
            raise UnraidConnectionError("Unraid sent an invalid JSON subscription frame.") from None

    async def _sample() -> tuple[list[dict[str, Any]], bool]:
        nonlocal subscribed

        # 1. connection_init — the ONLY place the API key is sent.
        try:
            await _send(json.dumps({"type": "connection_init", "payload": {"x-api-key": api_key}}))
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
        subscribed = True
        await _send(json.dumps({"id": _SUB_ID, "type": "subscribe", "payload": {"query": query}}))

        collected: dict[str, dict[str, Any]] = {}
        deadline_hit = False
        try:
            while True:
                try:
                    msg = await _recv()
                    if msg.get("type") == "ping":
                        await _send(json.dumps({"type": "pong"}))
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
                    if k is None:
                        # Keyless frame: neither a new reading nor a cycle-repeat
                        # signal. It must not reach is_complete, where was_new=False
                        # would masquerade as a repeat and truncate the sample.
                        continue
                    was_new = k not in collected
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
                        redact(str(e.get("message", "unknown error")), secrets) for e in errors
                    )
                    raise UnraidGraphQLError(f"Subscription error: {messages}", errors=errors)
                elif mtype == "complete":
                    break
                # ping handled above; connection_ack duplicates / unknown frames are ignored.
        except TimeoutError:
            # A blocked pong consumes the same sampling window as a blocked recv.
            deadline_hit = True

        log.debug(
            "subscription sample: %d payload(s), deadline_hit=%s", len(collected), deadline_hit
        )
        return list(collected.values()), deadline_hit

    async def _cleanup() -> None:
        cleanup_deadline = min(operation_deadline, loop.time() + CLEANUP_GRACE_S)
        if subscribed:
            try:
                # Reserve half the grace for closing even if unsubscribe blocks.
                await asyncio.wait_for(
                    transport.send(json.dumps({"id": _SUB_ID, "type": "complete"})),
                    timeout=max(0, (cleanup_deadline - loop.time()) / 2),
                )
            except Exception:
                log.debug("subscription cleanup: unsubscribe failed")
        try:
            await asyncio.wait_for(
                transport.close(), timeout=max(0, cleanup_deadline - loop.time())
            )
        except Exception:
            log.debug("subscription cleanup: close failed")

    try:
        async with asyncio.timeout_at(operation_deadline) as op_timeout:
            try:
                return await _sample()
            finally:
                try:
                    await _cleanup()
                except asyncio.CancelledError:
                    # The operation timeout may fire while cleanup is awaiting close.
                    # Preserve the primary result/error, but propagate caller cancellation.
                    # expired() is authoritative; loop.time() can read early since
                    # asyncio fires timers up to clock_resolution ahead of schedule.
                    if not op_timeout.expired():
                        raise
                    log.debug("subscription cleanup: operation deadline reached")
    except TimeoutError:
        raise UnraidConnectionError(
            "The Unraid stats subscription exceeded its sampling deadline. Retry the request."
        ) from None
    except WSClosed:
        raise UnraidConnectionError(
            "Unraid closed the stats subscription while sending a protocol frame."
        ) from None


class _WebsocketsTransport:
    """Adapter wrapping a live ``websockets`` connection as a :class:`WSTransport`."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._close_started = False

    async def send(self, message: str) -> None:
        import websockets

        try:
            await self._ws.send(message)
        except websockets.ConnectionClosed as exc:
            raise WSClosed(code=getattr(exc, "code", None)) from exc

    async def close(self) -> None:
        # The sampler owns cleanup; open_ws only closes if sampling never did.
        if self._close_started:
            return
        self._close_started = True
        try:
            await self._ws.close()
        finally:
            # Release the socket even if a stalled close handshake is cancelled.
            self._ws.transport.abort()

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
        ws = await connect(
            ws_url,
            subprotocols=[SUBPROTOCOL],  # type: ignore[list-item]
            ssl=ssl_context,
            open_timeout=open_timeout,
            proxy=None,  # parity with httpx trust_env=False: ignore proxy env vars
        )
        transport = _WebsocketsTransport(ws)
        try:
            yield transport
        finally:
            try:
                await asyncio.wait_for(transport.close(), timeout=CLEANUP_GRACE_S)
            except Exception:
                log.debug("subscription cleanup: close failed")
    except (OSError, websockets.WebSocketException) as exc:
        # Never include ``exc`` text verbatim — keep the message static and secret-free.
        raise UnraidConnectionError(
            f"Could not open a stats websocket to {_host(ws_url)}. "
            "Check UNRAID_API_URL, the network, and TLS settings."
        ) from exc


def _host(ws_url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(ws_url).netloc or ws_url
