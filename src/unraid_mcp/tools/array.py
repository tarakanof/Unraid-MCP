"""Array, parity, and disk tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import (
    shape_array_status,
    shape_mutation_result,
    shape_physical_disk,
    shape_physical_disks,
)
from ._base import DESTRUCTIVE, MUTATING, READ_ONLY, guarded, require_confirm

# ── Read logic ───────────────────────────────────────────────────────────────


async def fetch_array_status(client: UnraidClient) -> dict[str, Any]:
    return shape_array_status(await client.execute(queries.ARRAY_STATUS))


async def fetch_parity_status(client: UnraidClient) -> dict[str, Any]:
    data = await client.execute(queries.PARITY_STATUS)
    return (data.get("array") or {}).get("parityCheckStatus") or {}


async def fetch_parity_history(client: UnraidClient) -> list[dict[str, Any]]:
    return (await client.execute(queries.PARITY_HISTORY)).get("parityHistory") or []


async def fetch_disks(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_physical_disks(await client.execute(queries.LIST_DISKS))


def _disk_not_found(disk_id: str) -> ToolError:
    return ToolError(f"No disk matching '{disk_id}'. Use list_disks to see valid ids.")


async def fetch_disk(client: UnraidClient, disk_id: str) -> dict[str, Any]:
    try:
        data = await client.execute(queries.DISK_DETAILS, {"id": disk_id})
    except UnraidGraphQLError as exc:
        # The upstream resolver raises NotFoundException("Disk with id ${id} not
        # found") for unknown/malformed ids (see disks.service.ts). Match on that
        # narrow phrase so unrelated GraphQL errors (auth, other fields, etc.)
        # keep propagating as UnraidGraphQLError untouched.
        if "disk" in str(exc).lower() and "not found" in str(exc).lower():
            raise _disk_not_found(disk_id) from None
        raise
    disk = data.get("disk")
    if not disk:
        raise _disk_not_found(disk_id)
    return shape_physical_disk(disk)


# ── Mutation logic ─────────────────────────────────────────────────────────────


async def do_start_array(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "start the Unraid array")
    return shape_mutation_result(await client.execute(queries.START_ARRAY))


async def do_stop_array(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "stop the Unraid array (this unmounts all disks)")
    return shape_mutation_result(await client.execute(queries.STOP_ARRAY))


async def do_start_parity(client: UnraidClient, correct: bool, confirm: bool) -> dict[str, Any]:
    label = (
        "start a CORRECTING parity check (writes corrections to parity)"
        if correct
        else "start a parity check"
    )
    require_confirm(confirm, label)
    return shape_mutation_result(await client.execute(queries.START_PARITY, {"correct": correct}))


async def do_pause_parity(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "pause the parity check")
    return shape_mutation_result(await client.execute(queries.PAUSE_PARITY))


async def do_resume_parity(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "resume the parity check")
    return shape_mutation_result(await client.execute(queries.RESUME_PARITY))


async def do_cancel_parity(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, "cancel the parity check")
    return shape_mutation_result(await client.execute(queries.CANCEL_PARITY))


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_array_status(ctx: Context) -> dict[str, Any]:
        """Get the Unraid array: state, total/used/free capacity, every data/parity/cache
        disk with health, temperature and I/O counters, and live parity-check status."""
        return await guarded(ctx, fetch_array_status)

    @mcp.tool(annotations=READ_ONLY)
    async def get_parity_status(ctx: Context) -> dict[str, Any]:
        """Get the current parity-check status (progress, speed, errors, running/paused)."""
        return await guarded(ctx, fetch_parity_status)

    @mcp.tool(annotations=READ_ONLY)
    async def get_parity_history(ctx: Context) -> list[dict[str, Any]]:
        """Get the history of past parity checks (date, duration, speed, errors, status)."""
        return await guarded(ctx, fetch_parity_history)

    @mcp.tool(annotations=READ_ONLY)
    async def list_disks(ctx: Context) -> list[dict[str, Any]]:
        """List physical disks with model, size, interface, SMART status, temperature,
        and spin state. Use a disk id with get_disk for full details."""
        return await guarded(ctx, fetch_disks)

    @mcp.tool(annotations=READ_ONLY)
    async def get_disk(ctx: Context, disk_id: str) -> dict[str, Any]:
        """Get full details for one physical disk by its id (from list_disks),
        including partitions, firmware and SMART status. Errors (does not
        return null) if disk_id doesn't match a known disk — use list_disks
        to find a valid id."""
        return await guarded(ctx, fetch_disk, disk_id)


def register_mutations(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=MUTATING)
    async def start_array(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Start the Unraid array (brings storage online). Requires confirm=true."""
        return await guarded(ctx, do_start_array, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def stop_array(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Stop the Unraid array. Disruptive: unmounts all disks and stops dependent
        services. Requires confirm=true."""
        return await guarded(ctx, do_stop_array, confirm)

    @mcp.tool(annotations=MUTATING)
    async def start_parity_check(
        ctx: Context, correct: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Start a parity check. correct=false (default) only reports errors; correct=true
        writes corrections to parity — use with care, never on a degraded array.
        Requires confirm=true."""
        return await guarded(ctx, do_start_parity, correct, confirm)

    @mcp.tool(annotations=MUTATING)
    async def pause_parity_check(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Pause the running parity check. Requires confirm=true."""
        return await guarded(ctx, do_pause_parity, confirm)

    @mcp.tool(annotations=MUTATING)
    async def resume_parity_check(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Resume a paused parity check. Requires confirm=true."""
        return await guarded(ctx, do_resume_parity, confirm)

    @mcp.tool(annotations=MUTATING)
    async def cancel_parity_check(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Cancel the running parity check. Requires confirm=true."""
        return await guarded(ctx, do_cancel_parity, confirm)
