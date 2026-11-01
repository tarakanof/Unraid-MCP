"""Tests for the async GraphQL client (mocked with respx)."""

from __future__ import annotations

import json

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
KEY = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"


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


async def test_partial_response_returns_data(caplog):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"x": 5, "y": None},
                    "errors": [{"message": f"field y unavailable {KEY}"}],
                },
            )
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            data = await client.execute("query { x y }")
        assert data == {"x": 5, "y": None}
        assert "GraphQL returned partial errors" in caplog.text
        assert "field y unavailable" in caplog.text
        assert any(record.levelname == "WARNING" for record in caplog.records)
        assert KEY not in caplog.text


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


@pytest.mark.parametrize(
    "payload",
    [[], "str", None, {"data": []}, {"data": "str"}, {"errors": {}}],
)
async def test_invalid_graphql_envelope_raises_server_error(payload):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, content=json.dumps(payload)))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidServerError, match="Invalid GraphQL envelope") as exc:
                await client.execute("query { x }")
        assert KEY not in str(exc.value)
        assert "server logs" in str(exc.value)


@pytest.mark.parametrize("data", [None, {}, {"array": None}, {"x": None, "y": None}])
async def test_graphql_errors_without_populated_fields_are_fatal(data):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": data, "errors": [{"message": "boom"}]})
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError, match="boom"):
                await client.execute("query { x }")


async def test_non_dict_graphql_errors_are_coerced_and_redacted():
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"errors": ["x", f"rejected {KEY}", 42, None]})
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError) as exc:
                await client.execute("query { x }")
        assert exc.value.errors == [
            {"message": "x"},
            {"message": "rejected ***REDACTED***"},
            {"message": "42"},
            {"message": "None"},
        ]
        assert KEY not in str(exc.value)


async def test_null_errors_treated_as_empty():
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"data": {"a": 1}, "errors": None})
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            assert await client.execute("query { a }") == {"a": 1}


@pytest.mark.parametrize("secret", [KEY, "bearer-token-1234567890123456789012"])
async def test_execute_redacts_nested_data_and_partial_error_logs(secret, caplog):
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"server": [{"apikey": secret}, None]},
                    "errors": [{"message": f"partial {secret}", "extensions": {"echo": [secret]}}],
                },
            )
        )
        async with httpx.AsyncClient() as http:
            client = UnraidClient(URL, KEY, http, bearer_token=secret)
            result = await client.execute("query { server { apikey } }")
    assert result == {"server": [{"apikey": "***REDACTED***"}, None]}
    assert secret not in caplog.text
    assert "***REDACTED***" in caplog.text


async def test_execute_redacts_bearer_token_in_error_details():
    from pydantic import SecretStr

    token = "bearer-token-1234567890123456789012"
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": None,
                    "errors": [
                        {"message": f"rejected {KEY} {token}", "extensions": {"echo": [token, KEY]}}
                    ],
                },
            )
        )
        async with httpx.AsyncClient() as http:
            client = UnraidClient(URL, KEY, http, bearer_token=SecretStr(token))
            with pytest.raises(UnraidGraphQLError) as exc:
                await client.execute("query { server { apikey } }")
    assert token not in str(exc.value)
    assert KEY not in str(exc.value)
    assert exc.value.errors[0]["extensions"]["echo"] == ["***REDACTED***", "***REDACTED***"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": f"https://{KEY}.local/graphql"}),
        httpx.Response(500),
        httpx.Response(400),
        httpx.Response(200, text="not json"),
        httpx.ConnectTimeout(KEY),
        httpx.ConnectError(KEY),
    ],
)
async def test_execute_redacts_secrets_in_all_mapped_error_messages(response):
    token = "bearer-token-1234567890123456789012"
    with respx.mock:
        route = respx.post(URL)
        if isinstance(response, Exception):
            route.mock(side_effect=response)
        else:
            route.mock(return_value=response)
        async with httpx.AsyncClient() as http:
            client = UnraidClient(URL, KEY, http, host_label=f"{KEY}.{token}", bearer_token=token)
            with pytest.raises((UnraidConnectionError, UnraidServerError)) as exc:
                await client.execute("query { server { apikey } }")
    assert KEY not in str(exc.value)
    assert token not in str(exc.value)
    assert "***REDACTED***" in str(exc.value)


