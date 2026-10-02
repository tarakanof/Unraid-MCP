"""UPS, network, identity, health-summary, and the optional raw-query tool."""

from __future__ import annotations

import posixpath
from typing import Annotated, Any

from graphql import parse as graphql_parse
from graphql.error import GraphQLError
from graphql.language import OperationDefinitionNode, OperationType
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidAuthError, UnraidGraphQLError
from ..formatting import (
    MAX_LOG_RESULT_CHARS,
    limit_raw_result,
    shape_array_status,
    shape_connect_status,
    shape_health_temperature,
    shape_installed_unraid_plugins,
    shape_log_file,
    shape_log_files,
    shape_me,
    shape_network_interfaces,
    shape_notifications_overview,
    shape_plugins,
    shape_ups,
    shape_warnings_and_alerts,
    summarize_health,
)
from ..types import HealthSummary
from ._base import (
    READ_ONLY,
    execute_with_fallback,
    feature_unsupported,
    gather_all,
    get_app_context,
    guarded,
    is_permission_error,
    safe_query,
    safe_query_with_status,
    unsupported_field_error,
)

# Server-enforced cap on how many log lines a single read_log_file call may
# request; kept in sync with the docstring below.
MAX_LOG_LINES = 500

# Only paths under this prefix are accepted (defense-in-depth on top of
# server-side validation) — the API serves system logs from here.
LOG_PATH_PREFIX = "/var/log"


def _validate_log_path(path: str) -> None:
    """Require an absolute, `..`-free path that is /var/log or a descendant."""
    bad = (
        not path
        or "\x00" in path
        or not path.startswith("/")
        or ".." in path.split("/")
        or not (
            posixpath.normpath(path) == LOG_PATH_PREFIX
            or posixpath.normpath(path).startswith(LOG_PATH_PREFIX + "/")
        )
    )
    if bad:
        raise ToolError(
            f"path must be an absolute path under {LOG_PATH_PREFIX!r} with no '..' "
            "segments or NUL bytes. Call list_log_files first to get a valid path."
        )


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
    return shape_ups(
        await execute_with_fallback(client, queries.UPS_DEVICES, queries.UPS_DEVICES_LEGACY)
    )


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
    if lines < 1:
        raise ToolError(f"lines={lines} must be at least 1.")
    # startLine is a 1-based line number upstream.
    if start_line is not None and start_line < 1:
        raise ToolError(f"start_line={start_line} must be >= 1 (1-based line number).")
    _validate_log_path(path)

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
    # The two root queries are independent; the installed-plugins shaper only
    # needs the first result *after* both have arrived, so fetch concurrently.
    try:
        plugins_data, installed_raw = await gather_all(
            client.execute(queries.PLUGINS),
            safe_query(client, queries.INSTALLED_UNRAID_PLUGINS, lambda data: data, None),
        )
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported("plugin list", api_version=api_version) from None
        raise
    plugins = shape_plugins(plugins_data)
    known_names = {p["name"] for p in plugins if p.get("name")}
    installed = (
        shape_installed_unraid_plugins(installed_raw, known_names)
        if installed_raw is not None
        else []
    )
    return plugins + installed


