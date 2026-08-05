"""Tests for the ``get_docker_container_stats`` tool logic (``fetch_container_stats``).

These exercise the docker-layer wiring (settings → ws_url/ssl_context → sampler →
shaper → envelope) with an injected fake connect/transport, so no live box or real
websocket is touched. The graphql-transport-ws state machine itself is covered in
``test_subscriptions.py``.
"""

from __future__ import annotations

import json
import ssl
from contextlib import asynccontextmanager

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp.config import Settings
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

    async def recv(self):
        import asyncio

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
