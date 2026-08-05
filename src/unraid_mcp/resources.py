"""URI-addressable MCP *resources*.

Resources let a client read live server state without spending a tool call.
They intentionally add no new GraphQL: each one reuses the exact ``fetch_*``
function behind the equivalent read tool, so a resource read returns byte-for-
byte the same JSON shape as the tool. The logic lives in the tool modules; this
module only wires those functions onto resource URIs.

A resource read cannot take a ``Context`` parameter the way a tool can: SDK v2
only injects a ``Context`` into *template* resource handlers, and a static
FunctionResource is invoked with no arguments (``MCPServer.get_context()`` is
gone too). :func:`register_resources` therefore receives an accessor for the
lifespan-shared :class:`~unraid_mcp.server.AppContext` from ``build_server`` and
uses its :class:`~unraid_mcp.client.UnraidClient` at read time. Any domain error
is translated into a clean, secret-free protocol error so an unreachable box
degrades to a readable message instead of an unhandled crash.

The error is raised as :class:`MCPError` rather than ``ResourceError``: SDK v2
deliberately replaces the text of any other exception escaping a resource read
with a generic ``Error reading resource <uri>`` (so handler internals can't leak),
and only ``MCPError`` passes through verbatim. Our messages are curated for the
operator and contain no secrets, so they are worth keeping.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR

from .client import UnraidClient
from .errors import UnraidError
from .tools.misc import fetch_health
from .tools.system import fetch_system_info

if TYPE_CHECKING:
    from .config import Settings
    from .server import AppContext

AppContextAccessor = Callable[[], "AppContext | None"]

HEALTH_URI = "unraid://health"
SYSTEM_INFO_URI = "unraid://system-info"


async def _read(
    app_context: AppContextAccessor,
    fetch: Callable[[UnraidClient], Awaitable[Any]],
    uri: str,
) -> Any:
    """Run a ``fetch_*`` function with the lifespan client, mapping any domain
    error to a secret-free :class:`MCPError` so the read never crashes."""
    context = app_context()
    if context is None:  # pragma: no cover - a read can only arrive while running
        raise MCPError(INTERNAL_ERROR, f"Could not read {uri}: server is not running", {"uri": uri})
    try:
        return await fetch(context.client)
    except UnraidError as exc:
        # data.uri mirrors the SDK's own resource-error payloads so clients can
        # extract the failing URI without parsing the message.
        raise MCPError(INTERNAL_ERROR, f"Could not read {uri}: {exc}", {"uri": uri}) from None


def register_resources(mcp: MCPServer, settings: Settings, app_context: AppContextAccessor) -> None:
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
        return await _read(app_context, fetch_health, HEALTH_URI)

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
        return await _read(app_context, fetch_system_info, SYSTEM_INFO_URI)
