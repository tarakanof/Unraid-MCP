"""Tests for the optional read-only raw GraphQL passthrough.

The guard parses the document (graphql-core) and allows only `query` operations,
so it must resist denylist bypasses (leading comments/BOM/commas) and must NOT
false-positive on legitimate queries that merely contain keyword-like text.
"""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp.tools import misc


async def test_raw_query_executes_read_only(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {"info": {"machineId": "x"}}})) as (
        client,
        route,
    ):
        out = await misc.do_raw_query(client, "query { info { machineId } }")
        assert out == {"info": {"machineId": "x"}}
        assert route.call_count == 1


async def test_raw_query_allows_anonymous_shorthand(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {"x": 1}})) as (client, route):
        await misc.do_raw_query(client, "{ array { state } }")
        assert route.call_count == 1


@pytest.mark.parametrize(
    "bad",
    [
        "mutation { array { setState(input: {desiredState: STOP}) { state } } }",
        "  mutation Foo { x }",
        "subscription { arraySubscription { state } }",
        "query { a } mutation Sneaky { b }",
        # Denylist bypasses that a regex anchored on ^/} would miss:
        "# sneaky\nmutation Evil { stopArray }",
        "﻿mutation Evil { stopArray }",
        ",mutation Evil { stopArray }",
        "\n\n   mutation Evil { stopArray }",
    ],
)
async def test_raw_query_rejects_non_queries_without_request(mocked_client, bad):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await misc.do_raw_query(client, bad)
        assert route.call_count == 0


async def test_raw_query_rejects_invalid_graphql_without_request(mocked_client):
    async with mocked_client(httpx.Response(200, json={"data": {}})) as (client, route):
        with pytest.raises(ToolError):
            await misc.do_raw_query(client, "this is not graphql")
        assert route.call_count == 0


@pytest.mark.parametrize(
    "good",
    [
        "query { mutationLog }",  # field merely named like a keyword
        'query { shares(filter: "} mutation") { name } }',  # keyword inside a string literal
        "# a comment\nquery { info { machineId } }",  # leading comment before a query
    ],
)
async def test_raw_query_allows_legit_queries(mocked_client, good):
    async with mocked_client(httpx.Response(200, json={"data": {"ok": 1}})) as (client, route):
        await misc.do_raw_query(client, good)
        assert route.call_count == 1
