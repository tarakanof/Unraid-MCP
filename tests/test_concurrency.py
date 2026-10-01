"""Independent queries inside composed fetch_* functions run concurrently."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp import queries
from unraid_mcp.client import UnraidClient
from unraid_mcp.errors import UnraidConnectionError
from unraid_mcp.tools import _base, misc, system

from .conftest import KEY, URL

DELAY = 0.4


def _data(data):
    return httpx.Response(200, json={"data": data})


def _gql_error(msg="boom"):
    return httpx.Response(200, json={"errors": [{"message": msg}], "data": None})


def _router(responses, *, delay=DELAY, delays=None, log=None, cancelled=None):
    """side_effect that sleeps (per-query ``delays`` override ``delay``) then answers."""

    async def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if log is not None:
            log.append(query)
        try:
            await asyncio.sleep((delays or {}).get(query, delay))
        except asyncio.CancelledError:
            if cancelled is not None:
                cancelled.append(query)
            raise
        out = responses[query]
        if isinstance(out, Exception):
            raise out
        return out

    return handler


class _Mock:
    def __init__(self, responses, **kw):
        self.responses, self.kw = responses, kw

    async def __aenter__(self):
        self._mock = respx.mock
        self._mock.__enter__()
        respx.post(URL).mock(side_effect=_router(self.responses, **self.kw))
        self._http = httpx.AsyncClient()
        return UnraidClient(URL, KEY, self._http, host_label="tower.local")

    async def __aexit__(self, *exc):
        await self._http.aclose()
        self._mock.__exit__(*exc)


ARRAY_OK = _data({"array": {"state": "STARTED", "disks": []}})
UPS_OK = _data({"upsDevices": []})
NOTIF_OK = _data({"notifications": {"overview": {"unread": {"alert": 0, "warning": 0}}}})


async def test_health_runs_queries_concurrently():
    resp = {
        queries.ARRAY_STATUS: ARRAY_OK,
        queries.UPS_DEVICES: UPS_OK,
        queries.NOTIFICATIONS_OVERVIEW: NOTIF_OK,
    }
    async with _Mock(resp) as client:
        start = time.perf_counter()
        out = await misc.fetch_health(client)
        elapsed = time.perf_counter() - start
    assert out["overall"] == "ok"
    assert elapsed < DELAY * 2.5  # serial would be >= 3 * DELAY


async def test_health_array_failure_still_raises_and_cancels_siblings():
    log: list[str] = []
    cancelled: list[str] = []
    resp = {
        queries.ARRAY_STATUS: httpx.ConnectError("refused"),
        queries.UPS_DEVICES: UPS_OK,
        queries.NOTIFICATIONS_OVERVIEW: NOTIF_OK,
    }
    delays = {
        queries.ARRAY_STATUS: 0.02,
        queries.UPS_DEVICES: 10,
        queries.NOTIFICATIONS_OVERVIEW: 10,
    }
    async with _Mock(resp, delays=delays, log=log, cancelled=cancelled) as client:
        start = time.perf_counter()
        with pytest.raises(UnraidConnectionError):
            await misc.fetch_health(client)
        assert time.perf_counter() - start < 2  # did not wait for the slow siblings
    assert set(log) == set(resp)  # all three were in flight
    assert set(cancelled) == {queries.UPS_DEVICES, queries.NOTIFICATIONS_OVERVIEW}
    assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]


async def test_health_ups_failure_still_degrades():
    resp = {
        queries.ARRAY_STATUS: ARRAY_OK,
        queries.UPS_DEVICES: _gql_error("no ups"),
        queries.UPS_CONFIGURATION: _data({"upsConfiguration": {"service": "enable"}}),
        queries.NOTIFICATIONS_OVERVIEW: NOTIF_OK,
    }
    async with _Mock(resp, delay=0.01) as client:
        out = await misc.fetch_health(client)
    assert out["ups"] == []
    assert out["checks"] == {"array": "ok", "ups": "failed", "notifications": "ok"}
    assert out["overall"] == "degraded"


async def test_plugins_runs_queries_concurrently():
    resp = {
        queries.PLUGINS: _data({"plugins": [{"name": "a", "version": "1"}]}),
        queries.INSTALLED_UNRAID_PLUGINS: _data({"installedUnraidPlugins": ["a.plg", "b.plg"]}),
    }
    async with _Mock(resp) as client:
        start = time.perf_counter()
        out = await misc.fetch_plugins(client)
        elapsed = time.perf_counter() - start
    assert [p["name"] for p in out] == ["a", "b.plg"]  # dedup still uses known_names
    assert elapsed < DELAY * 1.9  # serial would be >= 2 * DELAY


async def test_plugins_installed_failure_degrades_and_plugins_failure_raises():
    resp = {
        queries.PLUGINS: _data({"plugins": []}),
        queries.INSTALLED_UNRAID_PLUGINS: _gql_error("nope"),
    }
    async with _Mock(resp, delay=0.01) as client:
        assert await misc.fetch_plugins(client) == []
    resp = {
        queries.PLUGINS: _gql_error('Cannot query field "plugins" on type "Query".'),
        queries.INSTALLED_UNRAID_PLUGINS: _data({"installedUnraidPlugins": []}),
    }
    async with _Mock(resp, delay=0.01) as client:
        with pytest.raises(ToolError):
            await misc.fetch_plugins(client)


async def test_system_info_runs_queries_concurrently():
    resp = {
        queries.SYSTEM_INFO: _data({"info": {"os": {"hostname": "tower"}}}),
        queries.FLASH: _data({"flash": {"guid": "g", "vendor": "v", "product": "p"}}),
    }
    async with _Mock(resp) as client:
        start = time.perf_counter()
        out = await system.fetch_system_info(client)
        elapsed = time.perf_counter() - start
    assert out["flash"]["guid"] == "g"
    assert elapsed < DELAY * 1.9


async def test_gather_all_cancels_siblings_on_failure():
    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def fail():
        await asyncio.sleep(0.01)
        raise ValueError("x")

    with pytest.raises(ValueError):
        await _base.gather_all(slow(), fail())
    assert cancelled.is_set()
