"""UPS, network, identity, health-summary, and the optional raw-query tool."""

from __future__ import annotations

import re
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidError
from ..formatting import (
    shape_array_status,
    shape_connect_status,
    shape_me,
    shape_network_interfaces,
    shape_notifications_overview,
    shape_ups,
    summarize_health,
)
from ._base import READ_ONLY, guarded

# Reject anything that runs an operation other than a query. Matches a
# mutation/subscription operation either at the very start or as a second
# operation after a previous one closes with "}".
_NON_QUERY_OP = re.compile(r"(^|})\s*(mutation|subscription)\b", re.IGNORECASE)


async def fetch_ups(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_ups(await client.execute(queries.UPS_DEVICES))


async def fetch_network_interfaces(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_network_interfaces(await client.execute(queries.NETWORK_INTERFACES))


async def fetch_me(client: UnraidClient) -> dict[str, Any]:
    return shape_me(await client.execute(queries.ME))


async def fetch_connect_status(client: UnraidClient) -> dict[str, Any]:
    return shape_connect_status(await client.execute(queries.CONNECT_STATUS))


async def _safe(client: UnraidClient, query: str, shaper, default):
    """Run an optional query; on any Unraid error fall back to a default so the
    health summary degrades gracefully when a feature isn't available."""
    try:
        return shaper(await client.execute(query))
    except UnraidError:
        return default


async def fetch_health(client: UnraidClient) -> dict[str, Any]:
    array = shape_array_status(await client.execute(queries.ARRAY_STATUS))
    ups = await _safe(client, queries.UPS_DEVICES, shape_ups, [])
    overview = await _safe(client, queries.NOTIFICATIONS_OVERVIEW, shape_notifications_overview, {})
    return summarize_health(array, ups, overview)


async def do_raw_query(
    client: UnraidClient, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    if _NON_QUERY_OP.search(query.strip()):
        raise ToolError(
            "run_graphql_query only accepts read-only queries; "
            "mutations and subscriptions are not allowed."
        )
    return await client.execute(query, variables)


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_ups_status(ctx: Context) -> list[dict[str, Any]]:
        """Get UPS devices: status, battery charge/runtime/health, and load/voltage."""
        return await guarded(ctx, fetch_ups)

    @mcp.tool(annotations=READ_ONLY)
    async def list_network_interfaces(ctx: Context) -> list[dict[str, Any]]:
        """List network interfaces with MAC, speed, state, and IPv4/IPv6 addresses."""
        return await guarded(ctx, fetch_network_interfaces)

    @mcp.tool(annotations=READ_ONLY)
    async def whoami(ctx: Context) -> dict[str, Any]:
        """Show the authenticated API user and its roles — useful to confirm the key's scope."""
        return await guarded(ctx, fetch_me)

    @mcp.tool(annotations=READ_ONLY)
    async def get_connect_status(ctx: Context) -> dict[str, Any]:
        """Get Unraid registration/license and remote-access (Connect) status."""
        return await guarded(ctx, fetch_connect_status)

    @mcp.tool(annotations=READ_ONLY)
    async def get_health_summary(ctx: Context) -> dict[str, Any]:
        """Compact health roll-up for triage: array state, capacity, any unhealthy disks,
        parity-check status, UPS state, and unread notification counts."""
        return await guarded(ctx, fetch_health)


def register_raw_query(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def run_graphql_query(
        ctx: Context, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an arbitrary READ-ONLY GraphQL query against the Unraid API (escape hatch
        for fields without a dedicated tool). Mutations and subscriptions are rejected."""
        return await guarded(ctx, do_raw_query, query, variables)
