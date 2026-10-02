"""User-share tools."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_shares
from ._base import DETAILS, READ_ONLY, Detail, contains_ci, guarded, require_choice, select_detail

CONCISE_SHARE_KEYS = ("name", "free", "used", "size")


async def fetch_shares(
    client: UnraidClient, *, name: str | None = None, detail: str = "full"
) -> list[dict[str, Any]]:
    require_choice("detail", detail, DETAILS)
    shares = [
        s
        for s in shape_shares(await client.execute(queries.SHARES))
        if contains_ci(name, s["name"])
    ]
    return select_detail(shares, CONCISE_SHARE_KEYS, detail)


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="List Shares", annotations=READ_ONLY)
    async def list_shares(
        ctx: Context, name: str | None = None, detail: Detail = "concise"
    ) -> list[dict[str, Any]]:
        """List Unraid user shares. Filter by name (case-insensitive substring)
        before listing everything. detail="concise" returns name, free, used,
        size; "full" adds comment, allocator, cache mode and (when set) include,
        exclude, split_level, floor, encryption_status."""
        return await guarded(ctx, fetch_shares, name=name, detail=detail)
