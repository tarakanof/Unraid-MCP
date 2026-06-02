"""Tests for the optional read-only raw GraphQL passthrough."""

from __future__ import annotations

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from unraid_mcp.tools import misc


async def test_raw_query_executes_read_only(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {"info": {"machineId": "x"}}})) as (
        client,
        route,
    ):
        out = await misc.do_raw_query(client, "query { info { machineId } }")
        assert out == {"info": {"machineId": "x"}}
        assert route.call_count == 1


@pytest.mark.parametrize(
    "bad",
    [
        "mutation { array { setState(input: {desiredState: STOP}) { state } } }",
        "  mutation Foo { x }",
        "subscription { arraySubscription { state } }",
        "query { a } mutation Sneaky { b }",
    ],
)
async def test_raw_query_rejects_non_queries_without_request(mocked_client, bad):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await misc.do_raw_query(client, bad)
        assert route.call_count == 0


async def test_raw_query_allows_field_named_like_keyword(mocked_client):
    # A selection field whose name merely contains a keyword must not be blocked.
    async with mocked_client(httpx.Response(200, json={"data": {"x": 1}})) as (client, route):
        await misc.do_raw_query(client, "query { mutationLog }")
        assert route.call_count == 1
