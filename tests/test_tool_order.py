"""tools/list order is explicit (alphabetical) and inputs reject unknown args."""

from __future__ import annotations

import asyncio

import pytest
import respx
from mcp.client import Client

from tests.conftest import make_settings
from unraid_mcp.server import build_server

CONFIGS = {
    "read_only": {},
    "mutations": {"allow_mutations": True},
    "dangerous": {"allow_mutations": True, "allow_dangerous": True},
    "everything": {"allow_mutations": True, "allow_dangerous": True, "allow_raw_query": True},
}


def _tools(**flags):
    async def go():
        async with Client(build_server(make_settings(**flags))) as session:
            return list((await session.list_tools()).tools)

    return asyncio.run(go())


@pytest.mark.parametrize("flags", CONFIGS.values(), ids=CONFIGS.keys())
def test_tools_list_is_alphabetical_and_stable(flags):
    first = [t.name for t in _tools(**flags)]
    assert first == sorted(first)
    assert first == [t.name for t in _tools(**flags)]


@pytest.mark.parametrize("flags", CONFIGS.values(), ids=CONFIGS.keys())
def test_every_input_schema_forbids_additional_properties(flags):
    for t in _tools(**flags):
        schema = t.input_schema
        assert schema.get("additionalProperties") is False, t.name
        assert "title" not in schema, t.name


@respx.mock(assert_all_called=False)
def test_unknown_argument_is_rejected_without_http(respx_mock):
    async def go():
        async with Client(build_server(make_settings(allow_mutations=True))) as session:
            tools = (await session.list_tools()).tools
            return [await session.call_tool(t.name, {"bogus_arg": 1}) for t in tools]

    results = asyncio.run(go())
    assert results
    for r in results:
        assert r.is_error
        assert "bogus_arg" in r.content[0].text
        assert "Extra inputs are not permitted" in r.content[0].text
    assert len(respx_mock.calls) == 0