async def fetch_health(
    client: UnraidClient, *, ignore_sensors: tuple[str, ...] = ()
) -> HealthSummary:
    """``ignore_sensors``: lower-cased temperature sensor ids/names/labels to leave
    out of the verdict (``Settings.health_ignored_sensors``)."""

    # Health only needs the baseline selections. They are accepted by every
    # API build, so a newer-field validation error can't mark a check failed.
    async def ups_check() -> tuple[list[dict[str, Any]], bool, bool]:
        # (devices, ok, eligible) — eligible: failed with a plain (non-permission,
        # supported) GraphQL error, so UPS may simply be unconfigured.
        try:
            ups_data, ups_errors = await client.execute_with_errors(queries.UPS_DEVICES_LEGACY)
            return shape_ups(ups_data), not ups_errors, False
        except UnraidGraphQLError as exc:
            return [], False, not is_permission_error(exc) and not unsupported_field_error(exc)
        except UnraidAuthError:
            return [], False, False

    (
        (array, array_ok),
        (ups, ups_ok, ups_eligible),
        (overview, notifications_ok),
        (alerts, alerts_ok),
        (sensors, temperature_ok),
    ) = await gather_all(
        safe_query_with_status(
            client, queries.ARRAY_STATUS_LEGACY, shape_array_status, {}, required_field="array"
        ),
        ups_check(),
        safe_query_with_status(
            client,
            queries.NOTIFICATIONS_OVERVIEW,
            shape_notifications_overview,
            {},
            tolerate_auth=True,
        ),
        # Optional: older builds lack warningsAndAlerts; then top_alerts is omitted.
        safe_query_with_status(
            client, queries.WARNINGS_AND_ALERTS, shape_warnings_and_alerts, [], tolerate_auth=True
        ),
        # Needs the newer per-sensor status/thresholds: older builds -> failed.
        safe_query_with_status(
            client,
            queries.HEALTH_TEMPERATURE,
            lambda data: shape_health_temperature(data, ignore_sensors),
            [],
            required_field="metrics",
            tolerate_auth=True,
        ),
    )
    checks = {
        name: "ok" if ok else "failed"
        for name, ok in (
            ("array", array_ok),
            ("ups", ups_ok),
            ("notifications", notifications_ok),
            ("temperature", temperature_ok),
        )
    }
    if ups_eligible:
        config, config_ok = await safe_query_with_status(
            client,
            queries.UPS_CONFIGURATION,
            lambda data: data.get("upsConfiguration") or {},
            {},
            required_field="upsConfiguration",
            tolerate_auth=True,
        )
        # Real boxes with no UPS report service=null, not "disable".
        if config_ok and (config.get("service") or "").lower() != "enable":
            checks["ups"] = "not_configured"
    top_alerts = alerts if alerts_ok or alerts else None
    return summarize_health(
        array, ups, overview, checks, top_alerts, sensors if temperature_ok or sensors else None
    )


