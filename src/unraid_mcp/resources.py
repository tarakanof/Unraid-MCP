"""URI-addressable MCP *resources*.

Resources let a client read live server state without spending a tool call.
They intentionally add no new GraphQL: each one reuses the exact ``fetch_*``
function behind the equivalent read tool, so a resource read returns byte-for-
byte the same JSON shape as the tool. The logic lives in the tool modules; this
module only wires those functions onto resource URIs.

A resource read cannot take a ``Context`` parameter the way a tool can (a static
FunctionResource is invoked with no arguments), so we reach the lifespan-shared
:class:`~unraid_mcp.client.UnraidClient` via ``mcp.get_context()`` at read time.
Any domain error is translated into a clean :class:`ResourceError` so an
unreachable box degrades to a readable message instead of an unhandled crash.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError

from .client import UnraidClient
from .errors import UnraidError
from .tools._base import get_client
from .tools.misc import fetch_health
from .tools.system import fetch_system_info

if TYPE_CHECKING:
    from .config import Settings

HEALTH_URI = "unraid://health"
SYSTEM_INFO_URI = "unraid://system-info"


async def _read(
    mcp: FastMCP,
    fetch: Callable[[UnraidClient], Awaitable[Any]],
    uri: str,
) -> Any:
    """Run a ``fetch_*`` function with the lifespan client, mapping any domain
    error to a secret-free :class:`ResourceError` so the read never crashes."""
    ctx = mcp.get_context()
    client = get_client(ctx)
    try:
        return await fetch(client)
    except UnraidError as exc:
        raise ResourceError(f"Could not read {uri}: {exc}") from None


def register_resources(mcp: FastMCP, settings: Settings) -> None:
    @mcp.resource(
        HEALTH_URI,
        name="unraid_health",
        title="Unraid health summary",
        description=(
            "Compact health roll-up for triage: array state, capacity, any "
            "unhealthy disks, parity-check status, UPS state, and unread "
            "notification counts. Same data as the get_health_summary tool."
        ),
        mime_type="application/json",
    )
    async def health_resource() -> dict[str, Any]:
        return await _read(mcp, fetch_health, HEALTH_URI)

    @mcp.resource(
        SYSTEM_INFO_URI,
        name="unraid_system_info",
        title="Unraid system information",
        description=(
            "Host system information: OS/kernel, CPU, memory layout, "
            "motherboard, versions, uptime, and flash identity. Same data as "
            "the get_system_info tool."
        ),
        mime_type="application/json",
    )
    async def system_info_resource() -> dict[str, Any]:
        return await _read(mcp, fetch_system_info, SYSTEM_INFO_URI)
