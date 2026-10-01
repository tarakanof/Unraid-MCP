"""Tests for the optional read-only raw GraphQL passthrough.

The guard parses the document (graphql-core) and allows only `query` operations,
so it must resist denylist bypasses (leading comments/BOM/commas) and must NOT
false-positive on legitimate queries that merely contain keyword-like text.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp.server import build_server
from unraid_mcp.tools import misc

from .conftest import KEY, URL


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


@pytest.mark.parametrize("secret", [KEY, "bearer-token-1234567890123456789012"])
async def test_run_graphql_query_redacts_nested_tool_output(settings_factory, secret):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"data": {"server": {"apikey": [secret, {"nested": secret}]}}}
            )
        )
        server = build_server(
            settings_factory(
                allow_raw_query=True, bearer_token="bearer-token-1234567890123456789012"
            )
        )
        async with Client(server) as session:
            result = await session.call_tool(
                "run_graphql_query", {"query": "query { server { apikey } }"}
            )
    assert not result.is_error
    assert secret not in str(result)
    assert "***REDACTED***" in str(result)


@pytest.mark.parametrize("data", [None, {}])
async def test_raw_query_empty_data(mocked_client, data):
    async with mocked_client(httpx.Response(200, json={"data": data})) as (client, route):
        assert await misc.do_raw_query(client, "query { server { apikey } }") == {}
        assert route.call_count == 1


async def test_run_graphql_query_redacts_tool_error_output(settings_factory):
    token = "bearer-token-1234567890123456789012"
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"data": None, "errors": [{"message": f"rejected {KEY} {token}"}]}
            )
        )
        server = build_server(settings_factory(allow_raw_query=True, bearer_token=token))
        async with Client(server) as session:
            result = await session.call_tool(
                "run_graphql_query", {"query": "query { server { apikey } }"}
            )
    assert result.is_error
    assert KEY not in str(result)
    assert token not in str(result)
    assert "***REDACTED***" in str(result)


@pytest.mark.parametrize("which", ["key", "token"])
async def test_run_graphql_query_parse_error_redacts_secrets(settings_factory, which):
    token = "bearertoken1234567890123456789012"
    secret = KEY if which == "key" else token
    server = build_server(settings_factory(allow_raw_query=True, bearer_token=token))
    async with Client(server) as session:
        result = await session.call_tool("run_graphql_query", {"query": f"query {{ a }} q{secret}"})
    assert result.is_error
    assert secret not in str(result)
    assert "***REDACTED***" in str(result)


async def test_run_graphql_query_redacts_numeric_secret(settings_factory):
    token = "12345678901234567890123456789012"
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"n": int(token), "ok": 42}})
        )
        server = build_server(settings_factory(allow_raw_query=True, bearer_token=token))
        async with Client(server) as session:
            result = await session.call_tool("run_graphql_query", {"query": "query { n ok }"})
    assert not result.is_error
    assert token not in str(result)
    assert "***REDACTED***" in str(result)
    assert "42" in str(result)
