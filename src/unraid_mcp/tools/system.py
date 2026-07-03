"""System information tools."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_system_info
from ._base import READ_ONLY, get_app_context, guarded


async def fetch_system_info(client: UnraidClient) -> dict[str, Any]:
    return shape_system_info(await client.execute(queries.SYSTEM_INFO))


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_system_info(ctx: Context) -> dict[str, Any]:
        """Get Unraid host system information: OS/kernel, CPU, memory layout,
        motherboard, Unraid + API versions, and uptime."""
        info = await guarded(ctx, fetch_system_info)
        # Surface the startup-probed versions explicitly at the top level so
        # agents can self-diagnose capability gaps even if info.versions is
        # absent on this API build.
        app = get_app_context(ctx)
        return {
            **info,
            "api_version": app.api_version,
            "unraid_version": app.unraid_version,
        }
