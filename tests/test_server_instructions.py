"""INSTRUCTIONS must stay short and consistent with the registered tool names."""

from __future__ import annotations

import asyncio
import fnmatch
import re

from mcp.client import Client

from tests.conftest import make_settings
from unraid_mcp.server import INSTRUCTIONS, build_server

ALWAYS_LOAD_KEY = "anthropic/alwaysLoad"
ALWAYS_LOAD = {"get_health_summary", "list_warnings_and_alerts", "get_system_info"}


def _tools(**flags):
    async def go():
        mcp = build_server(make_settings(**flags))
        async with Client(mcp) as session:
            return list((await session.list_tools()).tools)

    return asyncio.run(go())


def _mentioned(known: set[str]) -> list[str]:
    """Tool references in INSTRUCTIONS: snake_case names, ``*globs*``, names with
    digits, and any bare word that is itself a registered tool name."""
    out = []
    for t in re.findall(r"[A-Za-z0-9_*]+", INSTRUCTIONS):
        if "_" in t or "*" in t or any(c.isdigit() for c in t) or t in known:
            out.append(t)
    return out


def _matches(ref: str, names: set[str]) -> bool:
    return any(fnmatch.fnmatchcase(n, ref) for n in names)


def test_instructions_within_budget():
    assert len(INSTRUCTIONS) <= 1200


# References that intentionally match mutation tools only (checked against the
# mutations-enabled server instead of the read-only one). Keep empty unless needed.
MUTATION_ONLY_REFS: set[str] = set()


def unresolved_refs(refs: list[str], read: set[str], full: set[str]) -> list[str]:
    """References that do not resolve where they must: on the read-only tool set,
    or (explicit MUTATION_ONLY_REFS only) on the mutations-enabled set."""
    bad = []
    for ref in refs:
        pool = full if ref in MUTATION_ONLY_REFS else read
        if not _matches(ref, pool):
            bad.append(ref)
    return bad


def test_mentioned_tools_exist():
    read = {t.name for t in _tools()}
    full = {t.name for t in _tools(allow_mutations=True, allow_dangerous=True)}
    mentioned = _mentioned(full)
    assert {"get_health_summary", "*docker*", "whoami"} <= set(mentioned)
    assert unresolved_refs(mentioned, read, full) == []


def test_drift_check_catches_removed_read_tool():
    read = {t.name for t in _tools()}
    full = {t.name for t in _tools(allow_mutations=True, allow_dangerous=True)}
    # *vm* would still match mutation tools (vm_power...); it must still fail.
    assert any("vm" in n for n in full - read)
    assert unresolved_refs(["*vm*"], read - {"list_vms"}, full) == ["*vm*"]


def test_every_read_tool_is_covered():
    read = {t.name for t in _tools()}
    full = {t.name for t in _tools(allow_mutations=True, allow_dangerous=True)}
    refs = _mentioned(full)
    uncovered = sorted(n for n in read if not any(fnmatch.fnmatchcase(n, r) for r in refs))
    assert not uncovered, f"read tools not named or globbed in INSTRUCTIONS: {uncovered}"


def test_always_load_meta_only_on_core_tools():
    tools = _tools(allow_mutations=True, allow_dangerous=True)
    flagged = {t.name for t in tools if (t.meta or {}).get(ALWAYS_LOAD_KEY)}
    assert flagged == ALWAYS_LOAD
