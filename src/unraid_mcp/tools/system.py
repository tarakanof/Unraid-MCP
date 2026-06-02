"""System information tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_system_info
from ._base import READ_ONLY, guarded


async def fetch_system_info(client: UnraidClient) -> dict[str, Any]:
    return shape_system_info(await client.execute(queries.SYSTEM_INFO))


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_system_info(ctx: Context) -> dict[str, Any]:
        """Get Unraid host system information: OS/kernel, CPU, memory layout,
        motherboard, Unraid + API versions, and uptime."""
        return await guarded(ctx, fetch_system_info)
