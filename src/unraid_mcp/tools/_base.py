"""Shared helpers for tool modules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from ..client import UnraidClient
from ..errors import UnraidAuthError, UnraidError, UnraidGraphQLError
from ..logging import redact

if TYPE_CHECKING:  # avoid a runtime import cycle (server imports tools imports _base)
    from ..server import AppContext

# Hints for MCP clients. Read tools touch an external system (open world) but
# never change it; destructive mutations are flagged so hosts can warn/gate.
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


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
    except ToolError as exc:
        # Local errors (e.g. GraphQL parse failures) can echo caller input,
        # which may contain a configured secret.
        raise ToolError(redact(str(exc), client.secrets)) from None
    except UnraidError as exc:
        raise ToolError(redact(str(exc), client.secrets)) from None


async def safe_query(
    client: UnraidClient,
    query: str,
    shaper: Callable[[dict[str, Any] | None], Any],
    default: Any,
) -> Any:
    """Run an optional query; on any Unraid error fall back to ``default`` so a
    composed fetch degrades gracefully when a feature/field isn't available on
    this API build (e.g. an older server missing a root query entirely)."""
    try:
        return shaper(await client.execute(query))
    except UnraidError:
        return default


async def safe_query_with_status(
    client: UnraidClient,
    query: str,
    shaper: Callable[[dict[str, Any] | None], Any],
    default: Any,
    *,
    required_field: str | None = None,
    tolerate_auth: bool = False,
) -> tuple[Any, bool]:
    """Keep usable data but flag GraphQL errors or a missing required root field.

    Transport, configuration, and server errors propagate. Authentication errors
    (HTTP 401/403) propagate unless ``tolerate_auth`` is set, which is for
    sub-checks run after another query already proved the credentials valid.
    """
    try:
        data, errors = await client.execute_with_errors(query)
        ok = not errors and (required_field is None or data.get(required_field) is not None)
        return shaper(data), ok
    except UnraidGraphQLError:
        return default, False
    except UnraidAuthError:
        if tolerate_auth:
            return default, False
        raise


def is_permission_error(exc: UnraidGraphQLError) -> bool:
    """True iff a GraphQL error carries a structured permission code."""
    return any(
        (e.get("extensions") or {}).get("code") in ("FORBIDDEN", "UNAUTHENTICATED")
        for e in exc.errors
    )


async def gather_all(*aws: Awaitable[Any]) -> list[Any]:
    """Run independent awaitables concurrently and return results in order.

    If any raises, the siblings are cancelled and awaited before the exception
    propagates, so no task is left running or logs "exception was never
    retrieved". The original exception is re-raised unwrapped (unlike
    ``TaskGroup``'s ``ExceptionGroup``), preserving ``guarded``'s mapping.
    """
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def require_confirm(confirm: bool, action: str) -> None:
    """Raise ``ToolError`` (before any network call) if a destructive action was
    not explicitly confirmed."""
    if not confirm:
        raise ToolError(
            f"Refusing to {action} without explicit confirmation. "
            "Re-call this tool with confirm=true if you really intend to."
        )
