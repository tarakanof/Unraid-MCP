"""UPS, network, identity, health-summary, and the optional raw-query tool."""

from __future__ import annotations

from typing import Any

from graphql import parse as graphql_parse
from graphql.error import GraphQLError
from graphql.language import OperationDefinitionNode, OperationType
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import (
    shape_array_status,
    shape_connect_status,
    shape_installed_unraid_plugins,
    shape_log_file,
    shape_log_files,
    shape_me,
    shape_network_interfaces,
    shape_notifications_overview,
    shape_plugins,
    shape_ups,
    summarize_health,
)
from ._base import (
    READ_ONLY,
    feature_unsupported,
    get_app_context,
    guarded,
    safe_query,
    unsupported_field_error,
)

# Server-enforced cap on how many log lines a single read_log_file call may
# request; kept in sync with the docstring below.
MAX_LOG_LINES = 500

# Only paths under this prefix are accepted (defense-in-depth on top of
# server-side validation) — the API serves system logs from here.
LOG_PATH_PREFIX = "/var/log"


def _ensure_read_only(query: str) -> None:
    """Parse the GraphQL document and reject anything that isn't a query.

    Parsing (rather than regex matching) correctly ignores comments, BOM,
    commas and whitespace, and never mistakes a field/alias named like a
    keyword — or a keyword inside a string literal — for an operation.
    """
    try:
        document = graphql_parse(query)
    except GraphQLError as exc:
        raise ToolError(f"Invalid GraphQL query: {exc.message}") from None

    operations = [d for d in document.definitions if isinstance(d, OperationDefinitionNode)]
    if not operations:
        raise ToolError("run_graphql_query needs a query operation; none was found.")
    if any(op.operation is not OperationType.QUERY for op in operations):
        raise ToolError(
            "run_graphql_query only accepts read-only queries; "
            "mutations and subscriptions are not allowed."
        )


async def fetch_ups(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_ups(await client.execute(queries.UPS_DEVICES))


async def fetch_network_interfaces(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_network_interfaces(await client.execute(queries.NETWORK_INTERFACES))


async def fetch_me(client: UnraidClient) -> dict[str, Any]:
    return shape_me(await client.execute(queries.ME))


async def fetch_connect_status(client: UnraidClient) -> dict[str, Any]:
    return shape_connect_status(await client.execute(queries.CONNECT_STATUS))


async def fetch_log_files(
    client: UnraidClient, *, api_version: str | None = None
) -> list[dict[str, Any]]:
    try:
        return shape_log_files(await client.execute(queries.LOG_FILES))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "system log files", requires="7.2+", api_version=api_version
            ) from None
        raise


async def fetch_log_file(
    client: UnraidClient,
    path: str,
    lines: int = 100,
    start_line: int | None = None,
    *,
    api_version: str | None = None,
) -> dict[str, Any]:
    # Validate before any network I/O.
    if lines > MAX_LOG_LINES:
        raise ToolError(
            f"lines={lines} exceeds the maximum of {MAX_LOG_LINES} per call; "
            "request a smaller window and page with start_line instead."
        )
    if not path or not path.startswith(LOG_PATH_PREFIX):
        raise ToolError(
            f"path must start with {LOG_PATH_PREFIX!r}. Call list_log_files first "
            "to get a valid path."
        )

    variables: dict[str, Any] = {"path": path, "lines": lines}
    if start_line is not None:
        variables["startLine"] = start_line

    try:
        return shape_log_file(await client.execute(queries.LOG_FILE, variables))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "system log files", requires="7.2+", api_version=api_version
            ) from None
        raise


