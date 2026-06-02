"""User-share tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_shares
from ._base import READ_ONLY, guarded


async def fetch_shares(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_shares(await client.execute(queries.SHARES))


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def list_shares(ctx: Context) -> list[dict[str, Any]]:
        """List Unraid user shares with free/used/total sizes, comment, allocator and cache mode."""
        return await guarded(ctx, fetch_shares)
