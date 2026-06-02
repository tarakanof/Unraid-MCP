"""Build the FastMCP server: lifespan-managed GraphQL client + tool registration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .client import UnraidClient
from .config import Settings
from .logging import get_logger
from .tools import register_all

log = get_logger(__name__)

INSTRUCTIONS = (
    "Tools to monitor and (optionally) manage an Unraid server via its GraphQL API. "
    "Read tools cover system info, the array and disks, parity, Docker, VMs, shares, "
    "notifications, UPS, and network. Mutating tools are only present when enabled on "
    "the server; destructive ones require confirm=true. Sizes are reported in bytes with "
    "a human-readable form. Start with get_health_summary for a quick triage."
)


@dataclass
class AppContext:
    """Per-process context shared with every tool call via the lifespan."""

    client: UnraidClient
    settings: Settings


def _transport_security(settings: Settings) -> TransportSecuritySettings | None:
    """DNS-rebinding protection for the HTTP transport.

    Enabled for localhost binds (the safe default) or whenever the operator has
    provided an explicit host allow-list. For a non-localhost bind without an
    allow-list we leave it off — otherwise legitimate requests to the real
    hostname would be rejected — and rely on the bearer token (cli warns).
    """
    if settings.transport != "streamable-http":
        return None
    if settings.binds_localhost or settings.allowed_hosts:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.http_allowed_hosts(),
            allowed_origins=settings.http_allowed_origins(),
        )
    return None


def build_server(settings: Settings) -> FastMCP:
    """Construct a configured FastMCP server. Does not start any transport."""

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[AppContext]:
        async with httpx.AsyncClient(
            verify=settings.tls_verify(),
            timeout=settings.timeout,
            headers={"user-agent": "unraid-mcp"},
        ) as http:
            client = UnraidClient(
                settings.api_url,
                settings.api_key,
                http,
                host_label=settings.host_for_messages,
            )
            log.info(
                "unraid-mcp ready (target=%s, mutations=%s, raw_query=%s)",
                settings.host_for_messages,
                settings.allow_mutations,
                settings.allow_raw_query,
            )
            yield AppContext(client=client, settings=settings)

    mcp = FastMCP(
        "unraid",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.upper(),
        transport_security=_transport_security(settings),
    )
    register_all(mcp, settings)
    return mcp
