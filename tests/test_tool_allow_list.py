"""UNRAID_MCP_TOOLS: an allow-list that narrows, and never widens, the tier flags."""

from __future__ import annotations

import asyncio
import logging
from itertools import product

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from tests.conftest import URL, make_settings
from unraid_mcp import cli
from unraid_mcp.config import load_settings
from unraid_mcp.errors import UnraidConfigError
from unraid_mcp.server import build_server
from unraid_mcp.tools import _MODULES, misc, tool_catalog

ENV_KEY = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"
EMBER = "refresh_docker_digests,update_docker_container,list_docker_containers"
FLAGS = ("allow_mutations", "allow_dangerous", "allow_raw_query")
ALL_FLAGS = dict.fromkeys(FLAGS, True)
FLAG_COMBOS = [dict(zip(FLAGS, combo, strict=True)) for combo in product((False, True), repeat=3)]


@pytest.fixture(autouse=True)
def api():
    """Answer the lifespan's startup probes offline; the route records every request."""
    with respx.mock(assert_all_called=False) as router:
        yield router.post(URL).mock(return_value=httpx.Response(500))


def _unfiltered_names(**flags) -> set[str]:
    """The tier gate as it stood before the allow-list, kept here as the baseline."""
    settings = make_settings(**flags)
    mcp = MCPServer("baseline")
    for module in _MODULES:
        module.register(mcp, settings)
        if settings.allow_mutations and hasattr(module, "register_mutations"):
            module.register_mutations(mcp, settings)
        if (
            settings.allow_mutations
            and settings.allow_dangerous
            and hasattr(module, "register_dangerous")
        ):
            module.register_dangerous(mcp, settings)
    if settings.allow_raw_query:
        misc.register_raw_query(mcp, settings)
    return {t.name for t in mcp._tool_manager.list_tools()}


def _published(**settings) -> list[str]:
    """Tool names in ``tools/list`` order, through the in-memory client."""

    async def go():
        async with Client(build_server(make_settings(**settings))) as session:
            return [t.name for t in (await session.list_tools()).tools]

    return asyncio.run(go())


# ── Parsing ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["", "   ", "\t\n"])
def test_blank_means_no_filter(raw):
    assert make_settings(tools=raw).allowed_tools is None


def test_unset_means_no_filter():
    assert make_settings().allowed_tools is None


def test_names_ignore_whitespace_empties_and_repeats():
    settings = make_settings(tools=" whoami ,,list_disks,\tlist_shares , whoami,")
    assert settings.allowed_tools == {"whoami", "list_disks", "list_shares"}


@pytest.mark.parametrize("raw", [",", " , ,", ",,,"])
def test_a_value_with_no_names_is_rejected(raw):
    with pytest.raises(UnraidConfigError, match="UNRAID_MCP_TOOLS has no tool names"):
        load_settings(UNRAID_API_URL=URL, UNRAID_API_KEY=ENV_KEY, UNRAID_MCP_TOOLS=raw)


# ── Registration ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("flags", FLAG_COMBOS)
@pytest.mark.parametrize("blank", [None, "", "  "])
def test_blank_keeps_the_registered_set_for_every_flag_combination(flags, blank):
    settings = dict(flags) if blank is None else {**flags, "tools": blank}
    assert set(_published(**settings)) == _unfiltered_names(**flags)


def test_catalog_covers_every_tier_whatever_the_flags():
    assert set(tool_catalog(make_settings())) == _unfiltered_names(**ALL_FLAGS)


def test_listed_tools_publish_exactly_those_in_sorted_order():
    assert _published(allow_mutations=True, tools=EMBER) == sorted(EMBER.split(","))


def test_the_list_covers_read_tools():
    assert _published(tools="whoami") == ["whoami"]


def test_a_listed_mutation_stays_off_without_mutations_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="unraid_mcp.tools"):
        assert _published(tools=EMBER) == ["list_docker_containers"]
    warnings = sorted(r.getMessage() for r in caplog.records if r.name == "unraid_mcp.tools")
    assert len(warnings) == 2
    assert "refresh_docker_digests" in warnings[0]
    assert "update_docker_container" in warnings[1]
    assert all("UNRAID_MCP_ALLOW_MUTATIONS=true" in w for w in warnings)


