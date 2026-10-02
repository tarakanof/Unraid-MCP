"""Array, parity, and disk tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import (
    shape_array_status,
    shape_mutation_json_result,
    shape_mutation_result,
    shape_physical_disk,
    shape_physical_disks,
)
from ..types import Disk
from ._base import (
    DESTRUCTIVE,
    DESTRUCTIVE_IDEMPOTENT,
    DETAILS,
    MUTATING,
    MUTATING_IDEMPOTENT,
    READ_ONLY,
    Confirmation,
    Detail,
    contains_ci,
    execute_with_fallback,
    guarded,
    require_action,
    require_choice,
    require_confirm,
    require_confirmation,
    select_detail,
)

# ── Read logic ───────────────────────────────────────────────────────────────


async def fetch_array_status(client: UnraidClient) -> dict[str, Any]:
    return shape_array_status(
        await execute_with_fallback(client, queries.ARRAY_STATUS, queries.ARRAY_STATUS_LEGACY)
    )


async def fetch_parity_status(client: UnraidClient) -> dict[str, Any]:
    data = await client.execute(queries.PARITY_STATUS)
    return (data.get("array") or {}).get("parityCheckStatus") or {}


async def fetch_parity_history(client: UnraidClient) -> list[dict[str, Any]]:
    return (await client.execute(queries.PARITY_HISTORY)).get("parityHistory") or []


SmartStatus = Literal["OK", "UNKNOWN"]
_SMART_STATUSES: tuple[str, ...] = get_args(SmartStatus)
CONCISE_DISK_KEYS = ("id", "name", "device", "type", "smart_status", "temp_c", "spinning", "size")


async def fetch_disks(
    client: UnraidClient,
    *,
    name: str | None = None,
    disk_type: str | None = None,
    smart_status: str | None = None,
    detail: str = "full",
) -> list[Disk | None]:
    """List physical disks, filtered after the GraphQL call (#158). ``name``
    matches the model name or device path; ``disk_type`` (free-form upstream
    string, e.g. HD/SSD/NVMe) matches case-insensitively."""
    if smart_status is not None:
        require_choice("smart_status", smart_status, _SMART_STATUSES)
    require_choice("detail", detail, DETAILS)
    items = shape_physical_disks(await client.execute(queries.LIST_DISKS))
    if name is not None or disk_type is not None or smart_status is not None:
        items = [
            d
            for d in items
            if d is not None
            and contains_ci(name, d.get("name"), d.get("device"))
            and (disk_type is None or (d.get("type") or "").casefold() == disk_type.casefold())
            and (smart_status is None or d.get("smart_status") == smart_status)
        ]
    return select_detail(items, CONCISE_DISK_KEYS, detail)


def _disk_not_found(disk_id: str) -> ToolError:
    return ToolError(f"No disk matching '{disk_id}'. Use list_disks to see valid ids.")


async def fetch_disk(client: UnraidClient, disk_id: str) -> Disk:
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
    return shape_mutation_result(
        await client.execute(queries.START_ARRAY, timeout=client.long_request_timeout),
        ("array", "setState"),
    )


async def do_stop_array(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _stop_array_consequence())
    return shape_mutation_result(
        await client.execute(queries.STOP_ARRAY, timeout=client.long_request_timeout),
        ("array", "setState"),
    )


async def do_start_parity(client: UnraidClient, correct: bool, confirm: bool) -> dict[str, Any]:
    label = (
        "start a CORRECTING parity check (writes corrections to parity)"
        if correct
        else "start a parity check"
    )
    require_confirm(confirm, label)
    return shape_mutation_json_result(
        await client.execute(queries.START_PARITY, {"correct": correct}), ("parityCheck", "start")
    )


async def do_pause_parity(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _parity_control_consequence("pause"))
    return shape_mutation_json_result(
        await client.execute(queries.PAUSE_PARITY), ("parityCheck", "pause")
    )


async def do_resume_parity(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _parity_control_consequence("resume"))
    return shape_mutation_json_result(
        await client.execute(queries.RESUME_PARITY), ("parityCheck", "resume")
    )


async def do_cancel_parity(client: UnraidClient, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _parity_control_consequence("cancel"))
    return shape_mutation_json_result(
        await client.execute(queries.CANCEL_PARITY), ("parityCheck", "cancel")
    )


ParityControlAction = Literal["pause", "resume", "cancel"]
_PARITY_CONTROL_ACTIONS: tuple[str, ...] = get_args(ParityControlAction)
_PARITY_CONTROL_FNS = {
    "pause": do_pause_parity,
    "resume": do_resume_parity,
    "cancel": do_cancel_parity,
}


async def do_parity_check_control(
    client: UnraidClient, action: str, confirm: bool
) -> dict[str, Any]:
    """Pause/resume/cancel a running parity check (one ``MUTATING_IDEMPOTENT`` tool).

    start_parity_check is ``MUTATING`` (not idempotent) and stays separate.
    """
    require_action(action, _PARITY_CONTROL_ACTIONS)
    require_confirm(confirm, _parity_control_consequence(action))
    return await _PARITY_CONTROL_FNS[action](client, confirm)


def _parity_control_consequence(action: str) -> str:
    return f"{action} the parity check"


# ── Dangerous-tier logic ────────────────────────────────────────────────────


def _require_disk_id(disk_id: str) -> None:
    if not disk_id or not disk_id.strip():
        raise ToolError("disk_id must be a non-empty disk id (see list_disks for valid ids).")


async def do_mount_array_disk(client: UnraidClient, disk_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _mount_array_disk_consequence(disk_id))
    _require_disk_id(disk_id)
    return shape_mutation_result(
        await client.execute(queries.MOUNT_ARRAY_DISK, {"id": disk_id}), ("array", "mountArrayDisk")
    )


async def do_unmount_array_disk(
    client: UnraidClient, disk_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, _unmount_array_disk_consequence(disk_id))
    _require_disk_id(disk_id)
    return shape_mutation_result(
        await client.execute(queries.UNMOUNT_ARRAY_DISK, {"id": disk_id}),
        ("array", "unmountArrayDisk"),
    )


async def do_clear_disk_statistics(
    client: UnraidClient, disk_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, _clear_disk_statistics_consequence(disk_id))
    _require_disk_id(disk_id)
    return shape_mutation_result(
        await client.execute(queries.CLEAR_ARRAY_DISK_STATISTICS, {"id": disk_id}),
        ("array", "clearArrayDiskStatistics"),
    )


async def do_add_disk_to_array(
    client: UnraidClient, disk_id: str, slot: int | None = None, confirm: bool = False
) -> dict[str, Any]:
    require_confirm(confirm, _add_disk_to_array_consequence(disk_id))
    _require_disk_id(disk_id)
    if slot is not None and slot < 0:
        raise ToolError(f"slot must be a non-negative integer, got {slot}.")
    input_: dict[str, Any] = {"id": disk_id}
    if slot is not None:
        input_["slot"] = slot
    return shape_mutation_result(
        await client.execute(queries.ADD_DISK_TO_ARRAY, {"input": input_}),
        ("array", "addDiskToArray"),
    )


# Consequence strings are shared by the do_* gate and the elicitation resolvers
# so the human is shown exactly what the confirm-only refusal names.


def _stop_array_consequence() -> str:
    return "stop the Unraid array (this unmounts all disks)"


def _mount_array_disk_consequence(disk_id: str) -> str:
    return f"mount disk '{disk_id}' in the array (brings the disk online)"


def _unmount_array_disk_consequence(disk_id: str) -> str:
    return (
        f"unmount disk '{disk_id}' from the array (data on it becomes inaccessible until remounted)"
    )


def _clear_disk_statistics_consequence(disk_id: str) -> str:
    return (
        f"clear the read/write/error I/O statistics for disk '{disk_id}' "
        "(the counters are reset and cannot be recovered)"
    )


def _add_disk_to_array_consequence(disk_id: str) -> str:
    return (
        f"add disk '{disk_id}' to the array "
        "(the array must be stopped; assigning a slot can overwrite the disk)"
    )


def _confirm_stop_array(ctx: Context, confirm: bool) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _stop_array_consequence())


def _confirm_mount_array_disk(
    ctx: Context, confirm: bool, disk_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _mount_array_disk_consequence(disk_id))


def _confirm_unmount_array_disk(
    ctx: Context, confirm: bool, disk_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _unmount_array_disk_consequence(disk_id))


def _confirm_clear_disk_statistics(
    ctx: Context, confirm: bool, disk_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _clear_disk_statistics_consequence(disk_id))


def _confirm_add_disk_to_array(
    ctx: Context, confirm: bool, disk_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _add_disk_to_array_consequence(disk_id))


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Get Array Status", annotations=READ_ONLY)
    async def get_array_status(ctx: Context) -> dict[str, Any]:
        """Get the Unraid array: state, total/used/free capacity, every data/parity/cache
        disk with health, temperature and I/O counters, and live parity-check status."""
        return await guarded(ctx, fetch_array_status)

    @mcp.tool(title="Get Parity Status", annotations=READ_ONLY)
    async def get_parity_status(ctx: Context) -> dict[str, Any]:
        """Get the current parity-check status (progress, speed, errors, running/paused)."""
        return await guarded(ctx, fetch_parity_status)

    @mcp.tool(title="Get Parity History", annotations=READ_ONLY)
    async def get_parity_history(ctx: Context) -> list[dict[str, Any]]:
        """Get the history of past parity checks (date, duration, speed, errors, status)."""
        return await guarded(ctx, fetch_parity_history)

    @mcp.tool(title="List Disks", annotations=READ_ONLY)
    async def list_disks(
        ctx: Context,
        name: str | None = None,
        type: str | None = None,
        smart_status: SmartStatus | None = None,
        detail: Detail = "concise",
    ) -> list[Disk | None]:
        """List physical disks. Filter before listing everything: name (substring
        of model or device), type (HD/SSD/NVMe, case-insensitive), smart_status.
        detail="concise" returns id, name, device, type, smart_status, temp_c,
        spinning, size; "full" adds vendor, serial, interface. Use a disk id
        with get_disk for one disk's firmware and partitions."""
        return await guarded(
            ctx,
            fetch_disks,
            name=name,
            disk_type=type,
            smart_status=smart_status,
            detail=detail,
        )

    @mcp.tool(title="Get Disk", annotations=READ_ONLY)
    async def get_disk(ctx: Context, disk_id: str) -> Disk:
        """Get a physical disk by its id from list_disks (adds firmware and partitions).
        Errors if the id is unknown."""
        return await guarded(ctx, fetch_disk, disk_id)


def register_mutations(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Start Array", annotations=MUTATING_IDEMPOTENT)
    async def start_array(ctx: Context, confirm: bool = False) -> dict[str, Any]:
        """Start the Unraid array (brings storage online). Requires confirm=true."""
        return await guarded(ctx, do_start_array, confirm)

    @mcp.tool(title="Stop Array", annotations=DESTRUCTIVE_IDEMPOTENT)
    async def stop_array(
        ctx: Context,
        confirm: bool = False,
        *,
        confirmation: Annotated[ElicitationResult[Confirmation], Resolve(_confirm_stop_array)],
    ) -> dict[str, Any]:
        """Stop the Unraid array. Disruptive: unmounts all disks and stops dependent
        services. Requires confirm=true."""
        return await guarded(ctx, do_stop_array, confirm, confirmation=confirmation)

    @mcp.tool(title="Start Parity Check", annotations=MUTATING)
    async def start_parity_check(
        ctx: Context, correct: bool = False, confirm: bool = False
    ) -> dict[str, Any]:
        """Start a parity check. correct=false (default) only reports errors; correct=true
        writes corrections to parity — use with care, never on a degraded array.
        Requires confirm=true."""
        return await guarded(ctx, do_start_parity, correct, confirm)

    @mcp.tool(title="Parity Check Control", annotations=MUTATING_IDEMPOTENT)
    async def parity_check_control(
        ctx: Context, action: ParityControlAction, confirm: bool = False
    ) -> dict[str, Any]:
        """Pause, resume or cancel the parity check in progress (start one with
        start_parity_check). Requires confirm=true."""
        return await guarded(ctx, do_parity_check_control, action, confirm)


def register_dangerous(mcp: MCPServer, settings: Settings) -> None:
    """Dangerous-tier array-topology tools. Registered only when BOTH
    UNRAID_MCP_ALLOW_MUTATIONS and UNRAID_MCP_ALLOW_DANGEROUS are true."""

    @mcp.tool(title="Mount Array Disk", annotations=DESTRUCTIVE_IDEMPOTENT)
    async def mount_array_disk(
        ctx: Context,
        disk_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_mount_array_disk)
        ],
    ) -> dict[str, Any]:
        """DANGEROUS. Mount a single array disk by id (from list_disks), bringing it
        online. Operates on live storage — get the disk id right. Requires confirm=true."""
        return await guarded(ctx, do_mount_array_disk, disk_id, confirm, confirmation=confirmation)

    @mcp.tool(title="Unmount Array Disk", annotations=DESTRUCTIVE_IDEMPOTENT)
    async def unmount_array_disk(
        ctx: Context,
        disk_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_unmount_array_disk)
        ],
    ) -> dict[str, Any]:
        """DANGEROUS. Unmount a single array disk by id (from list_disks). Data on the
        disk becomes inaccessible to shares/services until it is remounted. Requires
        confirm=true."""
        return await guarded(
            ctx, do_unmount_array_disk, disk_id, confirm, confirmation=confirmation
        )

    @mcp.tool(title="Clear Disk Statistics", annotations=DESTRUCTIVE)
    async def clear_disk_statistics(
        ctx: Context,
        disk_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_clear_disk_statistics)
        ],
    ) -> dict[str, Any]:
        """DANGEROUS. Clear the read/write/error I/O counters for one array disk by id
        (from list_disks). The statistics are reset and cannot be recovered. Requires
        confirm=true."""
        return await guarded(
            ctx, do_clear_disk_statistics, disk_id, confirm, confirmation=confirmation
        )

    @mcp.tool(title="Add Disk To Array", annotations=DESTRUCTIVE)
    async def add_disk_to_array(
        ctx: Context,
        disk_id: str,
        slot: int | None = None,
        confirm: bool = False,
        *,
        confirmation: Annotated[
            ElicitationResult[Confirmation], Resolve(_confirm_add_disk_to_array)
        ],
    ) -> dict[str, Any]:
        """DANGEROUS. Assign a physical disk (id from list_disks) to the array, optionally
        at a specific slot. The array must be stopped first; assigning a disk to a data
        slot can overwrite it and, once started, will be formatted/rebuilt. Requires
        confirm=true."""
        return await guarded(
            ctx, do_add_disk_to_array, disk_id, slot, confirm, confirmation=confirmation
        )
