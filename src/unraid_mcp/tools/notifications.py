"""Notification tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import (
    shape_mutation_result,
    shape_notifications,
    shape_notifications_overview,
    shape_warnings_and_alerts,
)
from ._base import (
    DESTRUCTIVE,
    DESTRUCTIVE_IDEMPOTENT,
    MUTATING,
    MUTATING_IDEMPOTENT,
    READ_ONLY,
    Confirmation,
    feature_unsupported,
    get_app_context,
    guarded,
    local_id,
    require_action,
    require_confirm,
    require_confirmation,
    unsupported_field_error,
)

# The upstream query takes no arguments, so the cap is applied client-side.
MAX_ALERTS_LIMIT = 100
_VALID_IMPORTANCE = {"INFO", "WARNING", "ALERT"}


def _validate_importance(importance: str | None) -> None:
    if importance is not None and importance not in _VALID_IMPORTANCE:
        raise ToolError(
            f"Invalid importance '{importance}'. Must be one of: "
            f"{', '.join(sorted(_VALID_IMPORTANCE))}."
        )


async def fetch_overview(client: UnraidClient) -> dict[str, Any]:
    return shape_notifications_overview(await client.execute(queries.NOTIFICATIONS_OVERVIEW))


async def fetch_notifications(
    client: UnraidClient,
    notification_type: str = "UNREAD",
    importance: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    filt: dict[str, Any] = {"type": notification_type, "offset": offset, "limit": limit}
    if importance:
        filt["importance"] = importance
    return shape_notifications(await client.execute(queries.LIST_NOTIFICATIONS, {"filter": filt}))


async def fetch_warnings_and_alerts(
    client: UnraidClient, limit: int = 20, *, api_version: str | None = None
) -> list[dict[str, Any]]:
    if not 1 <= limit <= MAX_ALERTS_LIMIT:
        raise ToolError(f"limit must be between 1 and {MAX_ALERTS_LIMIT}.")
    try:
        items = shape_warnings_and_alerts(await client.execute(queries.WARNINGS_AND_ALERTS))
        return items[:limit]
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported("warnings and alerts", api_version=api_version) from None
        raise


async def do_archive_notification(
    client: UnraidClient, notification_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, _archive_state_consequence(notification_id, "archive"))
    return shape_mutation_result(
        await client.execute(queries.ARCHIVE_NOTIFICATION, {"id": local_id(notification_id)}),
        ("archiveNotification",),
    )


async def do_archive_all(
    client: UnraidClient, importance: str | None, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, _archive_all_consequence(importance))
    return shape_mutation_result(
        await client.execute(queries.ARCHIVE_ALL_NOTIFICATIONS, {"importance": importance}),
        ("archiveAll",),
    )


async def do_unread_notification(
    client: UnraidClient, notification_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, _archive_state_consequence(notification_id, "unarchive"))
    return shape_mutation_result(
        await client.execute(queries.UNREAD_NOTIFICATION, {"id": local_id(notification_id)}),
        ("unreadNotification",),
    )


async def do_delete_notification(
    client: UnraidClient, notification_id: str, notification_type: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, _delete_notification_consequence(notification_id))
    return shape_mutation_result(
        await client.execute(
            queries.DELETE_NOTIFICATION,
            {"id": local_id(notification_id), "type": notification_type},
        ),
        ("deleteNotification",),
    )


async def do_archive_notifications(
    client: UnraidClient, ids: list[str], confirm: bool
) -> dict[str, Any]:
    if not ids:
        raise ToolError("ids must be a non-empty list of notification ids.")
    require_confirm(confirm, _archive_state_bulk_consequence(ids, "archive"))
    return shape_mutation_result(
        await client.execute(queries.ARCHIVE_NOTIFICATIONS, {"ids": _local_ids(ids)}),
        ("archiveNotifications",),
    )


async def do_unarchive_notifications(
    client: UnraidClient, ids: list[str], confirm: bool
) -> dict[str, Any]:
    if not ids:
        raise ToolError("ids must be a non-empty list of notification ids.")
    require_confirm(confirm, _archive_state_bulk_consequence(ids, "unarchive"))
    return shape_mutation_result(
        await client.execute(queries.UNARCHIVE_NOTIFICATIONS, {"ids": _local_ids(ids)}),
        ("unarchiveNotifications",),
    )


async def do_unarchive_all(
    client: UnraidClient, importance: str | None, confirm: bool
) -> dict[str, Any]:
    _validate_importance(importance)
    require_confirm(confirm, "unarchive all notifications")
    return shape_mutation_result(
        await client.execute(queries.UNARCHIVE_ALL_NOTIFICATIONS, {"importance": importance}),
        ("unarchiveAll",),
    )


async def do_delete_archived_notifications(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _DELETE_ARCHIVED_CONSEQUENCE)
    return shape_mutation_result(
        await client.execute(queries.DELETE_ARCHIVED_NOTIFICATIONS),
        ("deleteArchivedNotifications",),
    )


async def do_create_notification(
    client: UnraidClient,
    title: str,
    subject: str,
    description: str,
    importance: str,
    confirm: bool,
    link: str | None = None,
) -> dict[str, Any]:
    if importance not in _VALID_IMPORTANCE:
        raise ToolError(
            f"Invalid importance '{importance}'. Must be one of: "
            f"{', '.join(sorted(_VALID_IMPORTANCE))}."
        )
    require_confirm(confirm, f"post notification '{title}' to the Unraid WebGUI")
    input_data: dict[str, Any] = {
        "title": title,
        "subject": subject,
        "description": description,
        "importance": importance,
    }
    if link is not None:
        input_data["link"] = link
    return shape_mutation_result(
        await client.execute(queries.CREATE_NOTIFICATION, {"input": input_data}),
        ("createNotification",),
    )


ArchiveAction = Literal["archive", "unarchive"]
_ARCHIVE_ACTIONS: tuple[str, ...] = get_args(ArchiveAction)


async def do_notification_archive(
    client: UnraidClient, notification_id: str, action: str, confirm: bool
) -> dict[str, Any]:
    """Archive one notification, or move it back to unread (``unarchive``)."""
    require_action(action, _ARCHIVE_ACTIONS)
    require_confirm(confirm, _archive_state_consequence(notification_id, action))
    if action == "archive":
        return await do_archive_notification(client, notification_id, confirm)
    return await do_unread_notification(client, notification_id, confirm)


async def do_notification_archive_bulk(
    client: UnraidClient, ids: list[str], action: str, confirm: bool
) -> dict[str, Any]:
    """Archive or unarchive a list of notifications by id."""
    require_action(action, _ARCHIVE_ACTIONS)
    if action == "archive":
        return await do_archive_notifications(client, ids, confirm)
    return await do_unarchive_notifications(client, ids, confirm)


def _local_ids(ids: list[str]) -> list[str]:
    """Bare ids, first occurrence kept: `x` and `<serverId>:x` are one notification."""
    return list(dict.fromkeys(local_id(i) for i in ids))


def _archive_state_consequence(notification_id: str, action: str) -> str:
    if action == "archive":
        return f"archive notification '{notification_id}'"
    return f"mark notification '{notification_id}' unread"


def _archive_state_bulk_consequence(ids: list[str], action: str) -> str:
    return f"{action} {len(ids)} notification(s)"


def _archive_all_consequence(importance: str | None) -> str:
    if importance:
        return f"archive all {importance} notifications"
    return "archive all notifications"


def _delete_notification_consequence(notification_id: str) -> str:
    return f"permanently delete notification '{notification_id}'"


_DELETE_ARCHIVED_CONSEQUENCE = "permanently delete ALL archived notifications (irreversible)"


def _confirm_archive_all_notifications(
    ctx: Context, confirm: bool, importance: str | None = None
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _archive_all_consequence(importance))


def _confirm_delete_notification(
    ctx: Context, confirm: bool, notification_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _delete_notification_consequence(notification_id))


def _confirm_delete_archived_notifications(
    ctx: Context, confirm: bool
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _DELETE_ARCHIVED_CONSEQUENCE)


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Get Notifications Overview", annotations=READ_ONLY)
    async def get_notifications_overview(ctx: Context) -> dict[str, Any]:
        """Get unread and archived notification counts by severity (info/warning/alert/total)."""
        return await guarded(ctx, fetch_overview)

    @mcp.tool(title="List Notifications", annotations=READ_ONLY)
    async def list_notifications(
        ctx: Context,
        notification_type: str = "UNREAD",
        importance: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List notifications. notification_type is UNREAD or ARCHIVE; importance optionally
        filters to INFO/WARNING/ALERT. Supports limit/offset paging."""
        return await guarded(ctx, fetch_notifications, notification_type, importance, limit, offset)

    @mcp.tool(
        title="List Warnings And Alerts", annotations=READ_ONLY, meta={"anthropic/alwaysLoad": True}
    )
    async def list_warnings_and_alerts(ctx: Context, limit: int = 20) -> list[dict[str, Any]]:
        """List current unread WARNING/ALERT notifications (deduplicated, latest first) —
        the cheapest "is anything wrong?" check. Same item shape as list_notifications;
        long descriptions are truncated. limit is 1-100 (default 20)."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_warnings_and_alerts, limit, api_version=api_version)


def register_mutations(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Notification Archive", annotations=MUTATING_IDEMPOTENT)
    async def notification_archive(
        ctx: Context, notification_id: str, action: ArchiveAction, confirm: bool = False
    ) -> dict[str, Any]:
        """Archive (clear) one unread notification by id, or unarchive one
        (mark an archived notification unread again). Requires confirm=true."""
        return await guarded(ctx, do_notification_archive, notification_id, action, confirm)

    @mcp.tool(title="Archive All Notifications", annotations=DESTRUCTIVE)
    async def archive_all_notifications(
        ctx: Context,
        importance: str | None = None,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_archive_all_notifications)
        ],
    ) -> dict[str, Any]:
        """Archive all unread notifications (optionally only one importance). Bulk action —
        requires confirm=true."""
        return await guarded(ctx, do_archive_all, importance, confirm, confirmation=confirmation)

    @mcp.tool(title="Delete Notification", annotations=DESTRUCTIVE_IDEMPOTENT)
    async def delete_notification(
        ctx: Context,
        notification_id: str,
        notification_type: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_delete_notification)
        ],
    ) -> dict[str, Any]:
        """Permanently delete a notification by id. notification_type must be UNREAD or
        ARCHIVE (matching where the notification currently lives). Irreversible —
        requires confirm=true."""
        return await guarded(
            ctx,
            do_delete_notification,
            notification_id,
            notification_type,
            confirm,
            confirmation=confirmation,
        )

    @mcp.tool(title="Notification Archive Bulk", annotations=MUTATING_IDEMPOTENT)
    async def notification_archive_bulk(
        ctx: Context, ids: list[str], action: ArchiveAction, confirm: bool = False
    ) -> dict[str, Any]:
        """Archive unread notifications, or unarchive archived ones back to unread,
        by id (from list_notifications). Requires a non-empty ids list and
        confirm=true."""
        return await guarded(ctx, do_notification_archive_bulk, ids, action, confirm)

    @mcp.tool(title="Unarchive All Notifications", annotations=MUTATING)
    async def unarchive_all_notifications(
        ctx: Context, importance: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Unarchive all archived notifications (optionally only one importance:
        INFO/WARNING/ALERT). Bulk action — requires confirm=true."""
        return await guarded(ctx, do_unarchive_all, importance, confirm)

    @mcp.tool(title="Delete Archived Notifications", annotations=DESTRUCTIVE)
    async def delete_archived_notifications(
        ctx: Context,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_delete_archived_notifications)
        ],
    ) -> dict[str, Any]:
        """Permanently delete ALL archived notifications. Irreversible bulk action —
        requires confirm=true."""
        return await guarded(
            ctx, do_delete_archived_notifications, confirm, confirmation=confirmation
        )

    @mcp.tool(title="Create Notification", annotations=MUTATING)
    async def create_notification(
        ctx: Context,
        title: str,
        subject: str,
        description: str,
        importance: str,
        link: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Post a notification to the Unraid WebGUI — use to leave the operator a
        persistent message. importance must be INFO, WARNING, or ALERT. Requires
        confirm=true."""
        return await guarded(
            ctx, do_create_notification, title, subject, description, importance, confirm, link
        )
