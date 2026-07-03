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

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from ..config import Settings

_MODULES = (system, array, docker, vm, shares, notifications, misc)


def register_all(mcp: FastMCP, settings: Settings) -> None:
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