async def fetch_plugins(
    client: UnraidClient, *, api_version: str | None = None
) -> list[dict[str, Any]]:
    """List installed plugins, combining two upstream root queries into one list.

    ``plugins`` gives rich per-plugin metadata (name/version/module flags);
    ``installedUnraidPlugins`` gives just installed ``.plg`` filenames (a
    coarser, OS-level view). Both are unioned into a single list, each entry
    tagged with its ``source`` — entries already covered by ``plugins`` are
    not duplicated from ``installedUnraidPlugins``. ``installedUnraidPlugins``
    degrades gracefully (older builds without it just contribute nothing extra);
    if ``plugins`` itself is unsupported, the whole tool raises a friendly error.
    """
    try:
        plugins = shape_plugins(await client.execute(queries.PLUGINS))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported("plugin list", api_version=api_version) from None
        raise
    known_names = {p["name"] for p in plugins if p.get("name")}
    installed = await safe_query(
        client,
        queries.INSTALLED_UNRAID_PLUGINS,
        lambda data: shape_installed_unraid_plugins(data, known_names),
        [],
    )
    return plugins + installed


async def fetch_health(client: UnraidClient) -> dict[str, Any]:
    array = shape_array_status(await client.execute(queries.ARRAY_STATUS))
    ups = await safe_query(client, queries.UPS_DEVICES, shape_ups, [])
    overview = await safe_query(
        client, queries.NOTIFICATIONS_OVERVIEW, shape_notifications_overview, {}
    )
    return summarize_health(array, ups, overview)


async def do_raw_query(
    client: UnraidClient, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    _ensure_read_only(query)
    return await client.execute(query, variables)


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_ups_status(ctx: Context) -> list[dict[str, Any]]:
        """Get UPS devices: status, battery charge/runtime/health, and load/voltage."""
        return await guarded(ctx, fetch_ups)

    @mcp.tool(annotations=READ_ONLY)
    async def list_network_interfaces(ctx: Context) -> list[dict[str, Any]]:
        """List network interfaces with MAC, speed, state, and IPv4/IPv6 addresses."""
        return await guarded(ctx, fetch_network_interfaces)

    @mcp.tool(annotations=READ_ONLY)
    async def whoami(ctx: Context) -> dict[str, Any]:
        """Show the authenticated API user and its roles — useful to confirm the key's scope."""
        return await guarded(ctx, fetch_me)

    @mcp.tool(annotations=READ_ONLY)
    async def get_connect_status(ctx: Context) -> dict[str, Any]:
        """Get Unraid registration/license and remote-access (Connect) status."""
        return await guarded(ctx, fetch_connect_status)

    @mcp.tool(annotations=READ_ONLY)
    async def list_plugins(ctx: Context) -> list[dict[str, Any]]:
        """List installed Unraid plugins: name, version, and whether they have API/CLI
        modules (from the `plugins` query), unioned with installed `.plg` filenames not
        otherwise represented (from `installedUnraidPlugins`). Each entry's `source`
        field indicates which query it came from."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_plugins, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def get_health_summary(ctx: Context) -> dict[str, Any]:
        """Compact health roll-up for triage: array state, capacity, any unhealthy disks,
        parity-check status, UPS state, and unread notification counts."""
        return await guarded(ctx, fetch_health)

    @mcp.tool(annotations=READ_ONLY)
    async def list_log_files(ctx: Context) -> list[dict[str, Any]]:
        """List available system log files: name, path, size, and last-modified time.
        Use a path from this list with read_log_file — arbitrary paths are rejected."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_log_files, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def read_log_file(
        ctx: Context,
        path: str,
        lines: int = 100,
        start_line: int | None = None,
    ) -> dict[str, Any]:
        """Read a slice of a system log file for triage (e.g. "why did my server do
        X last night"). `path` must be one listed by list_log_files (must start with
        `/var/log`) — call that tool first if you don't have a path. `lines` is capped
        at 500 per call.

        The response includes `total_lines` (the file's total line count) and
        `start_line` (where this slice began) so you can page through a large file.
        To page forward, call again with `start_line` advanced by `lines`. To read
        the tail of the file, first call with a small `lines` to learn `total_lines`,
        then call again with `start_line = total_lines - lines`.
        """
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_log_file, path, lines, start_line, api_version=api_version)


def register_raw_query(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def run_graphql_query(
        ctx: Context, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an arbitrary READ-ONLY GraphQL query against the Unraid API (escape hatch
        for fields without a dedicated tool). Mutations and subscriptions are rejected."""
        return await guarded(ctx, do_raw_query, query, variables)
