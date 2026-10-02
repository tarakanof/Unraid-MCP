"""Virtual machine tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

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
    CaseInsensitive,
    Confirmation,
    contains_ci,
    guarded,
    local_id,
    require_action,
    require_choice,
    require_confirm,
    require_confirmation,
    upper_if_str,
)


def _is_missing_domains_field_error(exc: UnraidGraphQLError) -> bool:
    """True if the error is specifically GraphQL rejecting the `domains`
    field (older Unraid API builds only expose the legacy `domain` field)."""
    message = str(exc)
    return "Cannot query field" in message and "domains" in message


VmState = Literal[
    "NOSTATE", "RUNNING", "IDLE", "PAUSED", "SHUTDOWN", "SHUTOFF", "CRASHED", "PMSUSPENDED"
]
_VM_STATES: tuple[str, ...] = get_args(VmState)


async def fetch_vms(
    client: UnraidClient, *, name: str | None = None, state: str | None = None
) -> list[dict[str, Any]]:
    """List VMs, filtered after the GraphQL call (#158). VMs are already
    shaped to id/name/state, so there is no ``detail`` level."""
    state = upper_if_str(state)
    if state is not None:
        require_choice("state", state, _VM_STATES)
    try:
        data = await client.execute(queries.LIST_VMS)
    except UnraidGraphQLError as exc:
        if not _is_missing_domains_field_error(exc):
            raise
        data = await client.execute(queries.LIST_VMS_LEGACY)
    return [
        v
        for v in shape_vms(data)
        if contains_ci(name, v.get("name")) and (state is None or v.get("state") == state)
    ]


async def do_start_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _vm_power_consequence(vm_id, "start"))
    return shape_mutation_result(
        await client.execute(queries.VM_START, {"id": local_id(vm_id)}), ("vm", "start")
    )


async def do_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _stop_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_STOP, {"id": local_id(vm_id)}), ("vm", "stop")
    )


async def do_pause_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _vm_power_consequence(vm_id, "pause"))
    return shape_mutation_result(
        await client.execute(queries.VM_PAUSE, {"id": local_id(vm_id)}), ("vm", "pause")
    )


async def do_resume_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _vm_power_consequence(vm_id, "resume"))
    return shape_mutation_result(
        await client.execute(queries.VM_RESUME, {"id": local_id(vm_id)}), ("vm", "resume")
    )


async def do_reboot_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _reboot_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_REBOOT, {"id": local_id(vm_id)}), ("vm", "reboot")
    )


async def do_force_stop_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _force_stop_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_FORCE_STOP, {"id": local_id(vm_id)}), ("vm", "forceStop")
    )


async def do_reset_vm(client: UnraidClient, vm_id: str, confirm: bool) -> dict[str, Any]:
    require_confirm(confirm, _reset_vm_consequence(vm_id))
    return shape_mutation_result(
        await client.execute(queries.VM_RESET, {"id": local_id(vm_id)}), ("vm", "reset")
    )


VmPowerAction = Literal["start", "pause", "resume"]
_VM_POWER_ACTIONS: tuple[str, ...] = get_args(VmPowerAction)
_VM_POWER_FNS = {"start": do_start_vm, "pause": do_pause_vm, "resume": do_resume_vm}


async def do_vm_power(
    client: UnraidClient, vm_id: str, action: str, confirm: bool
) -> dict[str, Any]:
    """Non-destructive VM power actions (one ``MUTATING_IDEMPOTENT`` tool).

    stop/reboot/force-stop/reset are DESTRUCTIVE and keep their own tools.
    """
    require_action(action, _VM_POWER_ACTIONS)
    require_confirm(confirm, _vm_power_consequence(vm_id, action))
    return await _VM_POWER_FNS[action](client, vm_id, confirm)


def _vm_power_consequence(vm_id: str, action: str) -> str:
    return f"{action} VM '{vm_id}'"


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
    async def list_vms(
        ctx: Context,
        name: str | None = None,
        state: Annotated[VmState | None, CaseInsensitive] = None,
    ) -> list[dict[str, Any]]:
        """List virtual machines (id, name, state). Filter before listing
        everything: name (case-insensitive substring), state."""
        return await guarded(ctx, fetch_vms, name=name, state=state)


def register_mutations(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(title="VM Power", annotations=MUTATING_IDEMPOTENT)
    async def vm_power(
        ctx: Context, vm_id: str, action: VmPowerAction, confirm: bool = False
    ) -> dict[str, Any]:
        """Start, pause or resume a VM by its id (from list_vms). To shut down,
        reboot, force-stop or reset use stop_vm / reboot_vm / force_stop_vm /
        reset_vm. Requires confirm=true."""
        return await guarded(ctx, do_vm_power, vm_id, action, confirm)

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
