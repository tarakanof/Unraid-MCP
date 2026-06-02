"""Command-line entry point: load config, build the server, run a transport."""

from __future__ import annotations

import secrets

from .auth import StaticBearerAuthMiddleware
from .config import Settings, load_settings
from .errors import UnraidConfigError
from .logging import configure_logging, get_logger
from .server import build_server

log = get_logger(__name__)


def _serve_http(mcp, settings: Settings) -> None:
    import uvicorn

    token = settings.bearer_token.get_secret_value() if settings.bearer_token else None
    if not token:
        token = secrets.token_urlsafe(32)
        log.warning(
            "No UNRAID_MCP_BEARER_TOKEN set; generated one for this run. "
            "Clients must send 'Authorization: Bearer <token>':\n    %s",
            token,
        )
    app = StaticBearerAuthMiddleware(mcp.streamable_http_app(), token)
    log.info("Serving streamable-HTTP on http://%s:%s/mcp", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


def main() -> int:
    # Configure logging immediately (no key yet) so config errors reach stderr.
    configure_logging(level="INFO", api_key=None)
    try:
        settings = load_settings()
    except UnraidConfigError as exc:
        log.error("%s", exc)
        return 1

    # Reconfigure with the real level and a redaction filter for the API key.
    configure_logging(settings.log_level, settings.api_key.get_secret_value())
    if not settings.verify_ssl and not settings.ca_bundle:
        log.warning(
            "TLS verification is DISABLED (UNRAID_VERIFY_SSL=false). "
            "Prefer setting UNRAID_CA_BUNDLE to trust the Unraid certificate instead."
        )

    mcp = build_server(settings)
    if settings.transport == "streamable-http":
        _serve_http(mcp, settings)
    else:
        mcp.run(transport="stdio")
    return 0