@pytest.mark.parametrize("partial", [False, True])
async def test_execute_with_errors_returns_redacted_errors(partial):
    payload = {"data": {"x": 5}}
    if partial:
        payload["errors"] = [
            {"message": f"rejected key {KEY}", "path": ["y"], "extensions": {"detail": KEY}}
        ]
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            data, errors = await client.execute_with_errors("query { x y }", {"a": 1})
    assert data == {"x": 5}
    assert KEY not in str(errors)
    if partial:
        assert errors == [
            {
                "message": "rejected key ***REDACTED***",
                "path": ["y"],
                "extensions": {"detail": "***REDACTED***"},
            }
        ]
    else:
        assert errors == []


async def test_default_timeout_used_for_normal_calls():
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        async with httpx.AsyncClient(timeout=30.0) as http:
            await (await _client(http)).execute("query { x }")
        assert route.calls.last.request.extensions["timeout"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }


async def test_per_call_timeout_overrides_client_default():
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200, json={"data": {}}))
        async with httpx.AsyncClient(timeout=30.0) as http:
            await (await _client(http)).execute("mutation { x }", timeout=600.0)
        assert route.calls.last.request.extensions["timeout"]["read"] == 600.0


async def test_timeout_on_long_call_says_may_still_be_running():
    with respx.mock:
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidConnectionError) as exc:
                await client.execute("mutation { x }", timeout=600.0)
        assert "may still be running" in str(exc.value)
        assert KEY not in str(exc.value)


async def test_timeout_on_normal_call_does_not_claim_still_running():
    with respx.mock:
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(UnraidConnectionError) as exc:
                await (await _client(http)).execute("query { x }")
        assert "may still be running" not in str(exc.value)


# ── HTTP 400 GraphQL errors (issue #195): bodies captured from API 4.37.9 ────

BAD_FIELD = {
    "errors": [
        {
            "message": 'Cannot query field "nope" on type "Info".',
            "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
        }
    ]
}
ALIAS_LIMIT = {
    "errors": [
        {
            "message": "Syntax Error: Aliases limit of 100 exceeded, found 101.",
            "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
        }
    ]
}
INTROSPECTION = {
    "errors": [
        {
            "message": "GraphQL introspection is not allowed, but the current request is "
            "for introspection.",
            "extensions": {"code": "INTROSPECTION_DISABLED"},
        }
    ]
}


@pytest.mark.parametrize("body", [BAD_FIELD, ALIAS_LIMIT, INTROSPECTION])
async def test_http_400_with_errors_raises_graphql_error(body):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(400, json=body))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError) as exc:
                await client.execute("query { info { nope } }")
    assert body["errors"][0]["message"] in str(exc.value)
    assert exc.value.errors == body["errors"]


async def test_http_400_with_errors_and_data_still_raises():
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(400, json={"data": {"ok": 1}, **BAD_FIELD})
        )
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError):
                await client.execute("query { ok }")


async def test_http_400_redacts_secrets_in_errors():
    body = {"errors": [{"message": f"bad {KEY}", "extensions": {"echo": KEY}}]}
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(400, json=body))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidGraphQLError) as exc:
                await client.execute("query { ok }")
    assert KEY not in str(exc.value)
    assert exc.value.errors[0]["extensions"]["echo"] == "***REDACTED***"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400),
        httpx.Response(400, text="not json"),
        httpx.Response(400, json={"errors": []}),
        httpx.Response(400, json=["x"]),
        httpx.Response(429, text="slow down"),
    ],
)
async def test_other_4xx_keep_unexpected_http_error(response):
    with respx.mock:
        respx.post(URL).mock(return_value=response)
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidServerError, match=f"Unexpected HTTP {response.status_code}"):
                await client.execute("query { ok }")


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_statuses_win_over_errors_body(status):
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(status, json=BAD_FIELD))
        async with httpx.AsyncClient() as http:
            client = await _client(http)
            with pytest.raises(UnraidAuthError):
                await client.execute("query { ok }")
