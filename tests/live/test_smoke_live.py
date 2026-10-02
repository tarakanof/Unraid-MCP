"""Env-gated live smoke suite against a real Unraid server (issue #32).

These tests hit a real Unraid GraphQL endpoint through a real ``UnraidClient``
and assert *shape invariants only* — never environment-specific values (disk
names, counts, temperatures, capacities, …). They must pass against ANY real
box regardless of its data or API version.

Gating (both must hold, or the whole module is skipped):

* the ``live`` marker is excluded by default via ``addopts = -m "not live"``,
  so plain ``pytest`` / CI never touch the network;
* ``pytestmark`` below adds a ``skipif`` so that even ``pytest -m live`` is a
  no-op unless ``UNRAID_LIVE_TEST=1`` (plus ``UNRAID_API_URL`` /
  ``UNRAID_API_KEY``) is set.

Run it yourself against your box::

    UNRAID_LIVE_TEST=1 UNRAID_API_URL=https://<hash>.myunraid.net/graphql \\
        UNRAID_API_KEY=<your-key> uv run pytest -m live -q

Tools that the box's API version does not support surface as
``UnraidGraphQLError`` (the GraphQL ``errors`` array — see ``errors.py`` and
the "optional field unavailable on this build" note in ``client.py``); those
are treated as capability degradation and ``skip``, not fail.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp.client import UnraidClient
from unraid_mcp.config import load_settings
from unraid_mcp.errors import UnraidGraphQLError
from unraid_mcp.tools import array, docker, misc, notifications, shares, system, vm

# Capability-degrading fetches (system.fetch_services, docker.fetch_docker_updates)
# raise `ToolError("... does not support ...")` on old API builds rather than a
# raw `UnraidGraphQLError` (see the #15 degradation contract in tools/_base.py:
# `feature_unsupported`). `_run` below treats both as the same "unsupported by
# this box" skip condition so these tools can share `LIST_READS` with the rest.

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("UNRAID_LIVE_TEST") != "1",
        reason="live smoke suite is opt-in: set UNRAID_LIVE_TEST=1 (+ UNRAID_API_URL/KEY)",
    ),
]


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def live_client():
    """A real ``UnraidClient`` built from the environment, mirroring how the
    server constructs its client in ``server.build_server``."""
    settings = load_settings()
    async with httpx.AsyncClient(
        verify=settings.tls_verify(),
        timeout=settings.timeout,
        headers={"user-agent": "unraid-mcp-live-test"},
        trust_env=False,
    ) as http:
        yield UnraidClient(
            settings.api_url,
            settings.api_key,
            http,
            host_label=settings.host_for_messages,
        )


# ── Shape helpers ────────────────────────────────────────────────────────────


def _is_int(value: Any) -> bool:
    # bool is a subclass of int; a size in bytes must never be a bool.
    return isinstance(value, int) and not isinstance(value, bool)


def _check_shapes(obj: Any) -> None:
    """Recursively validate the size-dict invariant across any nested structure.

    Every ``{"bytes": ..., "human": ...}`` dict (the package's normalised size
    shape) must have ``bytes`` as ``int|None`` and ``human`` as ``str|None``.
    """
    if isinstance(obj, dict):
        if set(obj.keys()) == {"bytes", "human"}:
            assert _is_int(obj["bytes"]) or obj["bytes"] is None, obj
            assert isinstance(obj["human"], str) or obj["human"] is None, obj
        for value in obj.values():
            _check_shapes(value)
    elif isinstance(obj, list):
        for item in obj:
            _check_shapes(item)


async def _run(fetch: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Call a read fetch, turning a capability-degradation error into a skip so
    tools unsupported by this API version don't fail the run.

    Most reads surface unsupported fields as a raw ``UnraidGraphQLError``.
    Some fetches instead follow issue #15's degrading-fetch pattern and
    translate that into a friendly ``ToolError`` via ``_base.feature_unsupported``
    (e.g. ``system.fetch_metrics``, ``system.fetch_services``,
    ``system.fetch_system_time``, ``misc.fetch_log_files``/``fetch_log_file``,
    ``docker.fetch_docker_updates``), whose message contains "does not support" —
    treat both the same way and skip.
    """
    try:
        return await fetch(*args, **kwargs)
    except UnraidGraphQLError as exc:
        pytest.skip(f"unsupported by this Unraid API version: {exc}")
    except ToolError as exc:
        if "does not support" in str(exc):
            pytest.skip(f"unsupported by this Unraid API version: {exc}")
        raise


