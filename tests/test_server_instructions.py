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


def _mentioned() -> list[str]:
    """Tool names (snake_case) and ``*prefix*`` globs mentioned in INSTRUCTIONS."""
    return [
        t for t in re.findall(r"\*?[a-z]+(?:_[a-z]+)*\*?", INSTRUCTIONS) if "_" in t or "*" in t
    ]


def test_instructions_within_budget():
    assert len(INSTRUCTIONS) <= 1200


def test_mentioned_tools_exist():
    mentioned = _mentioned()
    assert "get_health_summary" in mentioned and "*docker*" in mentioned
    names = {t.name for t in _tools(allow_mutations=True, allow_dangerous=True)}
    for ref in mentioned:
        assert any(fnmatch.fnmatchcase(n, ref) for n in names), f"{ref!r} matches no tool"


def test_always_load_meta_only_on_core_tools():
    tools = _tools(allow_mutations=True, allow_dangerous=True)
    flagged = {t.name for t in tools if (t.meta or {}).get(ALWAYS_LOAD_KEY)}
    assert flagged == ALWAYS_LOAD
