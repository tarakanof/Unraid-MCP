"""Tests for the ``get_docker_container_stats`` tool logic (``fetch_container_stats``).

These exercise the docker-layer wiring (settings → ws_url/ssl_context → sampler →
shaper → envelope) with an injected fake connect/transport, so no live box or real
websocket is touched. The graphql-transport-ws state machine itself is covered in
``test_subscriptions.py``.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp import subscriptions
from unraid_mcp.config import Settings
from unraid_mcp.errors import UnraidConnectionError
from unraid_mcp.server import build_server
from unraid_mcp.subscriptions import WSClosed
from unraid_mcp.tools import docker
from unraid_mcp.tools._base import feature_unsupported  # noqa: F401  (documents the path)

KEY = "supersecretkey123"
_BLOCK = object()


def _settings(**overrides) -> Settings:
    base = {"api_url": "https://tower.local/graphql", "api_key": KEY}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


class _FakeTransport:
    def __init__(self, script):
        self._script = list(script)
        self.sent: list[str] = []

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        pass

    async def recv(self):
        if not self._script:
            raise WSClosed()
        item = self._script.pop(0)
        if isinstance(item, WSClosed):
            raise item
        if item is _BLOCK:
            await asyncio.Event().wait()
        return item


def _ack():
    return json.dumps({"type": "connection_ack"})


def _next(cid, cpu=1.5, mem=2.5):
    return json.dumps(
        {
            "type": "next",
            "payload": {
                "data": {
                    "dockerContainerStats": {
                        "id": cid,
                        "cpuPercent": cpu,
                        "memPercent": mem,
                        "memUsage": "65.56MiB / 31.25GiB",
                        "netIO": "1kB / 2kB",
                        "blockIO": "0B / 0B",
                    }
                }
            },
        }
    )


def _fake_connect(transport, capture=None):
    @asynccontextmanager
    async def _connect(ws_url, ssl_context, *, open_timeout):
        if capture is not None:
            capture.update(ws_url=ws_url, ssl_context=ssl_context, open_timeout=open_timeout)
        yield transport

    return _connect


async def _fetch(script, *, settings=None, timeout_s=5.0, capture=None, api_version=None):
    settings = settings or _settings()
    transport = _FakeTransport(script)
    return transport, await docker.fetch_container_stats(
        None,
        settings=settings,
        connect=_fake_connect(transport, capture),
        timeout_s=timeout_s,
        api_version=api_version,
    )


# ── Happy path ────────────────────────────────────────────────────────────────


async def test_happy_path_returns_one_entry_per_container():
    script = [_ack(), _next("docker:a", cpu=10.0), _next("docker:b", cpu=20.0), _next("docker:a")]
    _, result = await _fetch(script)
    assert result["sampled"] == 2
    assert result["partial"] is False
    assert result["note"] is None
    ids = [c["id"] for c in result["containers"]]
    assert ids == ["docker:a", "docker:b"]
    first = result["containers"][0]
    assert first["cpu_percent"] == 10.0
    assert first["mem_usage"] == "65.56MiB / 31.25GiB"  # pre-formatted string, not bytes


async def test_ansi_in_id_is_sanitized_before_keying_and_output():
    """Regression for #27: the first-of-cycle id carries an ANSI escape. It must
    dedup against its clean form and be emitted control-char-free so it matches
    list_docker_containers ids."""
    polluted = "docker:abc123\x1b[H"
    script = [_ack(), _next(polluted), _next("docker:def456"), _next(polluted)]
    _, result = await _fetch(script)
    ids = [c["id"] for c in result["containers"]]
    assert ids == ["docker:abc123", "docker:def456"]  # clean, deduped
    assert all("\x1b" not in i and "[H" not in i for i in ids)
    assert result["sampled"] == 2  # the polluted repeat did not double-count


# ── Bounded / partial ─────────────────────────────────────────────────────────


async def test_deadline_mid_cycle_returns_partial_with_note():
    script = [_ack(), _next("docker:a"), _next("docker:b"), _BLOCK]
    _, result = await _fetch(script, timeout_s=0.1)
    assert result["partial"] is True
    assert result["sampled"] == 2
    assert "Partial snapshot" in result["note"]


async def test_no_event_raises_clear_tool_error_never_hangs():
    script = [_ack(), _BLOCK]  # ack but no data before deadline
    with pytest.raises(ToolError) as exc:
        await _fetch(script, timeout_s=0.1)
    assert "no sample" in str(exc.value)


# ── Old-build degradation ─────────────────────────────────────────────────────


async def test_old_build_unsupported_field_becomes_feature_unsupported():
    err = json.dumps(
        {
            "type": "error",
            "payload": [
                {"message": 'Cannot query field "dockerContainerStats" on type "Subscription".'}
            ],
        }
    )
    with pytest.raises(ToolError) as exc:
        await _fetch([_ack(), err], api_version="4.20.0")
    msg = str(exc.value)
    assert "does not support" in msg
    assert "4.20.0" in msg


# ── TLS parity wiring ─────────────────────────────────────────────────────────


async def test_connect_receives_ws_url_and_ssl_context_from_settings():
    capture: dict = {}
    settings = _settings()  # https → wss → a real SSLContext
    await _fetch([_ack(), _next("docker:a"), _next("docker:a")], settings=settings, capture=capture)
    assert capture["ws_url"] == settings.ws_url() == "wss://tower.local/graphql"
    assert isinstance(capture["ssl_context"], ssl.SSLContext)


async def test_connect_receives_none_ssl_for_plaintext_ws():
    capture: dict = {}
    settings = _settings(api_url="http://10.0.0.5:8080/graphql")
    await _fetch([_ack(), _next("docker:a"), _next("docker:a")], settings=settings, capture=capture)
    assert capture["ws_url"] == "ws://10.0.0.5:8080/graphql"
    assert capture["ssl_context"] is None


async def test_connection_init_carries_key_only_place():
    transport, _ = await _fetch([_ack(), _next("docker:a"), _next("docker:a")])
    init = json.loads(transport.sent[0])
    assert init["payload"] == {"x-api-key": KEY}


# ── Secrets never leak ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "script,timeout_s",
    [
        ([_ack(), _BLOCK], 0.1),  # no-event ToolError
        ([WSClosed(code=4403)], 0.1),  # auth close
        ([_ack(), json.dumps({"type": "error", "payload": [{"message": f"leak {KEY}"}]})], 5.0),
    ],
)
async def test_api_key_never_in_raised_tool_or_domain_error(script, timeout_s):
    with pytest.raises(Exception) as exc:  # ToolError or UnraidError subclass
        await _fetch(script, timeout_s=timeout_s)
    assert KEY not in str(exc.value)


async def test_stats_settings_bearer_token_redacted_in_tool_output():
    token = "bearer-token-1234567890123456789012"
    _, result = await _fetch(
        [_ack(), _next(f"docker:{token}"), json.dumps({"type": "complete"})],
        settings=_settings(bearer_token=token),
    )
    assert token not in str(result)
    assert "***REDACTED***" in str(result)


async def test_stats_connection_error_redacts_configured_secrets():
    token = "bearer-token-1234567890123456789012"

    @asynccontextmanager
    async def connect(*args, **kwargs):
        raise UnraidConnectionError(f"failed {KEY} {token}")
        yield

    with pytest.raises(UnraidConnectionError) as exc:
        await docker.fetch_container_stats(
            None, settings=_settings(bearer_token=token), connect=connect
        )
    assert KEY not in str(exc.value)
    assert token not in str(exc.value)
    assert "***REDACTED***" in str(exc.value)


async def test_overall_deadline_bounds_blocked_connection_setup(monkeypatch):
    monkeypatch.setattr(subscriptions, "CLEANUP_GRACE_S", 0.05)

    @asynccontextmanager
    async def connect(*args, **kwargs):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    start = time.monotonic()
    with pytest.raises(UnraidConnectionError, match="operation deadline") as exc:
        await asyncio.wait_for(
            docker.fetch_container_stats(
                None, settings=_settings(), connect=connect, timeout_s=0.05
            ),
            timeout=0.3,
        )
    assert time.monotonic() - start < 0.25
    assert KEY not in str(exc.value)


async def test_connection_setup_uses_sampling_window(monkeypatch):
    monkeypatch.setattr(subscriptions, "CLEANUP_GRACE_S", 0.05)
    transport = _FakeTransport([_ack(), _next("a"), _BLOCK])

    @asynccontextmanager
    async def connect(*args, **kwargs):
        await asyncio.sleep(0.06)
        yield transport

    start = time.monotonic()
    result = await docker.fetch_container_stats(
        None, settings=_settings(), connect=connect, timeout_s=0.1
    )
    assert result["partial"] is True
    assert time.monotonic() - start < 0.14


@pytest.mark.parametrize("blocked_send", [False, True])
async def test_stats_tool_through_in_memory_client(monkeypatch, blocked_send):
    class Transport(_FakeTransport):
        async def send(self, message):
            if blocked_send:
                await asyncio.Event().wait()
            await super().send(message)

    transport = Transport([_ack(), _next("docker:a"), _next("docker:a")])
    monkeypatch.setattr(subscriptions, "open_ws", _fake_connect(transport))
    # The tool wrapper uses fetch_container_stats's default timeout argument.
    monkeypatch.setattr(
        docker.fetch_container_stats,
        "__kwdefaults__",
        {**docker.fetch_container_stats.__kwdefaults__, "timeout_s": 0.05},
    )
    with respx.mock:
        respx.post("https://tower.local/graphql").mock(
            return_value=httpx.Response(200, json={"data": None})
        )
        async with Client(build_server(_settings()), raise_exceptions=True) as session:
            result = await session.call_tool("get_docker_container_stats", {})
    assert result.is_error is blocked_send
    if blocked_send:
        assert "connection_ack" in result.content[0].text
        assert KEY not in result.content[0].text
    else:
        assert result.structured_content["sampled"] == 1
        assert result.structured_content["partial"] is False


@pytest.mark.parametrize("primary_error", [False, True])
async def test_tool_deadline_during_close_preserves_primary_outcome(monkeypatch, primary_error):
    monkeypatch.setattr(subscriptions, "CLEANUP_GRACE_S", 0.05)

    class Transport(_FakeTransport):
        async def close(self):
            await asyncio.Event().wait()

    ending = (
        json.dumps({"type": "error", "payload": [{"message": "Cannot query field stats"}]})
        if primary_error
        else _BLOCK
    )
    transport = Transport([_ack(), _next("a"), ending])
    start = time.monotonic()
    if primary_error:
        with pytest.raises(ToolError, match="does not support"):
            await docker.fetch_container_stats(
                None, settings=_settings(), connect=_fake_connect(transport), timeout_s=0.05
            )
    else:
        result = await docker.fetch_container_stats(
            None, settings=_settings(), connect=_fake_connect(transport), timeout_s=0.05
        )
        assert result["partial"] is True
        assert result["sampled"] == 1
    assert time.monotonic() - start < 0.25
