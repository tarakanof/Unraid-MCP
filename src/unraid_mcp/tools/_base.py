"""Shared helpers for tool modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..client import UnraidClient
from ..errors import UnraidError, UnraidGraphQLError

if TYPE_CHECKING:  # avoid a runtime import cycle (server imports tools imports _base)
    from ..server import AppContext

# Hints for MCP clients. Read tools touch an external system (open world) but
# never change it; destructive mutations are flagged so hosts can warn/gate.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def get_app_context(ctx: Context) -> AppContext:
    """Fetch the whole per-process ``AppContext`` from the server lifespan.

    Tools use this to read the probed ``api_version`` / ``unraid_version`` so
    they can explain capability gaps (see :func:`feature_unsupported`).
    """
    return ctx.request_context.lifespan_context


def get_client(ctx: Context) -> UnraidClient:
    """Fetch the shared GraphQL client from the server lifespan context."""
    return get_app_context(ctx).client


def unsupported_field_error(exc: UnraidError) -> bool:
    """True iff ``exc`` is a GraphQL validation error for an unknown field.

    Detects the upstream phrase ``Cannot query field "<name>" on type "<Type>".``
    emitted when a query selects a field the connected API build doesn't have.
    This is the single detection point — tools must not string-match themselves.
    """
    if not isinstance(exc, UnraidGraphQLError):
        return False
    needle = "Cannot query field"
    if needle in str(exc):
        return True
    return any(needle in str(e.get("message", "")) for e in exc.errors)


def feature_unsupported(
    feature: str,
    *,
    requires: str | None = None,
    api_version: str | None = None,
) -> ToolError:
    """Build (do NOT raise) a friendly ``ToolError`` for a missing API feature.

    Use it in the degrading-fetch pattern so a query against an older Unraid
    build turns a raw GraphQL validation failure into actionable guidance::

        async def fetch_x(client, *, api_version=None):
            try:
                return shape_x(await client.execute(queries.X))
            except UnraidGraphQLError as exc:
                if unsupported_field_error(exc):
                    raise feature_unsupported(
                        "live system metrics", requires="7.2+", api_version=api_version
                    ) from None
                raise

    The tool wrapper supplies the version from context::

        @mcp.tool(annotations=READ_ONLY)
        async def get_x(ctx):
            api_version = get_app_context(ctx).api_version
            return await guarded(ctx, fetch_x, api_version=api_version)

    ``requires`` / ``api_version`` clauses are omitted gracefully when None.
    """
    msg = f"This Unraid API version does not support {feature}."
    if api_version:
        msg += f" Server reports API {api_version};"
        msg += f" requires {requires}." if requires else " unsupported on this build."
    elif requires:
        msg += f" Requires {requires}."
    msg += " Upgrade Unraid or the Connect plugin."
    return ToolError(msg)


async def guarded(
    ctx: Context,
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a tool logic function with the shared client, translating domain
    errors into user-facing ``ToolError`` messages (which never contain secrets)."""
    client = get_client(ctx)
    try:
        return await fn(client, *args, **kwargs)
    except ToolError:
        raise
    except UnraidError as exc:
        raise ToolError(str(exc)) from None


def require_confirm(confirm: bool, action: str) -> None:
    """Raise ``ToolError`` (before any network call) if a destructive action was
    not explicitly confirmed."""
    if not confirm:
        raise ToolError(
            f"Refusing to {action} without explicit confirmation. "
            "Re-call this tool with confirm=true if you really intend to."
        )
