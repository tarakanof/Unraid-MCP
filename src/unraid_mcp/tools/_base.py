"""Shared helpers for tool modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.fastmcp import Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..client import UnraidClient
from ..errors import UnraidError

# Hints for MCP clients. Read tools touch an external system (open world) but
# never change it; destructive mutations are flagged so hosts can warn/gate.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
MUTATING = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def get_client(ctx: Context) -> UnraidClient:
    """Fetch the shared GraphQL client from the server lifespan context."""
    return ctx.request_context.lifespan_context.client


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
