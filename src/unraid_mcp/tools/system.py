"""System information tools."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..errors import UnraidGraphQLError
from ..formatting import (
    HARDWARE_KINDS,
    shape_flash,
    shape_hardware_inventory,
    shape_metrics,
    shape_services,
    shape_system_info,
    shape_system_time,
)
from ._base import (
    READ_ONLY,
    feature_unsupported,
    gather_all,
    get_app_context,
    guarded,
    safe_query,
    unsupported_field_error,
)


async def fetch_system_info(client: UnraidClient) -> dict[str, Any]:
    info_data, flash = await gather_all(
        client.execute(queries.SYSTEM_INFO),
        safe_query(client, queries.FLASH, shape_flash, None),
    )
    info = shape_system_info(info_data)
    # `flash` is a separate root query (not nested under `info`), fetched here
    # as a second, independently-degrading call so older API builds without
    # this field still return system info — just without flash device identity.
    if flash is not None:
        info = {**info, "flash": flash}
    return info


async def fetch_system_time(
    client: UnraidClient, *, api_version: str | None = None
) -> dict[str, Any]:
    try:
        return shape_system_time(await client.execute(queries.SYSTEM_TIME))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "system time info", requires="7.1+", api_version=api_version
            ) from None
        raise


async def fetch_metrics(client: UnraidClient, *, api_version: str | None = None) -> dict[str, Any]:
    try:
        return shape_metrics(await client.execute(queries.SYSTEM_METRICS))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "live system metrics", requires="7.2+", api_version=api_version
            ) from None
        raise


async def fetch_services(
    client: UnraidClient, *, api_version: str | None = None
) -> list[dict[str, Any]]:
    try:
        return shape_services(await client.execute(queries.SERVICES))
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported("the services health list", api_version=api_version) from None
        raise


async def fetch_hardware_inventory(
    client: UnraidClient, kind: str | None = None, *, api_version: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    if kind is not None and kind not in HARDWARE_KINDS:
        raise ToolError(f"Unknown kind {kind!r}; expected one of: {', '.join(HARDWARE_KINDS)}.")
    try:
        return shape_hardware_inventory(await client.execute(queries.HARDWARE_INVENTORY), kind)
    except UnraidGraphQLError as exc:
        if unsupported_field_error(exc):
            raise feature_unsupported(
                "the hardware inventory (info.devices)", api_version=api_version
            ) from None
        raise


def register(mcp: MCPServer, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def get_system_info(ctx: Context) -> dict[str, Any]:
        """Get Unraid host system information: OS/kernel, CPU, memory layout,
        motherboard, Unraid + API versions, and uptime."""
        info = await guarded(ctx, fetch_system_info)
        # Surface the startup-probed versions explicitly at the top level so
        # agents can self-diagnose capability gaps even if info.versions is
        # absent on this API build.
        app = get_app_context(ctx)
        return {
            **info,
            "api_version": app.api_version,
            "unraid_version": app.unraid_version,
        }

    @mcp.tool(annotations=READ_ONLY)
    async def get_system_metrics(ctx: Context) -> dict[str, Any]:
        """Get live utilization: total/per-core CPU %, memory/swap usage,
        temperatures."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_metrics, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def get_services(ctx: Context) -> list[dict[str, Any]]:
        """Health of the Unraid services stack (API, dynamix, etc.): name,
        online, uptime, version."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_services, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def get_system_time(ctx: Context) -> dict[str, Any]:
        """Get server time, timezone, and NTP config — correlate log timestamps
        and spot NTP misconfig."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_system_time, api_version=api_version)

    @mcp.tool(annotations=READ_ONLY)
    async def get_hardware_inventory(
        ctx: Context, kind: Literal["gpu", "pci", "usb", "network"] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """List detected hardware: GPUs, PCI devices (incl. blacklisted/passthrough
        status), USB devices, and network adapters. Optional `kind` returns only
        one type. Answers "is my GPU visible / passed through?"."""
        api_version = get_app_context(ctx).api_version
        return await guarded(ctx, fetch_hardware_inventory, kind, api_version=api_version)
