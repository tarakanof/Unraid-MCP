"""Forbidden-resource translation and the startup read-only-key warning (#179)."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
import respx
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp import queries
from unraid_mcp.errors import UnraidGraphQLError
from unraid_mcp.server import build_server
from unraid_mcp.tools._base import forbidden_error, guarded, is_permission_error

from .conftest import KEY, URL

FORBIDDEN = {
    "errors": [{"message": "Forbidden resource", "extensions": {"code": "FORBIDDEN"}}],
    "data": None,
}
PROBE = {"data": {"info": {"versions": {"core": {"api": "4.20.0", "unraid": "7.2.0"}}}}}


def _me(*roles: str) -> dict:
    return {"data": {"me": {"id": "1", "name": "mcp", "description": None, "roles": list(roles)}}}


def _router(me: dict | Exception, other: dict = FORBIDDEN):
    """Answer the version probe, ``me`` and everything else by query text."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        if query == queries.API_PROBE:
            return httpx.Response(200, json=PROBE)
        if query == queries.ME:
            if isinstance(me, Exception):
                raise me
            return httpx.Response(200, json=me)
        return httpx.Response(200, json=other)

    return handler


def _me_calls(route) -> int:
    return sum(json.loads(c.request.content)["query"] == queries.ME for c in route.calls)


async def test_forbidden_mutation_is_actionable(settings_factory):
    with respx.mock:
        respx.post(URL).mock(side_effect=_router(_me("VIEWER")))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with Client(mcp) as session:
            result = await session.call_tool("refresh_docker_digests", {"confirm": True})
    assert result.is_error is True
    text = result.content[0].text
    assert "lacks permission" in text
    assert "Key roles: VIEWER" in text
    assert "DOCKER update" in text and "ADMIN" in text
    assert KEY not in text


async def test_forbidden_read_is_actionable_without_known_roles(settings_factory):
    """Mutations off: no identity check, so the roles clause is omitted."""
    with respx.mock:
        route = respx.post(URL).mock(side_effect=_router(_me("VIEWER")))
        mcp = build_server(settings_factory(allow_mutations=False))
        async with Client(mcp) as session:
            result = await session.call_tool("list_docker_containers", {})
    assert result.is_error is True
    text = result.content[0].text
    assert "lacks permission" in text
    assert "Key roles" not in text
    assert KEY not in text
    assert _me_calls(route) == 0


async def test_viewer_key_with_mutations_warns_once(settings_factory, caplog):
    with respx.mock, caplog.at_level(logging.WARNING, logger="unraid_mcp.server"):
        route = respx.post(URL).mock(side_effect=_router(_me("VIEWER")))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
            assert ctx.key_roles == ("VIEWER",)
    warnings = [r for r in caplog.records if "read-only role" in r.getMessage()]
    assert len(warnings) == 1
    assert KEY not in caplog.text
    assert _me_calls(route) == 1


async def test_admin_key_with_mutations_does_not_warn(settings_factory, caplog):
    with respx.mock, caplog.at_level(logging.WARNING, logger="unraid_mcp.server"):
        respx.post(URL).mock(side_effect=_router(_me("ADMIN")))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
            assert ctx.key_roles == ("ADMIN",)
    assert "read-only role" not in caplog.text


async def test_mutations_off_skips_identity_check(settings_factory, caplog):
    with respx.mock, caplog.at_level(logging.WARNING, logger="unraid_mcp.server"):
        route = respx.post(URL).mock(side_effect=_router(_me("VIEWER")))
        mcp = build_server(settings_factory(allow_mutations=False))
        async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
            assert ctx.key_roles is None
    assert "read-only role" not in caplog.text
    assert _me_calls(route) == 0


async def test_identity_check_failure_does_not_block_startup(settings_factory):
    for me in (httpx.ConnectError("boom"), FORBIDDEN, {"data": {"me": None}}):
        with respx.mock:
            respx.post(URL).mock(side_effect=_router(me, other=PROBE))
            mcp = build_server(settings_factory(allow_mutations=True))
            async with Client(mcp, raise_exceptions=True) as session:
                result = await session.call_tool("get_system_info", {})
                assert result.is_error is False


async def test_identity_check_is_time_bounded(settings_factory, monkeypatch):
    import asyncio

    from unraid_mcp import server

    monkeypatch.setattr(server, "IDENTITY_PROBE_TIMEOUT_S", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["query"] == queries.ME:
            await asyncio.sleep(5)
        return httpx.Response(200, json=PROBE)

    with respx.mock:
        respx.post(URL).mock(side_effect=slow)
        mcp = build_server(settings_factory(allow_mutations=True))
        async with asyncio.timeout(2):
            async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
                assert ctx.key_roles is None


async def test_malformed_identity_response_does_not_block_startup(settings_factory, caplog):
    """Envelope-valid but malformed ``me`` payloads leave roles unknown."""
    for me in (
        {"data": {"me": "unexpected"}},
        {"data": {"me": {"roles": "VIEWER"}}},
        {"data": {"me": {"roles": [None, 7]}}},
    ):
        with respx.mock:
            respx.post(URL).mock(side_effect=_router(me, other=PROBE))
            mcp = build_server(settings_factory(allow_mutations=True))
            async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server) as ctx:
                assert ctx.key_roles in (None, ())


@pytest.mark.parametrize("roles", [("GUEST",), ("VIEWER", "GUEST"), ("viewer",)])
async def test_read_only_role_sets_warn(settings_factory, caplog, roles):
    with respx.mock, caplog.at_level(logging.WARNING, logger="unraid_mcp.server"):
        respx.post(URL).mock(side_effect=_router(_me(*roles)))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server):
            pass
    warnings = [r for r in caplog.records if "read-only role" in r.getMessage()]
    assert len(warnings) == 1
    assert ", ".join(roles) in warnings[0].getMessage()


async def test_viewer_plus_admin_does_not_warn(settings_factory, caplog):
    with respx.mock, caplog.at_level(logging.WARNING, logger="unraid_mcp.server"):
        respx.post(URL).mock(side_effect=_router(_me("VIEWER", "ADMIN")))
        mcp = build_server(settings_factory(allow_mutations=True))
        async with mcp._lowlevel_server.lifespan(mcp._lowlevel_server):
            pass
    assert "read-only role" not in caplog.text


@pytest.mark.parametrize("extensions", ["oops", ["FORBIDDEN"], 1])
async def test_non_dict_extensions_do_not_raise(mocked_client, extensions):
    body = {"errors": [{"message": "boom", "extensions": extensions}], "data": None}
    exc = UnraidGraphQLError("GraphQL error: boom", errors=body["errors"])
    assert forbidden_error(exc) is False
    assert is_permission_error(exc) is False
    async with mocked_client(httpx.Response(200, json=body)) as (client, _):
        ctx = SimpleNamespace(
            request_context=SimpleNamespace(lifespan_context=SimpleNamespace(client=client))
        )
        with pytest.raises(ToolError, match="boom"):
            await guarded(ctx, lambda c: c.execute("query { x }"))
