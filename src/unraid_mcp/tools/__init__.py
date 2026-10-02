"""MCP tool registration.

Read-only tools are always registered. Mutating tools are registered only when
``settings.allow_mutations`` is true; high-blast-radius "dangerous" mutations
(array topology, container removal) additionally require ``allow_dangerous`` —
enabling ``allow_dangerous`` without ``allow_mutations`` unlocks nothing. The
read-only raw GraphQL passthrough registers only when ``settings.allow_raw_query``
is true. This keeps the default surface area strictly read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import array, docker, misc, notifications, shares, system, vm
from ._base import compact_read_results
from ._schema import forbid_unknown_arguments, trim_published_tools

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

    from ..config import Settings

_MODULES = (system, array, docker, vm, shares, notifications, misc)


def sort_published_tools(mcp: MCPServer) -> None:
    """Publish tools alphabetically by name, whatever the registration order.

    ``tools/list`` must be deterministic so clients can cache it and keep
    prompt-cache hits; sorting makes that explicit rather than an accident of
    module import and flag order. Reaches into the SDK's ``_tool_manager``
    like :func:`trim_published_tools` (``tests/test_tool_order.py`` guards it).
    """
    tools = mcp._tool_manager._tools  # noqa: SLF001 - no public Tool accessor
    ordered = {name: tools[name] for name in sorted(tools)}
    tools.clear()
    tools.update(ordered)


def register_all(mcp: MCPServer, settings: Settings) -> None:
    for module in _MODULES:
        module.register(mcp, settings)
        if settings.allow_mutations and hasattr(module, "register_mutations"):
            module.register_mutations(mcp, settings)
        # Dangerous tier is gated by BOTH flags: allow_dangerous alone is a no-op.
        if (
            settings.allow_mutations
            and settings.allow_dangerous
            and hasattr(module, "register_dangerous")
        ):
            module.register_dangerous(mcp, settings)
    if settings.allow_raw_query:
        misc.register_raw_query(mcp, settings)
    # Read tools return one compact, null-free text block (#156).
    compact_read_results(mcp)
    # Reject undeclared arguments; errors are redacted of configured secrets.
    forbid_unknown_arguments(
        mcp,
        [
            settings.api_key.get_secret_value(),
            settings.bearer_token.get_secret_value() if settings.bearer_token else None,
        ],
    )
    # Strip pydantic boilerplate from the published descriptions and schemas.
    trim_published_tools(mcp)
    sort_published_tools(mcp)
