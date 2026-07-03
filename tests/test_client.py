"""Tests for the async GraphQL client (mocked with respx)."""

from __future__ import annotations

import httpx
import pytest
import respx

from unraid_mcp.client import UnraidClient
from unraid_mcp.errors import (
    UnraidAuthError,
    UnraidConnectionError,
    UnraidGraphQLError,
    UnraidServerError,
)

URL = "https://tower.local/graphql"
KEY = "supersecretkey123"


async def _client(http: httpx.AsyncClient) -> UnraidClient:
    return UnraidClient(URL, KEY, http)


async def test_execute_sends_key_header_and_query_body():
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {"ok": 1}}))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            data = await client.execute("query { ok }", {"a": 1})
        assert data == {"ok": 1}
        req = route.calls.last.request
        assert req.headers["x-api-key"] == KEY
        assert req.headers["content-type"].startswith("application/json")
        import json

        body = json.loads(req.content)
        assert body == {"query": "query { ok }", "variables": {"a": 1}}


async def test_graphql_errors_raise():
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"errors": [{"message": "boom"}], "data": None})
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError) as exc:
                await client.execute("query { x }")
        assert "boom" in str(exc.value)
        assert exc.value.errors == [{"message": "boom"}]


async def test_partial_response_returns_data():
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"data": {"x": 5}, "errors": [{"message": "field y unavailable"}]}
            )
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            data = await client.execute("query { x y }")
        assert data == {"x": 5}


@pytest.mark.parametrize("status,exc_type", [(401, UnraidAuthError), (403, UnraidAuthError)])
async def test_auth_status_codes(status, exc_type):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status, json={}))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(exc_type):
                await client.execute("query { x }")


async def test_server_error_status():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(502, text="bad gateway"))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidServerError):
                await client.execute("query { x }")


async def test_timeout_maps_to_connection_error_without_leaking_key():
    with respx.mock:
        respx.post(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidConnectionError) as exc:
                await client.execute("query { x }")
        assert KEY not in str(exc.value)
        assert "tower.local" in str(exc.value)


async def test_connect_error_maps_to_connection_error():
    with respx.mock:
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidConnectionError) as exc:
                await client.execute("query { x }")
        assert KEY not in str(exc.value)


async def test_redirect_with_location_raises_connection_error():
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                301, headers={"Location": "https://tower.local/graphql?token=secret"}
            )
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidConnectionError) as exc:
                await client.execute("query { x }")
        message = str(exc.value)
        assert "https://tower.local" in message
        assert "UNRAID_API_URL" in message
        assert "token=secret" not in message


async def test_redirect_without_location_raises_clean_error():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(302))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidConnectionError) as exc:
                await client.execute("query { x }")
        assert "302" in str(exc.value)


async def test_non_json_response_raises_server_error():
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidServerError):
                await client.execute("query { x }")


async def test_graphql_error_message_redacts_api_key():
    # Defence in depth: even if an upstream reflected the key into an error,
    # it must not appear in the raised exception.
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json={"errors": [{"message": f"rejected key {KEY}"}], "data": None}
            )
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError) as exc:
                await client.execute("query { x }")
        assert KEY not in str(exc.value)
        assert "***REDACTED***" in str(exc.value)
        # The structured .errors payload must also be scrubbed, not just the message.
        assert KEY not in str(exc.value.errors)
        assert "***REDACTED***" in exc.value.errors[0]["message"]
