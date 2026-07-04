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

import pytest

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


async def _sample(script, *, deadline_s=5.0):
    transport = FakeTransport(script)
    result = await sample_subscription(
        transport,
        api_key=KEY,
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