async def do_raw_query(
    client: UnraidClient, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    _ensure_read_only(query)
    return limit_raw_result(await client.execute(query, variables))


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Get UPS Status", annotations=READ_ONLY)
    async def get_ups_status(ctx: Context) -> list[dict[str, Any]]:
        """Get UPS devices: status, battery charge/runtime/health, and load/voltage."""
        return await guarded(ctx, fetch_ups)

    @mcp.tool(title="List Network Interfaces", annotations=READ_ONLY)
    async def list_network_interfaces(ctx: Context) -> list[dict[str, Any]]:
        """List network interfaces with MAC, speed, state, and IPv4/IPv6 addresses."""
        return await guarded(ctx, fetch_network_interfaces)

    @mcp.tool(title="Whoami", annotations=READ_ONLY)
    async def whoami(ctx: Context) -> dict[str, Any]:
        """Show the authenticated API user and its roles — useful to confirm the key's scope."""
        return await guarded(ctx, fetch_me)

    @mcp.tool(title="Get Connect Status", annotations=READ_ONLY)
    async def get_connect_status(ctx: Context) -> dict[str, Any]:
        """Get Unraid registration/license and remote-access (Connect) status."""
        return await guarded(ctx, fetch_connect_status)

    @mcp.tool(title="List Plugins", annotations=READ_ONLY)
    async def list_plugins(ctx: Context) -> list[dict[str, Any]]:
        """List installed Unraid plugins: name, version, and whether they have API/CLI
        modules (from the `plugins` query), unioned with installed `.plg` filenames not
        otherwise represented (from `installedUnraidPlugins`). Each entry's `source`
        field indicates which query it came from."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_plugins, api_version=api_version)

    @mcp.tool(title="Get Health Summary", annotations=READ_ONLY)
    async def get_health_summary(ctx: Context) -> HealthSummary:
        """Compact health roll-up for triage: array state, capacity, any unhealthy disks,
        parity-check status, UPS state, unread notification counts, and up to 5 top
        unread warnings/alerts (`top_alerts`, when the API supports it).

        overall is critical for red/failed/missing disks, UPS LOWBATT, or ONBATT with
        charge <20% or runtime <300 seconds; attention for other unhealthy disks,
        unread alerts/warnings, UPS on battery, or parity errors. Failed queries
        yield degraded when no critical/attention signal exists; otherwise ok.
        Temperature sensors are picked by id (lm_sensors temp<N>_input, disk, IPMI;
        fans/voltages/power and sentinel readings are ignored). A sensor at critical
        raises critical (an NVMe at critical below 75 C only raises attention), at warning
        raises attention; `temperature` gives the hottest sensor and the
        warning/critical counts (omitted when that query failed).
        UNRAID_MCP_HEALTH_IGNORE_SENSORS excludes named sensors from the
        verdict (counted in `ignored_count`).
        reasons explains each signal; checks marks array/ups/notifications/temperature
        queries as ok or failed; ups is not_configured only when its query fails with a
        non-permission, supported GraphQL error and the UPS service is not enabled.
        HTTP 403 on the ups/notifications sub-checks marks them failed; connection errors
        propagate. Partial GraphQL errors mark a check failed while
        preserving usable data. Auth/connection/configuration errors propagate.
        Array state is informational. Also at unraid://health.
        """
        return await guarded(ctx, fetch_health, ignore_sensors=settings.health_ignored_sensors)

    @mcp.tool(title="List Log Files", annotations=READ_ONLY)
    async def list_log_files(ctx: Context) -> list[dict[str, Any]]:
        """List available system log files: name, path, size, and last-modified time.
        Use a path from this list with read_log_file — arbitrary paths are rejected."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_log_files, api_version=api_version)

    @mcp.tool(
        title="Read Log File",
        annotations=READ_ONLY,
        meta={"anthropic/maxResultSizeChars": MAX_LOG_RESULT_CHARS},
    )
    async def read_log_file(
        ctx: Context,
        path: str,
        lines: Annotated[int, Field(ge=1, le=MAX_LOG_LINES)] = 100,
        start_line: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict[str, Any]:
        """Read a slice of a system log file for triage (e.g. "why did my server do
        X last night"). `path` must be one listed by list_log_files (must start with
        `/var/log`, no `..`) — call that tool first if you don't have a path. `lines`
        is 1..500 per call; `start_line` is a 1-based line
        number. Only the file's basename is used: upstream resolves it inside
        `/var/log`, so nested paths read `/var/log/<basename>`.

        The response includes `total_lines` (the file's total line count) and
        `start_line` (where this slice began) so you can page through a large file.
        To page forward, call again with `start_line` advanced by `lines`. To read
        the tail of the file, first call with a small `lines` to learn `total_lines`,
        then call again with `start_line = total_lines - lines + 1`.

        The serialized result is capped at ~60k characters. If a slice is bigger (long lines),
        `content` keeps whole leading lines and the response adds `truncated: true`,
        `truncation_reason: "char_budget"`, `omitted_lines`, and `next_start_line`
        (the first omitted line) — call again with `start_line=next_start_line`
        (optionally a smaller `lines`) to continue. If a single line alone exceeds
        the budget it is cut and `line_truncated: true` is set; the remainder of
        that line cannot be retrieved by paging (`next_start_line` moves on to the
        next line).
        """
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_log_file, path, lines, start_line, api_version=api_version)


def register_raw_query(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Run GraphQL Query", annotations=READ_ONLY)
    async def run_graphql_query(
        ctx: Context, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run an arbitrary READ-ONLY GraphQL query against the Unraid API (escape hatch
        for fields without a dedicated tool). Mutations and subscriptions are rejected.

        Results over ~60k characters are replaced by {"truncated": true,
        "truncation_reason": "char_budget", "total_chars", "preview", "message"};
        narrow the query selection (fewer fields, filters, smaller windows) and re-run."""
        return await guarded(ctx, do_raw_query, query, variables)
