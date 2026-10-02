"""Published tool schemas are trimmed of pydantic boilerplate (#157)."""

import json

import pytest
import respx
from jsonschema import Draft202012Validator
from mcp.client import Client

from unraid_mcp.server import build_server
from unraid_mcp.tools._schema import (
    trim_description,
    trim_input_schema,
    trim_output_schema,
)

URL = "https://tower.local/graphql"
ALL_FLAGS = {"allow_mutations": True, "allow_dangerous": True, "allow_raw_query": True}

# 30% below the read-only tools/list payload before trimming (40,111 chars).
# Measured like the issue: sum of json.dumps(tool.model_dump(exclude_none=True,
# by_alias=True)). Adding a tool may need this raised; bloating schemas must not.
READ_ONLY_TOOLS_LIST_MAX = 28_077


async def _tools(settings):
    with respx.mock:
        respx.post(URL).respond(200, json={"data": {}})
        async with Client(build_server(settings), raise_exceptions=True) as session:
            return (await session.list_tools()).tools


def _subschemas(node, key=None, is_model=True):
    """Yield (schema, property-or-def name, is_model) for every schema node."""
    if not isinstance(node, dict):
        return
    yield node, key, is_model
    for kw in ("items", "additionalProperties", "not"):
        yield from _subschemas(node.get(kw), None, False)
    for kw in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for sub in node.get(kw) or []:
            yield from _subschemas(sub, None, False)
    for name, sub in (node.get("properties") or {}).items():
        yield from _subschemas(sub, name, False)
    for name, sub in (node.get("$defs") or {}).items():
        yield from _subschemas(sub, name, True)


def _schemas(tools):
    for tool in tools:
        yield tool.name, "input", tool.input_schema
        if tool.output_schema is not None:
            yield tool.name, "output", tool.output_schema


async def test_no_published_schema_has_an_auto_title(settings_factory):
    tools = await _tools(settings_factory(**ALL_FLAGS))
    for name, kind, schema in _schemas(tools):
        for node, key, is_model in _subschemas(schema):
            title = node.get("title")
            if title is None:
                continue
            assert key is not None, (name, kind, title)
            assert title != key.replace("_", " ").title(), (name, kind, key)
            if is_model:
                assert title != key, (name, kind, key)


async def test_published_schemas_are_valid_draft_2020_12(settings_factory):
    tools = await _tools(settings_factory(**ALL_FLAGS))
    for name, kind, schema in _schemas(tools):
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # pragma: no cover - message only
            pytest.fail(f"{name} {kind}Schema: {exc}")


async def test_read_only_tools_list_size_is_bounded(settings_factory):
    tools = await _tools(settings_factory())
    total = sum(len(json.dumps(t.model_dump(exclude_none=True, by_alias=True))) for t in tools)
    assert total <= READ_ONLY_TOOLS_LIST_MAX, total


async def test_size_is_one_shared_def(settings_factory):
    tools = await _tools(settings_factory())
    for tool in tools:
        schema = tool.output_schema or {}
        text = json.dumps(schema)
        if '"human": {' not in text:
            continue
        # Size is defined once under $defs and referenced everywhere else.
        assert text.count('"human": {') == 1, tool.name
        assert "human" in schema["$defs"]["Size"]["properties"], tool.name


async def test_descriptions_are_dedented_and_unwrapped(settings_factory):
    tools = await _tools(settings_factory(**ALL_FLAGS))
    for tool in tools:
        assert "\n " not in tool.description, tool.name


def test_publish_hook_reaches_registered_tools(settings_factory):
    # trim_published_tools relies on the SDK's private ToolManager; fail loudly
    # here, not with silently untrimmed schemas, if the SDK moves it.
    mcp = build_server(settings_factory())
    tools = mcp._tool_manager.list_tools()
    assert tools
    for tool in tools:
        assert "title" not in tool.parameters
        assert tool.fn_metadata.output_model is None or "title" not in tool.output_schema


def test_trim_keeps_non_auto_titles_and_data():
    schema = {
        "title": "HealthSummary",
        "type": "object",
        "properties": {
            "title": {"title": "Title", "type": "string", "default": "Title"},
            "warning_count": {"title": "Warning Count", "type": "integer"},
            "custom": {"title": "Something else", "type": "string"},
            "level": {"enum": ["title"], "title": "Level", "type": "string"},
        },
        "required": ["title"],
        "$defs": {"Size": {"title": "Size", "type": "object", "properties": {}}},
    }
    trimmed = trim_input_schema(schema)
    assert "title" not in trimmed
    assert trimmed["properties"]["title"] == {"type": "string", "default": "Title"}
    assert trimmed["properties"]["warning_count"] == {"type": "integer"}
    assert trimmed["properties"]["custom"]["title"] == "Something else"
    assert trimmed["properties"]["level"]["enum"] == ["title"]
    assert trimmed["required"] == ["title"]
    assert trimmed["$defs"]["Size"] == {"type": "object", "properties": {}}
    assert schema["properties"]["warning_count"]["title"] == "Warning Count"  # not mutated


@pytest.mark.parametrize(
    "prop,expected",
    [
        ({"anyOf": [{"type": "string"}, {"type": "null"}]}, {"type": ["string", "null"]}),
        (
            {"anyOf": [{"type": "integer"}, {"type": "number"}, {"type": "null"}]},
            {"type": ["number", "null"]},
        ),
        (
            {"anyOf": [{"enum": ["a", "b"], "type": "string"}, {"type": "null"}]},
            {"enum": ["a", "b", None], "type": ["string", "null"]},
        ),
        (
            {"anyOf": [{"minimum": 1, "type": "integer"}, {"type": "null"}], "default": None},
            {"minimum": 1, "type": ["integer", "null"], "default": None},
        ),
        (
            {"anyOf": [{"$ref": "#/$defs/X"}, {"type": "null"}]},
            {"anyOf": [{"$ref": "#/$defs/X"}, {"type": "null"}]},
        ),
        ({"type": "array", "items": {}}, {"type": "array"}),
        ({"type": "object", "additionalProperties": True}, {"type": "object"}),
        (
            {"type": "object", "additionalProperties": False},
            {"type": "object", "additionalProperties": False},
        ),
    ],
)
def test_output_trim_is_equivalent(prop, expected):
    schema = {"type": "object", "properties": {"p": prop}, "$defs": {"X": {"type": "object"}}}
    trimmed = trim_output_schema(schema)
    assert trimmed["properties"]["p"] == expected
    validator = Draft202012Validator(trimmed)
    original = Draft202012Validator(schema)
    for value in (None, "a", "b", "c", 0, 1, 1.5, [], [1], {}, {"k": 1}):
        doc = {"p": value}
        assert validator.is_valid(doc) == original.is_valid(doc), value


def test_input_trim_keeps_nullable_any_of():
    schema = {
        "type": "object",
        "properties": {"since": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
    }
    assert trim_input_schema(schema) == schema


def test_trim_description():
    doc = """First line
        wrapped here.

        Second paragraph
        - item one
        - item two
          continued
        """
    assert trim_description(doc) == (
        "First line wrapped here.\n\nSecond paragraph\n- item one\n- item two\n  continued"
    )
