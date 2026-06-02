"""Notification tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_mutation_result, shape_notifications, shape_notifications_overview
from ._base import DESTRUCTIVE, MUTATING, READ_ONLY, guarded, require_confirm


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


def register(mcp: FastMCP, settings: Settings) -> None:
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


def register_mutations(mcp: FastMCP, settings: Settings) -> None:
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
