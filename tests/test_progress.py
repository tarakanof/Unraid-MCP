"""Progress notifications (#114): stats sampling + batch docker updates.

Protocol tests drive the server through the SDK in-memory client with a
``progress_callback``; the logic-level tests cover the swallow-failures rule.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp import subscriptions
from unraid_mcp.server import build_server
from unraid_mcp.tools import _base, docker
from unraid_mcp.tools._base import progress_reporter, with_heartbeat

from .test_tools_stats import _ack, _fake_connect, _FakeTransport, _next, _settings

URL = "https://tower.local/graphql"


def _recorder():
    events: list[tuple[float, float | None, str | None]] = []

    async def cb(progress, total, message):
        events.append((progress, total, message))

    return events, cb


def _assert_monotonic(events):
    values = [e[0] for e in events]
    assert values == sorted(set(values)), values  # strictly increasing
    assert all(e[1] is None for e in events)  # total is never sent (unknown)


class _StubCtx:
    """Minimal Context stand-in: records report_progress calls, optionally stalls."""

    def __init__(self, *, stall=False, fail=False):
        self.calls = []
        self.stall, self.fail = stall, fail

    async def report_progress(self, progress, total=None, message=None):
        self.calls.append((progress, total, message))
        if self.stall:
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("client went away")


def _update_route(delay: float = 0.0, *, key: str = "updateContainers", n: int = 2):
    async def _side_effect(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if key in query:
            await asyncio.sleep(delay)
            rows = [
                {"id": f"1:{i}", "names": [f"/c{i}"], "state": "RUNNING", "status": "Up"}
                for i in range(n)
            ]
            return httpx.Response(200, json={"data": {"docker": {key: rows}}})
        return httpx.Response(500)  # startup probes: failure is tolerated

    return _side_effect


# ── Stats sampling ────────────────────────────────────────────────────────────


async def test_stats_progress_per_container_through_protocol(settings_factory, monkeypatch):
    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    monkeypatch.setattr(subscriptions, "open_ws", _fake_connect(transport))
    events, cb = _recorder()
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool("get_docker_container_stats", {}, progress_callback=cb)
    assert result.is_error is False
    assert result.structured_content["sampled"] == 2
    assert [e[0] for e in events] == [1, 2]
    _assert_monotonic(events)
    assert "Sampled 2" in events[-1][2]


async def test_stats_without_progress_token_unchanged(settings_factory, monkeypatch):
    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    monkeypatch.setattr(subscriptions, "open_ws", _fake_connect(transport))
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        mcp = build_server(settings_factory())
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool("get_docker_container_stats", {})
    assert result.is_error is False
    assert result.structured_content["sampled"] == 2


async def test_stats_progress_failure_is_swallowed():
    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    async with progress_reporter(_StubCtx(fail=True)) as progress:
        result = await docker.fetch_container_stats(
            None, settings=_settings(), connect=_fake_connect(transport), progress=progress
        )
    assert result["sampled"] == 2


async def test_stalled_reporter_cannot_hang_sampling(monkeypatch):
    monkeypatch.setattr(_base, "PROGRESS_TIMEOUT_S", 0.05)
    ctx = _StubCtx(stall=True)
    transport = _FakeTransport([_ack(), _next("docker:a"), _next("docker:b"), _next("docker:a")])
    async with progress_reporter(ctx) as progress:
        result = await asyncio.wait_for(
            docker.fetch_container_stats(
                None, settings=_settings(), connect=_fake_connect(transport), progress=progress
            ),
            timeout=3,
        )
    assert result["sampled"] == 2 and result["partial"] is False


async def test_stalled_reporter_does_not_eat_sampling_deadline(monkeypatch):
    """A stalled handler must yield the same result as no reporter at all."""
    monkeypatch.setattr(_base, "PROGRESS_TIMEOUT_S", 0.2)
    ids = [f"docker:{i}" for i in range(30)]
    script = [_ack(), *[_next(c) for c in ids], _next(ids[0])]

    async def run(progress_cm):
        transport = _FakeTransport(script)
        async with progress_cm as progress:
            return await docker.fetch_container_stats(
                None,
                settings=_settings(),
                connect=_fake_connect(transport),
                timeout_s=1.0,  # < 30 sequential 0.2s sends would need
                progress=progress,
            )

    @asynccontextmanager
    async def _none():
        yield None

    baseline = await run(_none())
    stalled = await run(progress_reporter(_StubCtx(stall=True)))
    assert (stalled["sampled"], stalled["partial"]) == (baseline["sampled"], baseline["partial"])
    assert stalled["sampled"] == 30 and stalled["partial"] is False


async def test_reporter_leaks_no_tasks():
    before = len(asyncio.all_tasks())
    async with progress_reporter(_StubCtx(stall=True)) as progress:
        await progress("x")
    assert len(asyncio.all_tasks()) == before


async def test_stalled_reporter_cannot_hang_updates(monkeypatch, mocked_client):
    monkeypatch.setattr(_base, "PROGRESS_TIMEOUT_S", 0.05)
    ctx = _StubCtx(stall=True)
    body = {"data": {"docker": {"updateContainers": [{"id": "1:a", "names": ["/a"]}]}}}
    async with (
        mocked_client(httpx.Response(200, json=body)) as (client, _route),
        progress_reporter(ctx) as progress,
    ):
        result = await asyncio.wait_for(
            docker.do_update_containers(client, ["1:a"], confirm=True, progress=progress),
            timeout=3,
        )
    assert result[0]["id"] == "1:a"
    assert len(ctx.calls) >= 1  # worker attempted (and timed out) without blocking the update


async def test_reporter_counter_strictly_increases():
    ctx = _StubCtx()
    async with progress_reporter(ctx) as report:
        for m in "abc":
            await report(m)
    assert [c[0] for c in ctx.calls] == [1, 2, 3]
    assert all(c[1] is None for c in ctx.calls)


# ── Batch updates ─────────────────────────────────────────────────────────────


async def test_batch_update_progress_through_protocol(settings_factory, monkeypatch):
    monkeypatch.setattr(docker, "UPDATE_HEARTBEAT_S", 0.05)
    events, cb = _recorder()
    with respx.mock:
        respx.post(URL).mock(side_effect=_update_route(delay=0.4))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool(
                "update_docker_containers",
                {"container_ids": ["1:0", "1:1"], "confirm": True},
                progress_callback=cb,
            )
    assert result.is_error is False
    assert len(events) >= 2
    _assert_monotonic(events)
    assert "Updating 2" in events[0][2] and "Updated 2" in events[-1][2]
    # delay (0.4s) >> interval (0.05s): several heartbeats, more than n=2 containers
    beats = [e for e in events[1:-1] if "elapsed" in (e[2] or "")]
    assert len(beats) >= 3


async def test_update_all_progress_through_protocol(settings_factory, monkeypatch):
    monkeypatch.setattr(docker, "UPDATE_HEARTBEAT_S", 0.05)
    events, cb = _recorder()
    with respx.mock:
        respx.post(URL).mock(side_effect=_update_route(delay=0.2, key="updateAllContainers"))
        mcp = build_server(settings_factory(allow_mutations=True, allow_dangerous=True))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool(
                "update_all_docker_containers", {"confirm": True}, progress_callback=cb
            )
    assert result.is_error is False
    assert len(events) >= 2
    _assert_monotonic(events)
    assert "Updated 2" in events[-1][2]


async def test_batch_update_without_progress_token_unchanged(settings_factory):
    with respx.mock:
        respx.post(URL).mock(side_effect=_update_route())
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp, raise_exceptions=True) as session:
            result = await session.call_tool(
                "update_docker_containers", {"container_ids": ["1:0", "1:1"], "confirm": True}
            )
    assert result.is_error is False
    assert len(result.structured_content["result"]) == 2


async def test_update_refused_without_confirm_reports_no_progress(mocked_client):
    ctx = _StubCtx()
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await docker.do_update_containers(client, ["1:a"], confirm=False, progress=None)
        assert route.call_count == 0
    assert ctx.calls == []


# ── Helpers ───────────────────────────────────────────────────────────────────


async def test_with_heartbeat_swallows_callback_errors_and_cancels_task():
    async def boom(_m):
        raise RuntimeError("nope")

    async def work():
        await asyncio.sleep(0.12)
        return "ok"

    assert await with_heartbeat(work(), boom, interval_s=0.03) == "ok"


async def test_with_heartbeat_without_callback_just_awaits():
    async def work():
        return 7

    assert await with_heartbeat(work(), None, interval_s=0.01) == 7


async def test_cancelled_call_leaves_no_reporter_tasks(monkeypatch):
    monkeypatch.setattr(_base, "PROGRESS_TIMEOUT_S", 5.0)
    before = {t for t in asyncio.all_tasks() if not t.done()}

    async def call():
        async with progress_reporter(_StubCtx(stall=True)) as progress:
            await progress("x")
            await asyncio.sleep(0.05)  # let the worker pick it up and stall

    task = asyncio.ensure_future(call())
    await asyncio.sleep(0.1)  # call has exited its body and is inside the flush
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert {t for t in asyncio.all_tasks() if not t.done()} == before
