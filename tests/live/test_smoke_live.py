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

import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mcp.server.fastmcp.exceptions import ToolError

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


class _NoNetworkTransport(httpx.AsyncBaseTransport):
    """Transport that fails the test on ANY request."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"unexpected network I/O: {request.method} {request.url} — "
            "the confirm-refusal path must not touch the server"
        )


@pytest_asyncio.fixture
async def tripwire_client():
    """Client for the confirm-refusal tests: its transport raises on any
    request, so a broken ``require_confirm`` fails the test loudly instead of
    mutating the real box (e.g. stopping the array mid-smoke-test)."""
    async with httpx.AsyncClient(transport=_NoNetworkTransport()) as http:
        yield UnraidClient("https://tripwire.invalid/graphql", "live-smoke-tripwire", http)


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


async def _run(fetch: Callable[..., Awaitable[Any]], *args: Any) -> Any:
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
        return await fetch(*args)
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
    notifications.fetch_notifications,
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


# ── Mutating tools: confirm-refusal path only (no state change) ───────────────

# Each entry invokes a ``do_*`` mutation with confirm=False. ``require_confirm``
# raises ``ToolError`` *before any network I/O*. These run against the
# tripwire client (not the live one), so if that invariant ever regresses the
# test fails on the attempted request instead of mutating the real server.
MUTATION_REFUSALS: list[tuple[str, Callable[[UnraidClient], Awaitable[Any]]]] = [
    ("start_array", lambda c: array.do_start_array(c, confirm=False)),
    ("stop_array", lambda c: array.do_stop_array(c, confirm=False)),
    ("start_parity", lambda c: array.do_start_parity(c, correct=False, confirm=False)),
    ("pause_parity", lambda c: array.do_pause_parity(c, confirm=False)),
    ("resume_parity", lambda c: array.do_resume_parity(c, confirm=False)),
    ("cancel_parity", lambda c: array.do_cancel_parity(c, confirm=False)),
    ("start_container", lambda c: docker.do_start_container(c, "x", confirm=False)),
    ("stop_container", lambda c: docker.do_stop_container(c, "x", confirm=False)),
    ("restart_container", lambda c: docker.do_restart_container(c, "x", confirm=False)),
    ("pause_container", lambda c: docker.do_pause_container(c, "x", confirm=False)),
    ("unpause_container", lambda c: docker.do_unpause_container(c, "x", confirm=False)),
    ("update_container", lambda c: docker.do_update_container(c, "x", confirm=False)),
    ("update_containers", lambda c: docker.do_update_containers(c, ["x"], confirm=False)),
    (
        "archive_notification",
        lambda c: notifications.do_archive_notification(c, "x", confirm=False),
    ),
    ("archive_all", lambda c: notifications.do_archive_all(c, None, confirm=False)),
    ("unread_notification", lambda c: notifications.do_unread_notification(c, "x", confirm=False)),
    (
        "delete_notification",
        lambda c: notifications.do_delete_notification(c, "x", "UNREAD", confirm=False),
    ),
    (
        "archive_notifications",
        lambda c: notifications.do_archive_notifications(c, ["x"], confirm=False),
    ),
    (
        "unarchive_notifications",
        lambda c: notifications.do_unarchive_notifications(c, ["x"], confirm=False),
    ),
    (
        "unarchive_all_notifications",
        lambda c: notifications.do_unarchive_all(c, None, confirm=False),
    ),
    (
        "delete_archived_notifications",
        lambda c: notifications.do_delete_archived_notifications(c, confirm=False),
    ),
    (
        "create_notification",
        lambda c: notifications.do_create_notification(c, "x", "x", "x", "INFO", confirm=False),
    ),
    ("start_vm", lambda c: vm.do_start_vm(c, "x", confirm=False)),
    ("stop_vm", lambda c: vm.do_stop_vm(c, "x", confirm=False)),
    ("pause_vm", lambda c: vm.do_pause_vm(c, "x", confirm=False)),
    ("resume_vm", lambda c: vm.do_resume_vm(c, "x", confirm=False)),
    ("reboot_vm", lambda c: vm.do_reboot_vm(c, "x", confirm=False)),
    ("force_stop_vm", lambda c: vm.do_force_stop_vm(c, "x", confirm=False)),
    ("reset_vm", lambda c: vm.do_reset_vm(c, "x", confirm=False)),
    # Dangerous tier — same tripwire client, never the live one.
    ("mount_array_disk", lambda c: array.do_mount_array_disk(c, "x", confirm=False)),
    ("unmount_array_disk", lambda c: array.do_unmount_array_disk(c, "x", confirm=False)),
    ("clear_disk_statistics", lambda c: array.do_clear_disk_statistics(c, "x", confirm=False)),
    ("add_disk_to_array", lambda c: array.do_add_disk_to_array(c, "x", confirm=False)),
    ("remove_disk_from_array", lambda c: array.do_remove_disk_from_array(c, "x", confirm=False)),
    ("remove_docker_container", lambda c: docker.do_remove_container(c, "x", confirm=False)),
    ("update_all_docker_containers", lambda c: docker.do_update_all_containers(c, confirm=False)),
]


@pytest.mark.parametrize(
    "call", [c for _, c in MUTATION_REFUSALS], ids=[n for n, _ in MUTATION_REFUSALS]
)
async def test_mutation_refuses_without_confirm(tripwire_client, call):
    with pytest.raises(ToolError):
        await call(tripwire_client)
