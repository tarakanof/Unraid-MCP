"""Shared helpers for tool modules."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import Context, Elicit, ElicitationResult
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, Field

from ..client import UnraidClient
from ..errors import UnraidAuthError, UnraidError, UnraidGraphQLError
from ..logging import get_logger, redact

if TYPE_CHECKING:  # avoid a runtime import cycle (server imports tools imports _base)
    from ..server import AppContext

log = get_logger(__name__)

# ``(message)`` — see :func:`progress_reporter`.
ProgressCallback = Callable[[str], Awaitable[None]]

# Upper bound on one progress notification send; a stalled client must not stall a tool.
PROGRESS_TIMEOUT_S = 1.0
PROGRESS_QUEUE_MAX = 16

# Hints for MCP clients. Read tools touch an external system (open world) but
# never change it; destructive mutations are flagged so hosts can warn/gate.
# ``*_IDEMPOTENT`` marks tools where repeating the call has no further effect
# (start/stop/pause/archive/delete-by-id), i.e. it is safe to retry. Bulk ops whose
# effect isn't bounded by their args stay non-idempotent.
READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
MUTATING = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MUTATING_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
DESTRUCTIVE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True
)


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
    confirmation: ElicitationResult[Confirmation] | None = None,
    **kwargs: Any,
) -> Any:
    """Run a tool logic function with the shared client, translating domain
    errors into user-facing ``ToolError`` messages (which never contain secrets)."""
    if confirmation is not None and (
        confirmation.action != "accept" or not confirmation.data.proceed
    ):
        raise ToolError("cancelled by user")
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


async def execute_with_fallback(
    client: UnraidClient,
    query: str,
    legacy_query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``query``; if the API rejects a newer field, retry once with
    ``legacy_query`` (the baseline selection every supported build accepts).

    Only an unknown-field validation error triggers the retry — auth, network
    and other GraphQL errors propagate untouched.
    """
    try:
        return await client.execute(query, variables)
    except UnraidGraphQLError as exc:
        if not unsupported_field_error(exc):
            raise
        return await client.execute(legacy_query, variables)


def require_confirm(confirm: bool, action: str) -> None:
    """Raise ``ToolError`` (before any network call) if a destructive action was
    not explicitly confirmed."""
    if not confirm:
        raise ToolError(
            f"Refusing to {action} without explicit confirmation. "
            "Re-call this tool with confirm=true if you really intend to."
        )


def require_action(action: str, allowed: tuple[str, ...]) -> None:
    """Raise ``ToolError`` (before any network call) if a consolidated tool's
    ``action`` is not one of ``allowed``. The MCP layer already validates the
    ``Literal`` schema; this guards direct callers of the ``do_*`` logic."""
    if action not in allowed:
        raise ToolError(f"Invalid action '{action}'. Must be one of: {', '.join(allowed)}.")


REAP_TIMEOUT_S = 1.0
_abandoned: set[asyncio.Future[Any]] = set()


def _retrieve(task: asyncio.Future[Any]) -> None:
    _abandoned.discard(task)
    if not task.cancelled():
        task.exception()  # mark retrieved (no "never retrieved" warning)


async def _reap(task: asyncio.Future[Any]) -> None:
    """Cancel a helper task and wait up to ``REAP_TIMEOUT_S`` for it to finish.

    ``asyncio.wait`` never raises the task's own CancelledError, so only a cancel aimed
    at the caller can interrupt it and that propagates. A task whose cancellation
    cleanup stalls past the budget is abandoned (strong ref kept until it ends) so it
    can never hang the tool."""
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=REAP_TIMEOUT_S)
    if done:
        _retrieve(task)
    else:
        log.debug("helper task cleanup exceeded %ss; abandoning it", REAP_TIMEOUT_S)
        _abandoned.add(task)
        task.add_done_callback(_retrieve)


