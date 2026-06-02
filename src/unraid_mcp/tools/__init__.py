"""MCP tool registration.

Read-only tools are always registered. Mutating tools are registered only when
``settings.allow_mutations`` is true; the read-only raw GraphQL passthrough only
when ``settings.allow_raw_query`` is true. This keeps the default surface area
strictly read-only.
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
    if settings.allow_raw_query:
        misc.register_raw_query(mcp, settings)
