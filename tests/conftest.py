"""Shared test fixtures: a respx-mocked GraphQL client and a Settings factory."""

from __future__ import annotations

import contextlib

import httpx
import pytest
import respx

from unraid_mcp.client import UnraidClient
from unraid_mcp.config import Settings

URL = "https://tower.local/graphql"
KEY = "supersecretkey123"


@contextlib.asynccontextmanager
async def _mocked_client(responses):
    """Yield ``(client, route)`` with the GraphQL endpoint mocked.

    ``responses`` is a single ``httpx.Response`` (returned every call) or a list
    consumed in order (``side_effect``) for multi-request flows.
    """
    with respx.mock:
        route = respx.post(URL)
        if isinstance(responses, list):
            route.mock(side_effect=responses)
        else:
            route.mock(return_value=responses)
        async with httpx.AsyncClient() as http:
            yield UnraidClient(URL, KEY, http, host_label="tower.local"), route


@pytest.fixture
def mocked_client():
    return _mocked_client


def make_settings(**overrides) -> Settings:
    base = {"api_url": URL, "api_key": KEY}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture
def settings_factory():
    return make_settings
