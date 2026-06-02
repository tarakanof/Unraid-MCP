"""Command-line entry point: load config, build the server, run a transport."""

from __future__ import annotations

import os
import secrets

from .auth import StaticBearerAuthMiddleware
from .config import Settings, load_settings
from .errors import UnraidConfigError
from .logging import configure_logging, get_logger
from .server import build_server

log = get_logger(__name__)


def _serve_http(mcp, settings: Settings) -> None:
    import uvicorn

    api_key = settings.api_key.get_secret_value()
    provided = settings.bearer_token.get_secret_value() if settings.bearer_token else None
    if provided:
        token = provided  # operator-supplied tokens are never logged
    else:
        token = secrets.token_urlsafe(32)
        log.warning(
            "No UNRAID_MCP_BEARER_TOKEN set; generated one for this run. "
            "Clients must send 'Authorization: Bearer <token>':\n    %s",
            token,
        )
    # Redact the bearer token from all subsequent logs (the generated one was
    # shown exactly once above so the operator can configure their client).
    configure_logging(settings.log_level, api_key, secrets=[token])

    if not settings.binds_localhost and not settings.allowed_hosts:
        log.warning(
            "Binding %s without UNRAID_MCP_ALLOWED_HOSTS: DNS-rebinding protection is "
            "off and only the bearer token guards access. Set UNRAID_MCP_ALLOWED_HOSTS "
            "and front this with a TLS reverse proxy for remote use.",
            settings.host,
        )
    app = StaticBearerAuthMiddleware(mcp.streamable_http_app(), token)
    log.info("Serving streamable-HTTP on http://%s:%s/mcp", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


def main() -> int:
    # Configure logging immediately so config errors reach stderr. Seed the
    # redaction filter straight from the environment so the key is scrubbed
    # even on the earliest log lines, before settings are parsed.
    configure_logging(level="INFO", api_key=os.environ.get("UNRAID_API_KEY"))
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
