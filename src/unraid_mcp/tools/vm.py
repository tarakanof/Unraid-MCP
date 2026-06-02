"""Virtual machine tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_vms
from ._base import DESTRUCTIVE, MUTATING, READ_ONLY, guarded, require_confirm


async def fetch_vms(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_vms(await client.execute(queries.LIST_VMS))


async def do_start_vm(client: UnraidClient, vm_id: str) -> dict[str, Any]:
    return await client.execute(queries.VM_START, {"id": vm_id})


async def do_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"stop VM '{vm_id}'")
    return await client.execute(queries.VM_STOP, {"id": vm_id})


async def do_pause_vm(client: UnraidClient, vm_id: str) -> dict[str, Any]:
    return await client.execute(queries.VM_PAUSE, {"id": vm_id})


async def do_resume_vm(client: UnraidClient, vm_id: str) -> dict[str, Any]:
    return await client.execute(queries.VM_RESUME, {"id": vm_id})


async def do_reboot_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"reboot VM '{vm_id}'")
    return await client.execute(queries.VM_REBOOT, {"id": vm_id})


async def do_force_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"force-stop VM '{vm_id}' (hard power off)")
    return await client.execute(queries.VM_FORCE_STOP, {"id": vm_id})


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def list_vms(ctx: Context) -> list[dict[str, Any]]:
        """List virtual machines with id, name, and state."""
        return await guarded(ctx, fetch_vms)


def register_mutations(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=MUTATING)
    async def start_vm(ctx: Context, vm_id: str) -> dict[str, Any]:
        """Start a VM by its id (from list_vms)."""
        return await guarded(ctx, do_start_vm, vm_id)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def stop_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Gracefully shut down a VM by id. Requires confirm=true."""
        return await guarded(ctx, do_stop_vm, vm_id, confirm)

    @mcp.tool(annotations=MUTATING)
    async def pause_vm(ctx: Context, vm_id: str) -> dict[str, Any]:
        """Pause a running VM by id."""
        return await guarded(ctx, do_pause_vm, vm_id)

    @mcp.tool(annotations=MUTATING)
    async def resume_vm(ctx: Context, vm_id: str) -> dict[str, Any]:
        """Resume a paused VM by id."""
        return await guarded(ctx, do_resume_vm, vm_id)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def reboot_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Reboot a VM by id. Requires confirm=true."""
        return await guarded(ctx, do_reboot_vm, vm_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def force_stop_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Force-stop (hard power off) a VM by id — may lose unsaved guest state.
        Requires confirm=true."""
        return await guarded(ctx, do_force_stop_vm, vm_id, confirm)
