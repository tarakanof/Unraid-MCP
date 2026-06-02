"""Docker container and network tools (reads + opt-in mutations)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .. import queries
from ..client import UnraidClient
from ..config import Settings
from ..formatting import shape_containers, shape_docker_networks
from ._base import DESTRUCTIVE, MUTATING, READ_ONLY, guarded, require_confirm


async def fetch_containers(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_containers(await client.execute(queries.LIST_CONTAINERS))


def _matches(container: dict[str, Any], identifier: str) -> bool:
    ident = identifier.lstrip("/")
    cid = container.get("id") or ""
    return cid == identifier or container.get("name") == ident or cid.split(":")[-1] == identifier


async def fetch_container(client: UnraidClient, identifier: str) -> dict[str, Any]:
    for container in await fetch_containers(client):
        if _matches(container, identifier):
            return container
    raise ToolError(f"No Docker container matching '{identifier}'.")


async def fetch_docker_networks(client: UnraidClient) -> list[dict[str, Any]]:
    return shape_docker_networks(await client.execute(queries.DOCKER_NETWORKS))


async def do_start_container(client: UnraidClient, container_id: str) -> dict[str, Any]:
    return await client.execute(queries.START_CONTAINER, {"id": container_id})


async def do_stop_container(
    client: UnraidClient, container_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, f"stop container '{container_id}'")
    return await client.execute(queries.STOP_CONTAINER, {"id": container_id})


async def do_restart_container(
    client: UnraidClient, container_id: str, confirm: bool
) -> dict[str, Any]:
    require_confirm(confirm, f"restart container '{container_id}'")
    await client.execute(queries.STOP_CONTAINER, {"id": container_id})
    return await client.execute(queries.START_CONTAINER, {"id": container_id})


def register(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=READ_ONLY)
    async def list_docker_containers(ctx: Context) -> list[dict[str, Any]]:
        """List Docker containers with id, name, image, state, status, autostart and ports."""
        return await guarded(ctx, fetch_containers)

    @mcp.tool(annotations=READ_ONLY)
    async def get_docker_container(ctx: Context, identifier: str) -> dict[str, Any]:
        """Get one Docker container by id or name."""
        return await guarded(ctx, fetch_container, identifier)

    @mcp.tool(annotations=READ_ONLY)
    async def list_docker_networks(ctx: Context) -> list[dict[str, Any]]:
        """List Docker networks with driver, scope, and flags."""
        return await guarded(ctx, fetch_docker_networks)


def register_mutations(mcp: FastMCP, settings: Settings) -> None:
    @mcp.tool(annotations=MUTATING)
    async def start_docker_container(ctx: Context, container_id: str) -> dict[str, Any]:
        """Start a Docker container by its id (get the id from list_docker_containers)."""
        return await guarded(ctx, do_start_container, container_id)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def stop_docker_container(
        ctx: Context, container_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Stop a running Docker container by id. Requires confirm=true."""
        return await guarded(ctx, do_stop_container, container_id, confirm)

    @mcp.tool(annotations=DESTRUCTIVE)
    async def restart_docker_container(
        ctx: Context, container_id: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Restart a Docker container by id (stop then start). Not atomic — if the
        start fails the container is left stopped. Requires confirm=true."""
        return await guarded(ctx, do_restart_container, container_id, confirm)
