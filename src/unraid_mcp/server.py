"""Build the MCP server: lifespan-managed GraphQL client + tool registration."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING

import httpx
from mcp.server import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from . import queries
from .client import UnraidClient
from .config import Settings
from .errors import UnraidError
from .logging import get_logger
from .prompts import register_prompts
from .resources import register_resources
from .tools import register_all

if TYPE_CHECKING:  # pragma: no cover - typing only (starlette arrives with the SDK)
    from starlette.applications import Starlette

log = get_logger(__name__)

INSTRUCTIONS = (
    "Tools to monitor and (optionally) manage an Unraid server via its GraphQL API. "
    "Read tools cover system info, the array and disks, parity, Docker, VMs, shares, "
    "notifications, UPS, and network. Mutating tools are only present when enabled on "
    "the server; destructive ones require confirm=true. Sizes are reported in bytes with "
    "a human-readable form. Start with get_health_summary for a quick triage."
)

# Cache hints (2026-07-28, SEP-2549): ``MCPServer(cache_hints=...)`` fills
# ttlMs/cacheScope on any cacheable-method result the handler leaves unset.
# 1h: the registered tool/prompt/resource set is fixed per process (mutations
# toggle only via env at startup).
_LIST_CACHE_TTL_MS = 60 * 60 * 1000
# 10s: health/system-info resources are point-in-time snapshots, so keep
# freshness tight.
_RESOURCE_READ_CACHE_TTL_MS = 10_000

CACHE_HINTS: dict[str, CacheHint] = {
    "tools/list": CacheHint(ttl_ms=_LIST_CACHE_TTL_MS, scope="public"),
    "prompts/list": CacheHint(ttl_ms=_LIST_CACHE_TTL_MS, scope="public"),
    "resources/list": CacheHint(ttl_ms=_LIST_CACHE_TTL_MS, scope="public"),
    "resources/read": CacheHint(ttl_ms=_RESOURCE_READ_CACHE_TTL_MS),
}


def _server_version() -> str:
    """Our own version for ``serverInfo.version``.

    SDK v1 filled the field with the SDK's version when the server left it
    unset; v2 leaves it empty. Report the distribution version instead so clients
    still see a meaningful build identifier.
    """
    try:
        return metadata.version("unraid-mcp")
    except metadata.PackageNotFoundError:  # pragma: no cover - source checkout only
        from . import __version__

        return __version__


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

    The SDK auto-enables its own localhost allow-list when the app is built with
    ``transport_security=None`` and a localhost ``host``; we always answer for
    the localhost case ourselves so the allow-list also covers the configured
    port, and :func:`http_app` passes the real bind host so the SDK's default
    never fires behind our back on the "leave it off" branch.
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


def http_app(mcp: MCPServer, settings: Settings) -> Starlette:
    """Build the streamable-HTTP ASGI app for ``mcp``.

    v2 moved the transport knobs out of the constructor, so host and DNS-rebinding
    settings are applied here (and by :meth:`MCPServer.run` for stdio). The
    returned Starlette app owns the session-manager lifespan, which uvicorn runs
    via the outermost app in ``cli._build_http_app`` (our ASGI middlewares pass
    ``lifespan`` scopes straight through).

    ``stateless_http=True`` (MCP spec 2026-07-28) is always on, unconditionally
    — there is no setting to disable it. Note the flag only changes the legacy
    (pre-2026) request path: modern requests are routed to
    ``handle_modern_request`` regardless and were already sessionless with
    ``can_send_request=False``. What the flag buys is sessionless service for
    pre-2026 clients too. This server never sends a server-initiated request
    (no sampling/elicitation/roots), so the one capability the legacy path
    gives up (``can_send_request=False`` on the per-request channel,
    ``streamable_http_manager.py:222``) costs nothing here. A legacy client
    that omits ``MCP-Protocol-Version`` on follow-ups is served at the SDK's
    ``DEFAULT_NEGOTIATED_VERSION`` rather than what its ``initialize``
    negotiated — no observable difference on this server's surface today, but
    version-gated SDK behavior could change that. ``json_response`` is
    deliberately left at its default (``False``, SSE-per-response): nothing
    requires pairing it with stateless mode, and flipping it would be an
    unrelated behavior change.
    Evidence for "always on, no flag" (installed ``mcp`` 2.0.0 source):

    - The session manager enters ``app.lifespan(app)`` exactly once for the
      manager's lifetime regardless of ``stateless``, and every per-request
      transport reuses that single ``lifespan_state``
      (``streamable_http_manager.py:145-149,235``) — stateless mode cannot
      trigger the ``build_server`` re-entry guard more than once per process.
    - Pre-2026 ("legacy") clients still work transparently: each stateless
      request builds its connection via ``Connection.from_envelope(...)``
      (``streamable_http_manager.py:228-232``), which unconditionally calls
      ``connection.initialized.set()`` (``connection.py:296``), so
      ``initialize_accepted`` is already true
      (``connection.py:333-337``) before the request's own method runs. A
      legacy client's ``initialize`` is still served correctly (the
      dispatcher special-cases it via ``inline_methods={"initialize"}``,
      ``streamable_http_manager.py:216``, handled in
      ``runner.py:201-202``); the request-gate check that would otherwise
      reject a not-yet-initialized connection
      (``runner.py:211``) never fires, because every stateless connection is
      born pre-initialized. No session ID is ever handed out
      (``mcp_session_id=None``, ``streamable_http_manager.py:202``), which
      legacy clients tolerate as a sessionless server.
    """
    return mcp.streamable_http_app(
        host=settings.host,
        transport_security=_transport_security(settings),
        stateless_http=True,
    )


def build_server(settings: Settings) -> MCPServer:
    """Construct a configured :class:`MCPServer`. Does not start any transport."""

    # Static resources cannot receive an injected ``Context`` in SDK v2 (only
    # URI templates can), and ``get_context()`` is gone, so the lifespan hands
    # the AppContext to this per-server holder for resource reads to pick up.
    # It is a closure cell, not module state (two servers never share it), and
    # it holds exactly one context: the lifespan refuses concurrent re-entry.
    running: dict[str, AppContext] = {}

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AppContext]:
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
            app_context = AppContext(
                client=client,
                settings=settings,
                api_version=api_version,
                unraid_version=unraid_version,
            )
            if "context" in running:
                # One holder per server: a second concurrent lifespan would make
                # resource reads silently use the wrong httpx client, then fail
                # outright when the first lifespan exits. Fail loud instead.
                raise RuntimeError(
                    "unraid-mcp server lifespan entered twice concurrently; "
                    "build a separate server per transport instead"
                )
            running["context"] = app_context
            try:
                yield app_context
            finally:
                running.pop("context", None)

    mcp = MCPServer(
        "unraid",
        instructions=INSTRUCTIONS,
        version=_server_version(),
        lifespan=lifespan,
        log_level=settings.log_level.upper(),
        cache_hints=CACHE_HINTS,
    )
    register_all(mcp, settings)
    # Resources and prompts are always on: both are read-only and reuse the
    # same fetch_* functions the read tools do (no new GraphQL surface).
    register_resources(mcp, settings, lambda: running.get("context"))
    register_prompts(mcp, settings)
    return mcp
