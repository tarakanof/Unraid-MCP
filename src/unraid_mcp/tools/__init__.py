"""MCP tool registration.

Read-only tools are always registered. Mutating tools are registered only when
``settings.allow_mutations`` is true; high-blast-radius "dangerous" mutations
(array topology, container removal) additionally require ``allow_dangerous`` —
enabling ``allow_dangerous`` without ``allow_mutations`` unlocks nothing. The
read-only raw GraphQL passthrough registers only when ``settings.allow_raw_query``
is true. This keeps the default surface area strictly read-only.

``UNRAID_MCP_TOOLS`` then narrows whatever those flags registered to the listed
names. It covers every tier but never enables one.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp.server.mcpserver import MCPServer

from ..errors import UnraidConfigError
from ..logging import get_logger
from . import array, docker, misc, notifications, shares, system, vm
from ._base import compact_read_results
from ._schema import forbid_unknown_arguments, trim_published_tools

if TYPE_CHECKING:
    from ..config import Settings

log = get_logger(__name__)

_MODULES = (system, array, docker, vm, shares, notifications, misc)

Registrar = Callable[[MCPServer, "Settings"], None]


@dataclass(frozen=True)
class _Tier:
    """One permission tier: when it registers, and what an operator must set."""

    needs: str
    enabled: Callable[[Settings], bool]
    registrars: tuple[Registrar, ...]


def _registrars(attr: str) -> tuple[Registrar, ...]:
    return tuple(getattr(m, attr) for m in _MODULES if hasattr(m, attr))


_TIERS = (
    _Tier("nothing", lambda _: True, _registrars("register")),
    _Tier(
        "UNRAID_MCP_ALLOW_MUTATIONS=true",
        lambda s: s.allow_mutations,
        _registrars("register_mutations"),
    ),
    # Gated by BOTH flags: allow_dangerous alone is a no-op.
    _Tier(
        "UNRAID_MCP_ALLOW_MUTATIONS=true and UNRAID_MCP_ALLOW_DANGEROUS=true",
        lambda s: s.allow_mutations and s.allow_dangerous,
        _registrars("register_dangerous"),
    ),
    _Tier(
        "UNRAID_MCP_ALLOW_RAW_QUERY=true",
        lambda s: s.allow_raw_query,
        (misc.register_raw_query,),
    ),
)


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


def tool_catalog(settings: Settings) -> dict[str, str]:
    """Every tool of every tier, whatever the flags, mapped to what it needs.

    Derived by registering each tier into a throwaway server, so a new tool
    joins the catalog without a hand-kept list.
    """
    catalog: dict[str, str] = {}
    for tier in _TIERS:
        probe = MCPServer("unraid-catalog")
        for register in tier.registrars:
            register(probe, settings)
        for tool in probe._tool_manager.list_tools():  # noqa: SLF001 - no public Tool accessor
            catalog.setdefault(tool.name, tier.needs)
    return catalog


def _apply_allow_list(mcp: MCPServer, settings: Settings, allowed: frozenset[str]) -> None:
    """Narrow ``mcp`` to ``allowed``; unknown names raise ``UnraidConfigError``.

    A catalog name whose tier is off stays off: the list never enables a tier.
    """
    catalog = tool_catalog(settings)
    unknown = sorted(allowed - catalog.keys())
    if unknown:
        raise UnraidConfigError(
            f"UNRAID_MCP_TOOLS names unknown tool(s): {', '.join(unknown)}. "
            "Use exact, case-sensitive tool names."
        )
    registered = {t.name for t in mcp._tool_manager.list_tools()}  # noqa: SLF001
    for name in registered - allowed:
        mcp.remove_tool(name)
    for name in sorted(allowed - registered):
        log.warning("UNRAID_MCP_TOOLS lists %s, but it stays off: it needs %s.", name, catalog[name])


def register_all(mcp: MCPServer, settings: Settings) -> None:
    for tier in _TIERS:
        if tier.enabled(settings):
            for register in tier.registrars:
                register(mcp, settings)
    # Before the steps below, which process whatever is registered.
    if (allowed := settings.allowed_tools) is not None:
        _apply_allow_list(mcp, settings, allowed)
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
