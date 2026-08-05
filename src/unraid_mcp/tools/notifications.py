"""Notification tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_mutation_result, shape_notifications, shape_notifications_overview
from ._base import DESTRUCTIVE, MUTATING, READ_ONLY, guarded, require_confirm

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


async def do_archive_notification(
    client: UnraidClient, notification_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, f"archive notification '{notification_id}'")
    return shape_mutation_result(
        await client.execute(queries.ARCHIVE_NOTIFICATION, {"id": notification_id})
    )


async def do_archive_all(
    client: UnraidClient, importance: str | None, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, "archive all notifications")
    return shape_mutation_result(
        await client.execute(queries.ARCHIVE_ALL_NOTIFICATIONS, {"importance": importance})
    )


async def do_unread_notification(
    client: UnraidClient, notification_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, f"mark notification '{notification_id}' unread")
    return shape_mutation_result(
        await client.execute(queries.UNREAD_NOTIFICATION, {"id": notification_id})
    )


async def do_delete_notification(
    client: UnraidClient, notification_id: str, notification_type: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, f"permanently delete notification '{notification_id}'")
    return shape_mutation_result(
        await client.execute(
            queries.DELETE_NOTIFICATION, {"id": notification_id, "type": notification_type}
        )
    )


async def do_archive_notifications(
    client: UnraidClient, ids: list[str], confirm: bool
) -> dict[str, Any]:
    if not ids:
        raise ToolError("ids must be a non-empty list of notification ids.")
    require_confirm(confirm, f"archive {len(ids)} notification(s)")
    return shape_mutation_result(await client.execute(queries.ARCHIVE_NOTIFICATIONS, {"ids": ids}))


async def do_unarchive_notifications(
    client: UnraidClient, ids: list[str], confirm: bool
) -> dict[str, Any]:
    if not ids:
        raise ToolError("ids must be a non-empty list of notification ids.")
    require_confirm(confirm, f"unarchive {len(ids)} notification(s)")
    return shape_mutation_result(
        await client.execute(queries.UNARCHIVE_NOTIFICATIONS, {"ids": ids})
    )


async def do_unarchive_all(
    client: UnraidClient, importance: str | None, confirm: bool
) -> dict[str, Any]:
    _validate_importance(importance)
    require_confirm(confirm, "unarchive all notifications")
    return shape_mutation_result(
        await client.execute(queries.UNARCHIVE_ALL_NOTIFICATIONS, {"importance": importance})
    )


async def do_delete_archived_notifications(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "permanently delete ALL archived notifications (irreversible)")
    return shape_mutation_result(await client.execute(queries.DELETE_ARCHIVED_NOTIFICATIONS))


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
        await client.execute(queries.CREATE_NOTIFICATION, {"input": input_data})
    )


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_notifications_overview(ctx: Context) -> dict[str, Any]:
        """Get unread and archived notification counts by severity (info/warning/alert/total)."""
        return await guarded(ctx, fetch_overview)

    @mcp.tool(annotations=READ_ONLY)
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


def register_mutations(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=MUTATING)
    async def archive_notification(
        ctx: Context, notification_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Archive (clear) a single unread notification by id. Requires confirm=true."""
        return await guarded(ctx, do_archive_notification, notification_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def archive_all_notifications(
        ctx: Context, importance: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Archive all unread notifications (optionally only one importance). Bulk action —
        requires confirm=true."""
        return await guarded(ctx, do_archive_all, importance, confirm)

    @mcp.tool(annotations=MUTATING)
    async def mark_notification_unread(
        ctx: Context, notification_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Mark an archived notification unread again by id. Requires confirm=true."""
        return await guarded(ctx, do_unread_notification, notification_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_notification(
        ctx: Context,
        notification_id: str,
        notification_type: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Permanently delete a notification by id. notification_type must be UNREAD or
        ARCHIVE (matching where the notification currently lives). Irreversible —
        requires confirm=true."""
        return await guarded(
            ctx, do_delete_notification, notification_id, notification_type, confirm
        )

    @mcp.tool(annotations=MUTATING)
    async def archive_notifications(
        ctx: Context, ids: list[str], confirm: bool = False
    ) -> dict[str, Any]:
        """Bulk-archive unread notifications by id (from list_notifications). Requires a
        non-empty ids list and confirm=true."""
        return await guarded(ctx, do_archive_notifications, ids, confirm)

    @mcp.tool(annotations=MUTATING)
    async def unarchive_notifications(
        ctx: Context, ids: list[str], confirm: bool = False
    ) -> dict[str, Any]:
        """Bulk-unarchive notifications by id, moving them back to unread. Requires a
        non-empty ids list and confirm=true."""
        return await guarded(ctx, do_unarchive_notifications, ids, confirm)

    @mcp.tool(annotations=MUTATING)
    async def unarchive_all_notifications(
        ctx: Context, importance: str | None = None, confirm: bool = False
    ) -> dict[str, Any]:
        """Unarchive all archived notifications (optionally only one importance:
        INFO/WARNING/ALERT). Bulk action — requires confirm=true."""
        return await guarded(ctx, do_unarchive_all, importance, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_archived_notifications(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Permanently delete ALL archived notifications. Irreversible bulk action —
        requires confirm=true."""
        return await guarded(ctx, do_delete_archived_notifications, confirm)

    @mcp.tool(annotations=MUTATING)
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
