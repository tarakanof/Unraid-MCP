"""Read tools return one compact text block without null-valued keys;
structuredContent is unchanged (#156)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pydantic_core
import pytest
import respx
from mcp.client import Client
from mcp.types import CallToolResult, TextContent

from tests.compact_fixtures import CASES
from tests.conftest import URL, make_settings
from tests.test_tool_contracts import _ALL_FLAGS, READ_CASES, _session
from unraid_mcp.server import build_server
from unraid_mcp.tools._base import CompactFuncMetadata, compact_result, compact_text

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "structured_golden.json").read_text())


def _null_keys(value, path="$"):
    """Paths of dict keys whose value is null (list positions don't count)."""
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out += [f"{path}.{k}"] if v is None else _null_keys(v, f"{path}.{k}")
        return out
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _null_keys(v, f"{path}[{i}]")]
    return []


def _assert_compact(result: CallToolResult) -> object:
    """Exactly one text block of compact JSON with no null-valued keys."""
    assert result.is_error is False, result.content
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, TextContent)
    text = block.text
    assert "\n" not in text
    parsed = json.loads(text)
    # Canonical compact form: no indentation, no separator padding.
    assert text == json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    assert _null_keys(parsed) == []
    return parsed


def _prune_expected(value):
    """Independent reference for the pruning rule: drop null-valued keys only."""
    if isinstance(value, dict):
        return {k: _prune_expected(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_prune_expected(v) for v in value]
    return value


# ── Pure helper ──────────────────────────────────────────────────────────────


def test_compact_text_drops_only_null_keys():
    value = {
        "a": None,
        "b": {"c": None, "d": []},
        "e": [None, {}, {"f": None}, 1],
        "g": 0,
        "h": False,
        "i": "",
        "j": {"k": {"l": None}},
        "lines": [],
        "m": "ü",
    }
    assert compact_text(value) == (
        '{"b":{"d":[]},"e":[null,{},{},1],"g":0,"h":false,"i":"","j":{"k":{}},"lines":[],"m":"ü"}'
    )


def test_compact_text_preserves_list_positions():
    # Leading and interior None must keep per-core indices aligned.
    assert compact_text({"per_core": [None, 50, None, 80]}) == '{"per_core":[null,50,null,80]}'
    # An item whose fields are all null stays present as {}.
    assert compact_text([{"a": None}, {"a": 1}]) == '[{},{"a":1}]'
    assert compact_text([None, {}]) == "[null,{}]"
    assert compact_text({"a": None}) == "{}"


def test_compact_text_without_pruning_keeps_nulls():
    assert compact_text({"a": None, "b": [None]}, prune=False) == '{"a":null,"b":[null]}'


def test_compact_result_unwraps_wrapped_output_and_keeps_structured():
    structured = {"result": [{"a": 1, "b": None}]}
    original = CallToolResult(
        content=[TextContent(type="text", text="old")], structured_content=structured
    )
    out = compact_result(original, wrap_output=True)
    assert out.structured_content is structured
    assert [b.text for b in out.content] == ['[{"a":1}]']
    raw = compact_result(original, wrap_output=True, prune=False)
    assert [b.text for b in raw.content] == ['[{"a":1,"b":null}]']


def test_compact_result_leaves_errors_and_unstructured_alone():
    err = CallToolResult(content=[TextContent(type="text", text="boom")], is_error=True)
    assert compact_result(err, wrap_output=False) is err
    plain = CallToolResult(content=[TextContent(type="text", text="x")])
    assert compact_result(plain, wrap_output=False) is plain


# ── Registration seam ────────────────────────────────────────────────────────


def test_only_read_tools_are_compacted():
    mcp = build_server(make_settings(**_ALL_FLAGS))
    tools = mcp._tool_manager.list_tools()
    read = [t for t in tools if t.annotations and t.annotations.read_only_hint]
    assert read and len(read) < len(tools)
    for tool in tools:
        compact = isinstance(tool.fn_metadata, CompactFuncMetadata)
        assert compact is bool(tool.annotations.read_only_hint), tool.name
        if compact:
            assert tool.fn_metadata.prune is (tool.name != "run_graphql_query"), tool.name
        # Output schema contract is untouched by the swap.
        assert tool.output_schema == tool.fn_metadata.output_schema


# ── End to end ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("key,tool,arguments,data", CASES, ids=[c[0] for c in CASES])
async def test_structured_content_byte_identical_and_text_compact(key, tool, arguments, data, mode):
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(make_settings()), raise_exceptions=True, mode=mode) as s:
            result = await s.call_tool(tool, arguments)
    # Byte-identical to the pre-#156 output captured in the golden file.
    assert json.dumps(result.structured_content) == GOLDEN[key]
    parsed = _assert_compact(result)
    sc = result.structured_content
    payload = sc["result"] if tool.startswith("list_") else sc
    assert parsed == _prune_expected(payload)


