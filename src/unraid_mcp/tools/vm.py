"""Virtual machine tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import shape_mutation_result, shape_vms
from ._base import DESTRUCTIVE, MUTATING, READ_ONLY, guarded, require_confirm


def _is_missing_domains_field_error(exc: UnraidGraphQLError) -> bool:
    """True if the error is specifically GraphQL rejecting the `domains`
    field (older Unraid API builds only expose the legacy `domain` field)."""
    message = str(exc)
    return "Cannot query field" in message and "domains" in message


async def fetch_vms(client: UnraidClient) -> list[dict[str, Any]]:
    try:
        data = await client.execute(queries.LIST_VMS)
    except UnraidGraphQLError as exc:
        if not _is_missing_domains_field_error(exc):
            raise
        data = await client.execute(queries.LIST_VMS_LEGACY)
    return shape_vms(data)


async def do_start_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"start VM '{vm_id}'")
    return shape_mutation_result(await client.execute(queries.VM_START, {"id": vm_id}))


async def do_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"stop VM '{vm_id}'")
    return shape_mutation_result(await client.execute(queries.VM_STOP, {"id": vm_id}))


async def do_pause_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"pause VM '{vm_id}'")
    return shape_mutation_result(await client.execute(queries.VM_PAUSE, {"id": vm_id}))


async def do_resume_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"resume VM '{vm_id}'")
    return shape_mutation_result(await client.execute(queries.VM_RESUME, {"id": vm_id}))


async def do_reboot_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"reboot VM '{vm_id}'")
    return shape_mutation_result(await client.execute(queries.VM_REBOOT, {"id": vm_id}))


async def do_force_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"force-stop VM '{vm_id}' (hard power off)")
    return shape_mutation_result(await client.execute(queries.VM_FORCE_STOP, {"id": vm_id}))


async def do_reset_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(
        confirm, f"hard-reset VM '{vm_id}' (like the reset button — unsaved guest state is lost)"
    )
    return shape_mutation_result(await client.execute(queries.VM_RESET, {"id": vm_id}))


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def list_vms(ctx: Context) -> list[dict[str, Any]]:
        """List virtual machines with id, name, and state (state values come
        from the `VmState` enum)."""
        return await guarded(ctx, fetch_vms)


def register_mutations(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=MUTATING)
    async def start_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Start a VM by its id (from list_vms). Requires confirm=true."""
        return await guarded(ctx, do_start_vm, vm_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def stop_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Gracefully shut down a VM by id. Requires confirm=true."""
        return await guarded(ctx, do_stop_vm, vm_id, confirm)

    @mcp.tool(annotations=MUTATING)
    async def pause_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Pause a running VM by id. Requires confirm=true."""
        return await guarded(ctx, do_pause_vm, vm_id, confirm)

    @mcp.tool(annotations=MUTATING)
    async def resume_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Resume a paused VM by id. Requires confirm=true."""
        return await guarded(ctx, do_resume_vm, vm_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def reboot_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Reboot a VM by id. Requires confirm=true."""
        return await guarded(ctx, do_reboot_vm, vm_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def force_stop_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Force-stop (hard power off) a VM by id — may lose unsaved guest state.
        Requires confirm=true."""
        return await guarded(ctx, do_force_stop_vm, vm_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def reset_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Hard-reset a VM by id — like pressing the physical reset button;
        unsaved guest state is lost. Requires confirm=true."""
        return await guarded(ctx, do_reset_vm, vm_id, confirm)
