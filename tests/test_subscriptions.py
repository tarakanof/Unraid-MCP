"""Unit tests for the MCP-free graphql-transport-ws sampler.

The protocol state machine (connection_init → ack → subscribe → next* → complete)
is driven against a scripted fake transport — no live box, no real websocket. The
required failure paths (no ack, auth close, error frame, premature close, deadline
mid-cycle) each get a test, plus the secrets-never-leak invariant on every path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import Mock

import pytest

from unraid_mcp import subscriptions
from unraid_mcp.errors import UnraidAuthError, UnraidConnectionError, UnraidGraphQLError
from unraid_mcp.subscriptions import WSClosed, sample_subscription

KEY = "supersecretkey123"


# Simple, sanitization-free key/complete for exercising the state machine itself
# (id sanitization is a docker-layer + shaper concern, tested separately).
def _key(data):
    return (data.get("dockerContainerStats") or {}).get("id")


def _complete(collected, was_new):
    return not was_new and len(collected) >= 1


_BLOCK = object()


class FakeTransport:
    """Returns a canned script of server frames from ``recv``; records ``send``.

    Script items: a JSON string (returned), a :class:`WSClosed` (raised),
    or ``_BLOCK`` (hang forever so ``wait_for`` hits its deadline). An exhausted
    script raises ``WSClosed`` (peer closed).
    """

    def __init__(self, script):
        self._script = list(script)
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        pass

    async def recv(self) -> str:
        if not self._script:
            raise WSClosed()
        item = self._script.pop(0)
        if isinstance(item, WSClosed):
            raise item
        if item is _BLOCK:
            await asyncio.Event().wait()  # cancelled by wait_for on deadline
        return item


def _ack() -> str:
    return json.dumps({"type": "connection_ack"})


def _next(cid: str, cpu: float = 1.5, mem: float = 2.5) -> str:
    return json.dumps(
        {
            "type": "next",
            "payload": {
                "data": {
                    "dockerContainerStats": {
                        "id": cid,
                        "cpuPercent": cpu,
                        "memPercent": mem,
                        "memUsage": "10MiB / 1GiB",
                        "netIO": "1kB / 2kB",
                        "blockIO": "0B / 0B",
                    }
                }
            },
        }
    )


async def _sample(script, *, deadline_s=5.0, bearer_token=None):
    transport = FakeTransport(script)
    result = await sample_subscription(
        transport,
        api_key=KEY,
        bearer_token=bearer_token,
        query="subscription { dockerContainerStats { id } }",
        deadline_s=deadline_s,
        key=_key,
        is_complete=_complete,
    )
    return transport, result


# ── Happy path ────────────────────────────────────────────────────────────────


async def test_keyless_frame_is_ignored_not_treated_as_cycle_repeat():
    """A ``next`` frame whose key() is None (missing/empty id) must be skipped:
    with was_new=False it would otherwise satisfy is_complete and silently
    truncate the sample after the first container."""
    keyless = json.dumps(
        {"type": "next", "payload": {"data": {"dockerContainerStats": {"cpuPercent": 0.1}}}}
    )
    script = [_ack(), _next("a"), keyless, _next("b"), _next("a")]
    _, (events, deadline_hit) = await _sample(script)
    ids = [(e["dockerContainerStats"]["id"]) for e in events]
    assert ids == ["a", "b"]  # keyless frame neither collected nor completing
    assert deadline_hit is False


async def test_happy_path_multi_container_full_cycle():
    script = [_ack(), _next("a"), _next("b"), _next("c"), _next("a")]
    transport, (events, deadline_hit) = await _sample(script)
    ids = [(e["dockerContainerStats"]["id"]) for e in events]
    assert ids == ["a", "b", "c"]  # deduped, insertion-ordered, stops on repeat
    assert deadline_hit is False
    # connection_init carried the key; subscribe + a closing complete were sent.
    init = json.loads(transport.sent[0])
    assert init["type"] == "connection_init"
    assert init["payload"] == {"x-api-key": KEY}
    assert any(json.loads(m).get("type") == "subscribe" for m in transport.sent)
    assert any(json.loads(m).get("type") == "complete" for m in transport.sent)


async def test_single_container_completes_on_repeat():
    _, (events, deadline_hit) = await _sample([_ack(), _next("only"), _next("only")])
    assert [e["dockerContainerStats"]["id"] for e in events] == ["only"]
    assert deadline_hit is False


async def test_server_complete_frame_ends_sampling():
    _, (events, deadline_hit) = await _sample(
        [_ack(), _next("a"), json.dumps({"type": "complete"})]
    )
    assert [e["dockerContainerStats"]["id"] for e in events] == ["a"]
    assert deadline_hit is False


async def test_ping_is_answered_with_pong():
    transport, (events, _) = await _sample(
        [_ack(), json.dumps({"type": "ping"}), _next("a"), _next("a")]
    )
    assert [e["dockerContainerStats"]["id"] for e in events] == ["a"]
    assert any(json.loads(m).get("type") == "pong" for m in transport.sent)


# ── Failure paths ─────────────────────────────────────────────────────────────


async def test_no_ack_times_out_as_connection_error():
    with pytest.raises(UnraidConnectionError):
        await _sample([_BLOCK], deadline_s=0.1)


async def test_close_before_ack_is_connection_error():
    with pytest.raises(UnraidConnectionError):
        await _sample([WSClosed()])


async def test_auth_close_code_before_ack_is_auth_error():
    with pytest.raises(UnraidAuthError):
        await _sample([WSClosed(code=4403)])


async def test_connection_error_frame_is_auth_error():
    with pytest.raises(UnraidAuthError):
        await _sample([json.dumps({"type": "connection_error", "payload": {}})])


async def test_unexpected_first_frame_is_connection_error():
    with pytest.raises(UnraidConnectionError):
        await _sample([json.dumps({"type": "next", "payload": {}})])


async def test_error_frame_raises_graphql_error():
    err = json.dumps(
        {
            "type": "error",
            "payload": [{"message": 'Cannot query field "dockerContainerStats".'}],
        }
    )
    with pytest.raises(UnraidGraphQLError) as exc:
        await _sample([_ack(), err])
    assert "Cannot query field" in str(exc.value)


async def test_premature_close_with_data_returns_partial():
    _, (events, deadline_hit) = await _sample([_ack(), _next("a"), _next("b"), WSClosed()])
    assert [e["dockerContainerStats"]["id"] for e in events] == ["a", "b"]
    assert deadline_hit is True


async def test_premature_close_without_data_is_connection_error():
    with pytest.raises(UnraidConnectionError):
        await _sample([_ack(), WSClosed()])


async def test_deadline_hit_mid_cycle_returns_partial():
    _, (events, deadline_hit) = await _sample(
        [_ack(), _next("a"), _next("b"), _BLOCK], deadline_s=0.1
    )
    assert [e["dockerContainerStats"]["id"] for e in events] == ["a", "b"]
    assert deadline_hit is True


# ── Secrets never leak ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        [_BLOCK],  # no ack
        [WSClosed()],  # close before ack
        [WSClosed(code=4403)],  # auth close
        [json.dumps({"type": "connection_error"})],  # auth frame
        [_ack(), WSClosed()],  # premature close no data
        [_ack(), json.dumps({"type": "error", "payload": [{"message": "boom"}]})],
    ],
)
async def test_api_key_never_in_raised_error(script):
    with pytest.raises((UnraidConnectionError, UnraidAuthError, UnraidGraphQLError)) as exc:
        await _sample(script, deadline_s=0.1)
    assert KEY not in str(exc.value)


async def test_server_echoed_key_is_redacted_in_error():
    """Defence in depth: a hostile server reflecting the key into an error frame
    must not surface it in the raised exception."""
    err = json.dumps({"type": "error", "payload": [{"message": f"bad key {KEY} here"}]})
    with pytest.raises(UnraidGraphQLError) as exc:
        await _sample([_ack(), err])
    assert KEY not in str(exc.value)
    assert "***REDACTED***" in str(exc.value)


async def test_api_key_not_logged(caplog):
    with caplog.at_level(logging.DEBUG, logger="unraid_mcp.subscriptions"):
        await _sample([_ack(), _next("a"), _next("a")])
    assert KEY not in caplog.text


@pytest.mark.parametrize("secret", [KEY, "bearer-token-1234567890123456789012"])
async def test_unexpected_frame_type_redacts_secrets(secret):
    with pytest.raises(UnraidConnectionError) as exc:
        await _sample([json.dumps({"type": f"unexpected {secret}"})], bearer_token=secret)
    assert secret not in str(exc.value)
    assert "***REDACTED***" in str(exc.value)


@pytest.mark.parametrize("secret", [KEY, "bearer-token-1234567890123456789012"])
@pytest.mark.parametrize("as_list", [True, False])
async def test_error_frame_redacts_message_and_structured_errors(secret, as_list):
    payload = {"message": f"bad {secret}", "extensions": {"echo": [secret, {"nested": secret}]}}
    if as_list:
        payload = [payload]
    with pytest.raises(UnraidGraphQLError) as exc:
        await _sample(
            [_ack(), json.dumps({"type": "error", "payload": payload})], bearer_token=secret
        )
    assert secret not in str(exc.value)
    assert secret not in str(exc.value.errors)
    assert "***REDACTED***" in str(exc.value)
    assert "***REDACTED***" in str(exc.value.errors)


@pytest.mark.parametrize("secret", [KEY, "bearer-token-1234567890123456789012"])
async def test_subscription_samples_and_complete_payload_redact_secrets(secret, caplog):
    frame = json.dumps(
        {
            "type": "next",
            "payload": {
                "data": {"dockerContainerStats": {"id": "a", "echo": [secret, {"nested": secret}]}}
            },
        }
    )
    complete = json.dumps({"type": "complete", "payload": {"echo": secret}})
    with caplog.at_level(logging.DEBUG, logger="unraid_mcp.subscriptions"):
        _, (events, deadline_hit) = await _sample([_ack(), frame, complete], bearer_token=secret)
    assert events[0]["dockerContainerStats"]["echo"] == [
        "***REDACTED***",
        {"nested": "***REDACTED***"},
    ]
    assert deadline_hit is False
    assert secret not in caplog.text


async def test_invalid_json_frame_maps_to_secret_free_connection_error():
    with pytest.raises(UnraidConnectionError) as exc:
        await _sample([f"invalid {KEY}"])
    assert KEY not in str(exc.value)


async def test_numeric_frame_type_redacts_all_digit_secret():
    secret = "1234567890123456"
    with pytest.raises(UnraidConnectionError) as exc:
        await _sample([json.dumps({"type": int(secret)})], bearer_token=secret)
    assert secret not in str(exc.value)
    assert "***REDACTED***" in str(exc.value)


class BlockingTransport(FakeTransport):
    def __init__(self, script, *, blocked_send=None, blocked_close=False):
        super().__init__(script)
        self.blocked_send = blocked_send
        self.blocked_close = blocked_close
        self.close_started = False

    async def send(self, message):
        await super().send(message)
        if self.blocked_send == "*" or json.loads(message)["type"] == self.blocked_send:
            await asyncio.Event().wait()

    async def close(self):
        self.close_started = True
        if self.blocked_close:
            await asyncio.Event().wait()


async def _sample_transport(transport, *, deadline_s=0.05):
    return await sample_subscription(
        transport,
        api_key=KEY,
        query="subscription { dockerContainerStats { id } }",
        deadline_s=deadline_s,
        key=_key,
        is_complete=_complete,
    )


@pytest.mark.parametrize("blocked_send", ["connection_init", "subscribe", "*"])
async def test_blocked_send_obeys_deadline(blocked_send):
    transport = BlockingTransport([_ack()], blocked_send=blocked_send)
    start = time.monotonic()
    # The watchdog makes the old unbounded send fail instead of hanging pytest.
    with pytest.raises(UnraidConnectionError) as exc:
        await asyncio.wait_for(_sample_transport(transport), timeout=2.3)
    assert time.monotonic() - start < 2.2
    assert KEY not in str(exc.value)
    assert transport.close_started


async def test_blocked_pong_returns_partial_at_deadline():
    transport = BlockingTransport(
        [_ack(), _next("a"), json.dumps({"type": "ping"})], blocked_send="pong"
    )
    events, deadline_hit = await asyncio.wait_for(_sample_transport(transport), timeout=2.3)
    assert events == [json.loads(_next("a"))["payload"]["data"]]
    assert deadline_hit is True


async def test_pong_send_closed_returns_partial():
    class ClosingPong(FakeTransport):
        async def send(self, message):
            await super().send(message)
            if json.loads(message)["type"] == "pong":
                raise WSClosed()

    transport = ClosingPong([_ack(), _next("a"), json.dumps({"type": "ping"})])
    events, deadline_hit = await _sample_transport(transport, deadline_s=5.0)
    assert events == [json.loads(_next("a"))["payload"]["data"]]
    assert deadline_hit is True


async def test_pong_send_closed_without_data_is_connection_error():
    class ClosingPong(FakeTransport):
        async def send(self, message):
            await super().send(message)
            if json.loads(message)["type"] == "pong":
                raise WSClosed()

    transport = ClosingPong([_ack(), json.dumps({"type": "ping"})])
    with pytest.raises(UnraidConnectionError, match="before sending any data"):
        await _sample_transport(transport, deadline_s=5.0)


async def test_operation_timeout_during_cleanup_detected_despite_early_clock(monkeypatch):
    # asyncio may fire timers slightly early: loop.time() then still reads before
    # the deadline. The timeout must be recognised via the timeout context itself.
    monkeypatch.setattr(subscriptions, "CLEANUP_GRACE_S", 0.05)
    loop = asyncio.get_running_loop()
    real_time = loop.time

    class EarlyClock(FakeTransport):
        async def close(self):
            # Timers are already scheduled; lag the clock so they fire "early".
            monkeypatch.setattr(loop, "time", lambda: real_time() - 0.5, raising=False)
            await asyncio.Event().wait()

    events, _ = await asyncio.wait_for(
        _sample_transport(EarlyClock([_ack(), _next("a"), _BLOCK])), timeout=3
    )
    assert events


@pytest.mark.parametrize(
    "blocked_send,blocked_close", [("complete", False), (None, True), ("complete", True)]
)
@pytest.mark.parametrize("outcome", ["success", "partial", "error"])
async def test_blocked_cleanup_preserves_primary_outcome(
    monkeypatch, caplog, blocked_send, blocked_close, outcome
):
    monkeypatch.setattr(subscriptions, "CLEANUP_GRACE_S", 0.05)
    endings = {
        "success": [_next("a"), _next("a")],
        "partial": [_next("a"), _BLOCK],
        "error": [json.dumps({"type": "error", "payload": [{"message": "primary error"}]})],
    }
    transport = BlockingTransport(
        [_ack(), *endings[outcome]], blocked_send=blocked_send, blocked_close=blocked_close
    )
    start = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="unraid_mcp.subscriptions"):
        if outcome == "error":
            with pytest.raises(UnraidGraphQLError, match="primary error"):
                await asyncio.wait_for(_sample_transport(transport), timeout=0.3)
        else:
            events, deadline_hit = await asyncio.wait_for(_sample_transport(transport), timeout=0.3)
            assert len(events) == 1
            assert deadline_hit is (outcome == "partial")
    assert time.monotonic() - start < 0.25
    assert "cleanup" in caplog.text
    assert KEY not in caplog.text


async def test_cleanup_exceptions_are_logged_without_secrets(caplog):
    class FailingCleanupTransport(FakeTransport):
        async def send(self, message):
            if json.loads(message)["type"] == "complete":
                raise RuntimeError(KEY)
            await super().send(message)

        async def close(self):
            raise RuntimeError(KEY)

    with caplog.at_level(logging.DEBUG, logger="unraid_mcp.subscriptions"):
        events, deadline_hit = await _sample_transport(
            FailingCleanupTransport([_ack(), _next("a"), _next("a")])
        )
    assert len(events) == 1
    assert deadline_hit is False
    assert "unsubscribe failed" in caplog.text
    assert "close failed" in caplog.text
    assert KEY not in caplog.text


async def test_caller_cancellation_during_cleanup_propagates():
    close_started = asyncio.Event()

    class CancelledTransport(FakeTransport):
        async def close(self):
            close_started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        _sample_transport(CancelledTransport([_ack(), _next("a"), _next("a")]))
    )
    await asyncio.wait_for(close_started.wait(), timeout=0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize("primary_error", [False, True])
async def test_open_ws_stalled_close_aborts_socket_without_masking_outcome(
    monkeypatch, primary_error
):
    monkeypatch.setattr(subscriptions, "CLEANUP_GRACE_S", 0.05)
    socket = BlockingTransport([_ack(), _next("a"), _next("a")], blocked_close=True)
    socket.transport = Mock()

    async def connect(*args, **kwargs):
        return socket

    monkeypatch.setattr("websockets.asyncio.client.connect", connect)

    async def run():
        async with subscriptions.open_ws("ws://tower.local/graphql", None, open_timeout=0.05) as ws:
            if primary_error:
                raise UnraidConnectionError("primary error")
            return await _sample_transport(ws)

    if primary_error:
        with pytest.raises(UnraidConnectionError, match="primary error"):
            await asyncio.wait_for(run(), timeout=0.3)
    else:
        events, deadline_hit = await asyncio.wait_for(run(), timeout=0.3)
        assert len(events) == 1
        assert deadline_hit is False
    socket.transport.abort.assert_called_once()