# ── Read tools ───────────────────────────────────────────────────────────────

# READ_ONLY fetches whose logic returns a dict.
DICT_READS: list[Callable[..., Awaitable[Any]]] = [
    array.fetch_array_status,
    array.fetch_parity_status,
    misc.fetch_me,
    misc.fetch_connect_status,
    misc.fetch_health,
    notifications.fetch_overview,
    system.fetch_system_info,
    system.fetch_metrics,
    system.fetch_system_time,
    system.fetch_hardware_inventory,
    docker.fetch_docker_port_conflicts,
]

# READ_ONLY fetches whose logic returns a list of dicts.
LIST_READS: list[Callable[..., Awaitable[Any]]] = [
    array.fetch_parity_history,
    array.fetch_disks,
    docker.fetch_containers,
    docker.fetch_docker_networks,
    docker.fetch_docker_updates,
    misc.fetch_ups,
    misc.fetch_network_interfaces,
    misc.fetch_log_files,
    misc.fetch_plugins,
    notifications.fetch_notifications,
    notifications.fetch_warnings_and_alerts,
    shares.fetch_shares,
    system.fetch_services,
    vm.fetch_vms,
]


@pytest.mark.parametrize("fetch", DICT_READS, ids=lambda f: f.__name__)
async def test_read_returns_dict(live_client, fetch):
    result = await _run(fetch, live_client)
    assert isinstance(result, dict)
    _check_shapes(result)


@pytest.mark.parametrize("fetch", LIST_READS, ids=lambda f: f.__name__)
async def test_read_returns_list(live_client, fetch):
    result = await _run(fetch, live_client)
    assert isinstance(result, list)
    for item in result:
        assert isinstance(item, dict)
    _check_shapes(result)


@pytest.mark.parametrize(
    "fetch,kwargs,keys",
    [
        (
            docker.fetch_containers,
            {"state": "RUNNING"},
            {"id", "name", "image", "state", "status", "update_available", "web_ui_url"},
        ),
        (
            array.fetch_disks,
            {},
            {"id", "name", "device", "type", "smart_status", "temp_c", "spinning", "size"},
        ),
        (shares.fetch_shares, {}, {"name", "free", "used", "size"}),
    ],
    ids=lambda v: getattr(v, "__name__", None),
)
async def test_list_concise_and_filters(live_client, fetch, kwargs, keys):
    """#158: concise key set is a subset of full; filters only narrow."""
    full = await _run(fetch, live_client, detail="full")
    concise = await _run(fetch, live_client, detail="concise", **kwargs)
    for item in concise:
        assert set(item) <= keys, item
        if "state" in kwargs:
            assert item["state"] == kwargs["state"]
    assert len(concise) <= len(full)
    _check_shapes(concise)


async def test_get_disk_detail(live_client):
    """list_disks → get_disk: exercise the by-id read against a real disk."""
    disks = await _run(array.fetch_disks, live_client)
    if not disks:
        pytest.skip("box reports no physical disks")
    disk_id = disks[0].get("id")
    if not disk_id:
        pytest.skip("physical disk has no id to look up")
    detail = await _run(array.fetch_disk, live_client, disk_id)
    assert detail is None or isinstance(detail, dict)
    if detail is not None:
        assert "size" in detail
        _check_shapes(detail)
        assert isinstance(detail.get("partitions"), list)
        for part in detail["partitions"]:
            assert set(part["size"]) == {"bytes", "human"}, part


async def test_get_container_detail(live_client):
    """list_docker_containers → get_docker_container by id/name."""
    containers = await _run(docker.fetch_containers, live_client)
    if not containers:
        pytest.skip("box reports no Docker containers")
    identifier = containers[0].get("id") or containers[0].get("name")
    if not identifier:
        pytest.skip("container has no id or name to look up")
    detail = await _run(docker.fetch_container, live_client, identifier)
    assert isinstance(detail, dict)
    _check_shapes(detail)
    if containers[0].get("id"):
        # The listed (short) id round-trips to the same container (#172).
        assert detail["id"] == containers[0]["id"]
        assert ":" not in detail["id"]


