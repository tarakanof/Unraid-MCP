"""Build the FastMCP server: lifespan-managed GraphQL client + tool registration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import queries
from .client import UnraidClient
from .config import Settings
from .errors import UnraidError
from .logging import get_logger
from .prompts import register_prompts
from .resources import register_resources
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
    # Versions probed once at startup (see lifespan). Left None if the probe
    # fails or the fields are absent — never blocks startup. Tools read these to
    # explain capability gaps (see tools/_base.feature_unsupported).
    api_version: str | None = None
    unraid_version: str | None = None


async def _probe_versions(client: UnraidClient) -> tuple[str | None, str | None]:
    """Resolve (api_version, unraid_version) via a cheap GraphQL probe.

    Never raises: any failure returns (None, None). Missing keys degrade to
    None so startup is never blocked by an old/limited API build.
    """
    try:
        data = await client.execute(queries.API_PROBE)
    except UnraidError as exc:
        log.warning("API version probe failed; continuing without versions: %s", exc)
        return None, None
    except Exception as exc:  # noqa: BLE001 - startup must never be blocked by the probe
        log.warning("API version probe raised unexpectedly; continuing: %s", exc)
        return None, None
    core = ((data or {}).get("info") or {}).get("versions") or {}
    core = core.get("core") or {}
    return core.get("api"), core.get("unraid")


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
            trust_env=False,
        ) as http:
            client = UnraidClient(
                settings.api_url,
                settings.api_key,
                http,
                host_label=settings.host_for_messages,
            )
            api_version, unraid_version = await _probe_versions(client)
            log.info(
                "unraid-mcp ready (target=%s, mutations=%s, raw_query=%s, api=%s, unraid=%s)",
                settings.host_for_messages,
                settings.allow_mutations,
                settings.allow_raw_query,
                api_version,
                unraid_version,
            )
            yield AppContext(
                client=client,
                settings=settings,
                api_version=api_version,
                unraid_version=unraid_version,
            )

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
    # Resources and prompts are always on: both are read-only and reuse the
    # same fetch_* functions the read tools do (no new GraphQL surface).
    register_resources(mcp, settings)
    register_prompts(mcp, settings)
    return mcp