async def test_twenty_container_text_much_smaller_than_pretty_size():
    _, tool, arguments, data = CASES[0]
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(make_settings()), raise_exceptions=True) as s:
            result = await s.call_tool(tool, arguments)
    items = result.structured_content["result"]
    assert len(items) == 20
    # Old SDK text: one indent=2 block per item.
    old = sum(len(pydantic_core.to_json(item, indent=2)) for item in items)
    new = len(result.content[0].text)
    assert len(result.content) == 1
    # One block instead of 20, no null keys or indentation: 5,304 vs 8,823
    # chars (60.1%) on this fixture. The 64-hex container ids, ports and kept
    # empty lists are payload pruning must not remove, so the issue's 50%
    # target holds only for null-heavy rows without ports.
    assert new <= old * 0.61, (new, old)


async def test_system_metrics_per_core_keeps_positions():
    data = {
        "metrics": {
            "cpu": {
                "percentTotal": 32.5,
                "cpus": [
                    {"percentTotal": None},
                    {"percentTotal": 50},
                    {"percentTotal": None},
                    {"percentTotal": 80},
                ],
            }
        }
    }
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        async with Client(build_server(make_settings()), raise_exceptions=True) as s:
            result = await s.call_tool("get_system_metrics", {})
    parsed = _assert_compact(result)
    per_core = result.structured_content["cpu"]["per_core"]
    assert len(per_core) == 4
    assert parsed["cpu"]["per_core"] == _prune_expected(per_core)
    assert len(parsed["cpu"]["per_core"]) == 4


async def test_raw_query_text_is_compact_but_unpruned():
    data = {"array": {"disks": [{"name": "disk1", "temp": None}, None, {"name": None}]}}
    with respx.mock:
        respx.post(URL).respond(200, json={"data": data})
        mcp = build_server(make_settings(allow_raw_query=True))
        async with Client(mcp, raise_exceptions=True) as s:
            result = await s.call_tool(
                "run_graphql_query", {"query": "query { array { disks { name temp } } }"}
            )
    assert result.is_error is False
    assert len(result.content) == 1
    sc = result.structured_content
    assert result.content[0].text == json.dumps(sc, separators=(",", ":"), ensure_ascii=False)
    assert '"temp":null' in result.content[0].text


@pytest.mark.parametrize("name", sorted(READ_CASES))
async def test_every_read_tool_returns_one_compact_block(settings_factory, name):
    args, expected = READ_CASES[name]
    empty = httpx.Response(200, json={"data": {}})
    async with _session(settings_factory, empty, **_ALL_FLAGS) as (session, _):
        result = await session.call_tool(name, args)
    if expected not in {"dict", "list"}:
        # Intentional ToolError: error results keep the SDK's plain message.
        assert result.is_error is True
        assert expected in result.content[0].text
        return
    if name == "run_graphql_query":
        return  # unpruned by design; covered by test_raw_query_text_is_compact_but_unpruned
    _assert_compact(result)