async def test_get_container_logs(live_client):
    """list_docker_containers → get_docker_container_logs for the first id."""
    containers = await _run(docker.fetch_containers, live_client)
    if not containers:
        pytest.skip("box reports no Docker containers")
    container_id = containers[0].get("id")
    if not container_id:
        pytest.skip("container has no id to look up")
    try:
        result = await docker.fetch_container_logs(live_client, container_id, tail=10)
    except ToolError as exc:
        if "does not support" in str(exc):
            pytest.skip(f"unsupported by this Unraid API version: {exc}")
        raise
    except UnraidGraphQLError as exc:
        pytest.skip(f"unsupported by this Unraid API version: {exc}")
    assert isinstance(result, dict)
    assert "container_id" in result
    assert isinstance(result["lines"], list)
    for line in result["lines"]:
        assert isinstance(line, dict)
    _check_shapes(result)


async def test_get_docker_container_stats(live_client):
    """One-shot sample of the dockerContainerStats subscription (#65).

    Opens a real short-lived websocket to the box. Shape-invariant only: an
    envelope with a ``containers`` list of per-container dicts whose ids are
    control-char-free. Skips where the subscription is unsupported (old API
    build) or produces no sample (no running containers) — both surface as a
    ``ToolError`` from ``fetch_container_stats``."""
    settings = load_settings()
    try:
        result = await docker.fetch_container_stats(live_client, settings=settings, timeout_s=15.0)
    except ToolError as exc:
        # Old build (feature_unsupported) or no-sample both raise ToolError.
        pytest.skip(f"stats subscription unavailable on this box: {exc}")
    except UnraidGraphQLError as exc:
        pytest.skip(f"unsupported by this Unraid API version: {exc}")
    assert isinstance(result, dict)
    assert isinstance(result["containers"], list)
    assert result["sampled"] == len(result["containers"])
    assert isinstance(result["partial"], bool)
    for c in result["containers"]:
        assert isinstance(c, dict)
        assert isinstance(c["id"], str)
        assert "\x1b" not in c["id"]  # control chars stripped
        assert isinstance(c["cpu_percent"], (int, float))
        assert isinstance(c["mem_percent"], (int, float))
        # Pre-formatted composite strings, not {bytes, human} size dicts.
        for field in ("mem_usage", "net_io", "block_io"):
            assert c[field] is None or isinstance(c[field], str)
    _check_shapes(result)


async def test_read_log_file(live_client):
    """list_log_files → read_log_file: exercise paging against a real log."""
    log_files = await _run(misc.fetch_log_files, live_client)
    if not log_files:
        pytest.skip("box reports no log files")
    preferred = next((f for f in log_files if f.get("path") == "/var/log/syslog"), None)
    path = (preferred or log_files[0]).get("path")
    if not path:
        pytest.skip("listed log file has no path to read")
    detail = await _run(misc.fetch_log_file, live_client, path, 10, None)
    assert isinstance(detail, dict)
    assert detail.get("path") == path
    assert isinstance(detail.get("content"), str)
    assert isinstance(detail.get("total_lines"), int)
    _check_shapes(detail)


async def test_run_graphql_query(live_client):
    """run_graphql_query escape hatch: ``__typename`` is valid in any GraphQL
    schema, so this is version-independent."""
    result = await _run(misc.do_raw_query, live_client, "query { __typename }")
    assert isinstance(result, dict)
    assert result.get("__typename") == "Query"


@pytest.mark.parametrize("tool", ["list_docker_containers", "get_health_summary"])
async def test_compact_text_block_through_server(tool):
    """#156: through the real server, a read tool returns ONE compact text block
    (no newline, null-valued keys omitted) whose content mirrors ``structuredContent``."""
    from mcp.client import Client

    from unraid_mcp.server import build_server
    from unraid_mcp.tools._base import compact_text

    async with Client(build_server(load_settings()), raise_exceptions=True) as session:
        result = await session.call_tool(tool, {})
    assert result.is_error is False
    assert len(result.content) == 1
    text = result.content[0].text
    assert "\n" not in text
    json.loads(text)
    sc = result.structured_content
    assert text == compact_text(sc["result"] if tool.startswith("list_") else sc)