@contextlib.asynccontextmanager
async def progress_reporter(ctx: Context) -> AsyncIterator[ProgressCallback]:
    """Yield a best-effort, NON-BLOCKING progress callback bound to ``ctx`` for
    the plain ``fetch_*``/``do_*`` functions (they stay MCP-free).

    The callback only enqueues (bounded queue, oldest dropped when full) and never
    awaits a send, so a stalled client cannot eat a tool's deadline. A background
    worker performs the sends, each bounded by ``PROGRESS_TIMEOUT_S`` with errors
    swallowed (debug-logged). The worker owns one monotonic counter so ``progress``
    strictly increases (MCP spec); ``total`` is never sent (unknown) — counts and
    elapsed go in the message. On exit, pending messages get one bounded flush
    attempt and the worker is cancelled (no leaked tasks). A no-op on the wire
    when the client sent no progress token."""
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=PROGRESS_QUEUE_MAX)

    async def _worker() -> None:
        counter = 0
        while True:
            message = await queue.get()
            counter += 1
            try:
                # asyncio.timeout, not wait_for: on 3.11 wait_for can swallow a cancel
                # that lands as the awaitable completes, leaving the worker un-cancelled.
                async with asyncio.timeout(PROGRESS_TIMEOUT_S):
                    await ctx.report_progress(counter, None, message)
            except TimeoutError:
                log.debug("progress report timed out")
            except Exception as exc:  # noqa: BLE001 - progress must never fail the tool
                log.debug("progress report failed: %s", type(exc).__name__)
            finally:
                queue.task_done()

    async def _report(message: str) -> None:
        while True:
            try:
                queue.put_nowait(message)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                    queue.task_done()  # dropped oldest

    task = asyncio.ensure_future(_worker())
    try:
        yield _report
    finally:
        try:
            # asyncio.timeout (not wait_for) so a caller cancel is never swallowed.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(PROGRESS_TIMEOUT_S):
                    await queue.join()
        finally:
            # Unconditional, even if the flush was cancelled: never leak the worker.
            await _reap(task)


async def with_heartbeat(
    awaitable: Awaitable[Any],
    progress: ProgressCallback | None,
    *,
    interval_s: float,
    message: str = "Still working",
) -> Any:
    """Await ``awaitable``; if ``progress`` is given, emit a heartbeat every
    ``interval_s`` seconds (progress = elapsed seconds, monotonically increasing)
    while it runs. The heartbeat task is always cancelled; callback errors are
    swallowed."""
    if progress is None:
        return await awaitable

    async def _beat() -> None:
        elapsed = 0.0
        while True:
            await asyncio.sleep(interval_s)
            elapsed += interval_s
            with contextlib.suppress(Exception):
                await progress(f"{message} ({elapsed:.0f}s elapsed)")

    task = asyncio.ensure_future(_beat())
    try:
        return await awaitable
    finally:
        await _reap(task)


class Confirmation(BaseModel):
    """Human approval of the consequence shown by the host."""

    proceed: bool = Field(description="Accept this action and its consequences")


_MRTR_VERSION = "2026-07-28"


def _can_elicit(ctx: Context) -> bool:
    """True iff an elicitation from this request can actually reach the client.

    Needs (a) a declared form-elicitation capability and (b) a delivery path:
    ``InputRequiredResult`` on protocol >= 2026-07-28, or a live
    ``elicitation/create`` on a back channel (``session.can_send_request``,
    i.e. stdio or stateful HTTP). Legacy clients over stateless HTTP have no
    back channel, so they stay confirm-only instead of failing the call.
    """
    capabilities = ctx.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is None or (elicitation.form is None and elicitation.url is not None):
        return False
    version = ctx.protocol_version
    if version is not None and is_version_at_least(version, _MRTR_VERSION):
        return True
    return bool(ctx.request_context.session.can_send_request)


def require_confirmation(
    ctx: Context, confirm: bool, consequence: str
) -> Confirmation | Elicit[Confirmation]:
    """Resolver gate for destructive tools, before their bodies can perform I/O.

    Elicits where the request can deliver it (see :func:`_can_elicit`); every
    other client keeps the confirm-only gate. A bare elicitation capability
    means form support.
    """
    require_confirm(confirm, consequence)
    if _can_elicit(ctx):
        return Elicit(consequence, Confirmation)
    return Confirmation(proceed=True)