@pytest.mark.parametrize(
    ("flags", "on"),
    [
        ({}, False),
        ({"allow_dangerous": True}, False),
        ({"allow_mutations": True}, False),
        ({"allow_mutations": True, "allow_dangerous": True}, True),
    ],
)
def test_a_listed_dangerous_tool_needs_both_flags(flags, on, caplog):
    with caplog.at_level(logging.WARNING, logger="unraid_mcp.tools"):
        published = _published(tools="remove_docker_container,whoami", **flags)
    assert ("remove_docker_container" in published) is on
    assert "whoami" in published
    warnings = [r.getMessage() for r in caplog.records if "UNRAID_MCP_TOOLS" in r.getMessage()]
    if on:
        assert not warnings
    else:
        assert len(warnings) == 1
        assert "UNRAID_MCP_ALLOW_MUTATIONS=true" in warnings[0]
        assert "UNRAID_MCP_ALLOW_DANGEROUS=true" in warnings[0]


def test_a_listed_raw_query_needs_its_flag(caplog):
    with caplog.at_level(logging.WARNING, logger="unraid_mcp.tools"):
        assert _published(tools="run_graphql_query") == []
    assert "UNRAID_MCP_ALLOW_RAW_QUERY=true" in caplog.text
    assert _published(tools="run_graphql_query", allow_raw_query=True) == ["run_graphql_query"]


def test_unlisted_tools_cannot_be_called(api):
    async def go():
        async with Client(build_server(make_settings(tools="whoami"))) as session:
            before = api.call_count
            result = await session.call_tool("list_disks", {})
            return result, api.call_count - before

    result, sent = asyncio.run(go())
    assert result.is_error
    assert "list_disks" in result.content[0].text
    assert sent == 0


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ("whoami,no_such_tool", "no_such_tool"),
        ("Whoami", "Whoami"),  # matching is case-sensitive
        ("zzz,aaa", "aaa, zzz"),
    ],
)
def test_unknown_names_are_rejected(value, named):
    with pytest.raises(UnraidConfigError, match=named):
        build_server(make_settings(tools=value))


def test_unknown_name_fails_before_any_tier_warning(caplog):
    with caplog.at_level(logging.WARNING), pytest.raises(UnraidConfigError):
        build_server(make_settings(tools="update_docker_container,nope"))
    assert not caplog.records


def test_filtered_tools_still_get_the_post_registration_steps():
    async def go():
        async with Client(build_server(make_settings(tools="get_disk,whoami"))) as session:
            tools = (await session.list_tools()).tools
            return tools, await session.call_tool("get_disk", {"disk_id": "1", "bogus": 1})

    tools, result = asyncio.run(go())
    assert [t.name for t in tools] == ["get_disk", "whoami"]
    assert all(t.input_schema["additionalProperties"] is False for t in tools)
    assert result.is_error
    assert "Unknown argument" in result.content[0].text


def test_resources_and_prompts_are_unaffected():
    async def go(settings):
        async with Client(build_server(settings)) as session:
            resources = {str(r.uri) for r in (await session.list_resources()).resources}
            prompts = {p.name for p in (await session.list_prompts()).prompts}
            return resources, prompts

    assert asyncio.run(go(make_settings(tools="whoami"))) == asyncio.run(go(make_settings()))


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("refresh_docker_digests", {}),
        ("update_docker_container", {"container_id": "plex"}),
    ],
)
def test_a_listed_mutation_still_refuses_without_confirm(api, tool, args):
    async def go():
        async with Client(build_server(make_settings(allow_mutations=True, tools=EMBER))) as s:
            before = api.call_count
            result = await s.call_tool(tool, args)
            return result, api.call_count - before

    result, sent = asyncio.run(go())
    assert result.is_error
    assert "confirm=true" in result.content[0].text
    assert sent == 0


# ── Startup through the CLI ──────────────────────────────────────────────


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UNRAID_API_URL", URL)
    monkeypatch.setenv("UNRAID_API_KEY", ENV_KEY)
    for var in ("UNRAID_MCP_TRANSPORT", "UNRAID_MCP_ALLOW_MUTATIONS", "UNRAID_MCP_ALLOW_RAW_QUERY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # no developer .env
    return monkeypatch


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("whoami,no_such_tool", "unknown tool(s): no_such_tool"),
        (",", "no tool names"),
    ],
)
def test_bad_values_fail_startup_with_a_clean_message(cli_env, capsys, value, message):
    cli_env.setenv("UNRAID_MCP_TOOLS", value)
    served = []
    cli_env.setattr(MCPServer, "run", lambda *a, **k: served.append(a))
    assert cli.main() == 1
    err = capsys.readouterr().err
    assert message in err
    assert "UNRAID_MCP_TOOLS" in err
    assert "Traceback" not in err
    assert ENV_KEY not in err
    assert not served


def test_a_valid_list_starts(cli_env):
    cli_env.setenv("UNRAID_MCP_TOOLS", "whoami")
    served = []
    cli_env.setattr(MCPServer, "run", lambda self, **k: served.append(self))
    assert cli.main() == 0
    assert [t.name for t in served[0]._tool_manager.list_tools()] == ["whoami"]
