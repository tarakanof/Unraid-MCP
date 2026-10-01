"""Virtual machine tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context, Elicit, ElicitationResult, MCPServer, Resolve

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import shape_mutation_result, shape_vms
from ._base import (
    DESTRUCTIVE,
    DESTRUCTIVE_IDEMPOTENT,
    MUTATING_IDEMPOTENT,
    READ_ONLY,
    Confirmation,
    guarded,
    require_confirm,
    require_confirmation,
)


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
    return shape_mutation_result(
        await client.execute(queries.VM_START, {"id": vm_id}), ("vm", "start")
    )


async def do_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _stop_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_STOP, {"id": vm_id}), ("vm", "stop")
    )


async def do_pause_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"pause VM '{vm_id}'")
    return shape_mutation_result(
        await client.execute(queries.VM_PAUSE, {"id": vm_id}), ("vm", "pause")
    )


async def do_resume_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, f"resume VM '{vm_id}'")
    return shape_mutation_result(
        await client.execute(queries.VM_RESUME, {"id": vm_id}), ("vm", "resume")
    )


async def do_reboot_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _reboot_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_REBOOT, {"id": vm_id}), ("vm", "reboot")
    )


async def do_force_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _force_stop_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_FORCE_STOP, {"id": vm_id}), ("vm", "forceStop")
    )


async def do_reset_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _reset_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_RESET, {"id": vm_id}), ("vm", "reset")
    )


def _stop_vm_consequence(vm_id: str) -> str:
    return f"stop VM '{vm_id}'"


def _reboot_vm_consequence(vm_id: str) -> str:
    return f"reboot VM '{vm_id}'"


def _force_stop_vm_consequence(vm_id: str) -> str:
    return f"force-stop VM '{vm_id}' (hard power off)"


def _reset_vm_consequence(vm_id: str) -> str:
    return f"hard-reset VM '{vm_id}' (like the reset button — unsaved guest state is lost)"


def _confirm_stop_vm(
    ctx: Context, confirm: bool, vm_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _stop_vm_consequence(vm_id))


def _confirm_reboot_vm(
    ctx: Context, confirm: bool, vm_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _reboot_vm_consequence(vm_id))


def _confirm_force_stop_vm(
    ctx: Context, confirm: bool, vm_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _force_stop_vm_consequence(vm_id))


def _confirm_reset_vm(
    ctx: Context, confirm: bool, vm_id: str
) -> Confirmation | Elicit[Confirmation]:
    return require_confirmation(ctx, confirm, _reset_vm_consequence(vm_id))


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="List VMs", annotations=READ_ONLY)
    async def list_vms(ctx: Context) -> list[dict[str, Any]]:
        """List virtual machines with id, name, and state (state values come
        from the `VmState` enum)."""
        return await guarded(ctx, fetch_vms)


def register_mutations(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="Start VM", annotations=MUTATING_IDEMPOTENT)
    async def start_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Start a VM by its id (from list_vms). Requires confirm=true."""
        return await guarded(ctx, do_start_vm, vm_id, confirm)

    @mcp.tool(title="Stop VM", annotations=DESTRUCTIVE_IDEMPOTENT)
    async def stop_vm(
        ctx: Context,
        vm_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[ElicitationResult[Confirmation], Resolve(_confirm_stop_vm)],
    ) -> dict[str, Any]:
        """Gracefully shut down a VM by id. Requires confirm=true."""
        return await guarded(ctx, do_stop_vm, vm_id, confirm, confirmation=confirmation)

    @mcp.tool(title="Pause VM", annotations=MUTATING_IDEMPOTENT)
    async def pause_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Pause a running VM by id. Requires confirm=true."""
        return await guarded(ctx, do_pause_vm, vm_id, confirm)

    @mcp.tool(title="Resume VM", annotations=MUTATING_IDEMPOTENT)
    async def resume_vm(ctx: Context, vm_id: str, confirm: bool = False) -> dict[str, Any]:
        """Resume a paused VM by id. Requires confirm=true."""
        return await guarded(ctx, do_resume_vm, vm_id, confirm)

    @mcp.tool(title="Reboot VM", annotations=DESTRUCTIVE)
    async def reboot_vm(
        ctx: Context,
        vm_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[ElicitationResult[Confirmation], Resolve(_confirm_reboot_vm)],
    ) -> dict[str, Any]:
        """Reboot a VM by id. Requires confirm=true."""
        return await guarded(ctx, do_reboot_vm, vm_id, confirm, confirmation=confirmation)

    @mcp.tool(title="Force Stop VM", annotations=DESTRUCTIVE_IDEMPOTENT)
    async def force_stop_vm(
        ctx: Context,
        vm_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[ElicitationResult[Confirmation], Resolve(_confirm_force_stop_vm)],
    ) -> dict[str, Any]:
        """Force-stop (hard power off) a VM by id — may lose unsaved guest state.
        Requires confirm=true."""
        return await guarded(ctx, do_force_stop_vm, vm_id, confirm, confirmation=confirmation)

    @mcp.tool(title="Reset VM", annotations=DESTRUCTIVE)
    async def reset_vm(
        ctx: Context,
        vm_id: str,
        confirm: bool = False,
        *,
        confirmation: Annotated[ElicitationResult[Confirmation], Resolve(_confirm_reset_vm)],
    ) -> dict[str, Any]:
        """Hard-reset a VM by id — like pressing the physical reset button;
        unsaved guest state is lost. Requires confirm=true."""
        return await guarded(ctx, do_reset_vm, vm_id, confirm, confirmation=confirmation)
