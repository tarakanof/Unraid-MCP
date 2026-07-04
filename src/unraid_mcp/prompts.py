"""Reusable MCP *prompts*.

A prompt encodes "how to investigate this box" once so any client can invoke it.
The ``triage`` prompt tells the agent to start from the health summary and then
drill into whichever subsystem needs attention, naming the exact tools to use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from .config import Settings


def _triage_instructions(focus: str) -> str:
    focus = (focus or "").strip()
    scope = (
        f"The operator is specifically worried about: {focus}. Prioritise that "
        "subsystem, but still confirm nothing else needs attention.\n\n"
        if focus
        else ""
    )
    return (
        "You are triaging an Unraid server through its MCP tools. Work top-down "
        "and stop as soon as you can give a clear verdict.\n\n"
        f"{scope}"
        "1. Call `get_health_summary` first. It rolls up array state, capacity, "
        "unhealthy disks, parity-check status, UPS state, and unread notification "
        "counts. Read its `overall` field: `ok` means no action, `attention` "
        "means dig deeper.\n"
        "2. Drill into whichever subsystem the summary flags:\n"
        "   - Unhealthy or missing disks -> call `get_disk` for each flagged "
        "device, and `get_array_status` for the full topology.\n"
        "   - Unread alert/warning notifications -> call `list_notifications` "
        "(filter to unread alerts/warnings) to read the actual messages.\n"
        "   - Parity problems -> inspect `parity_check` and `get_array_status`.\n"
        "   - UPS on battery / low charge -> call `get_ups_status`.\n"
        "   - Suspected host-level issue -> read the `unraid://system-info` "
        "resource or call `get_system_info` / `get_system_metrics`, and check "
        "`get_services` for offline services.\n"
        "3. Correlate anything time-sensitive with logs: `list_log_files` then "
        "`read_log_file` around the relevant timestamp.\n\n"
        "Report a concise verdict: overall status, each problem found with its "
        "evidence, and the single most useful next action. Do not run any "
        "mutating tool without explicit operator confirmation."
    )


def register_prompts(mcp: FastMCP, settings: Settings) -> None:
    @mcp.prompt(
        name="triage",
        title="Triage this Unraid server",
        description=(
            "Guided health triage: start from get_health_summary, then drill "
            "into whichever subsystem (disks, notifications, parity, UPS, "
            "services) is unhealthy. Optional `focus` narrows the investigation."
        ),
    )
    def triage(focus: str = "") -> str:
        """Investigate the health of the Unraid server, drilling into whatever
        subsystem the health summary flags as needing attention."""
        return _triage_instructions(focus)
