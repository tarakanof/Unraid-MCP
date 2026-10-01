"""Command-line entry point: load config, build the server, run a transport."""

from __future__ import annotations

import os
import secrets

from pydantic import SecretStr

from .auth import StaticBearerAuthMiddleware
from .config import Settings, load_settings
from .errors import UnraidConfigError
from .health import HealthCheckMiddleware
from .logging import configure_logging, get_logger
from .server import build_server, http_app

log = get_logger(__name__)


def _build_http_app(mcp, settings: Settings, token: str):
    """Compose the ASGI app: an unauthenticated ``/health`` in front of the bearer gate.

    Both wrappers are pure-ASGI and forward ``lifespan`` scopes untouched, so
    uvicorn's lifespan run reaches the Starlette app returned by
    :func:`~unraid_mcp.server.http_app` and starts its session manager.
    """
    inner = http_app(mcp, settings)
    return HealthCheckMiddleware(StaticBearerAuthMiddleware(inner, token))


def _with_bearer_token(settings: Settings) -> Settings:
    """Return settings carrying the effective HTTP bearer token.

    A configured token is kept. Otherwise one is generated and shown exactly
    once so the operator can configure their client (localhost binds only: a
    non-localhost bind without a token is refused with ``UnraidConfigError``).
    The effective token lives on the settings so the client, stats sampler and logging all scrub it.
    """
    if settings.bearer_token and settings.bearer_token.get_secret_value():
        return settings
    if not settings.binds_localhost:
        # A generated token would land in container logs that others may read.
        raise UnraidConfigError(
            f"UNRAID_MCP_BEARER_TOKEN is required when binding {settings.host} "
            "(non-localhost). Set it to a long random value; generate one with: "
            "python -c 'import secrets;print(secrets.token_urlsafe(32))'"
        )
    token = secrets.token_urlsafe(32)
    log.warning(
        "No UNRAID_MCP_BEARER_TOKEN set; generated one for this run. "
        "Clients must send 'Authorization: Bearer <token>':\n    %s",
        token,
    )
    return settings.model_copy(update={"bearer_token": SecretStr(token)})


def _log_secrets(settings: Settings) -> list[str]:
    return [settings.bearer_token.get_secret_value()] if settings.bearer_token else []


def _serve_http(mcp, settings: Settings) -> None:
    import uvicorn

    settings = _with_bearer_token(settings)
    token = settings.bearer_token.get_secret_value()  # type: ignore[union-attr]

    ssl_kwargs: dict[str, str] = {}
    if settings.tls_enabled:
        ssl_kwargs = {"ssl_certfile": settings.tls_cert, "ssl_keyfile": settings.tls_key}
    elif not settings.binds_localhost:
        log.warning(
            "Serving PLAINTEXT HTTP on a non-localhost address (%s): the bearer token "
            "travels unencrypted. Set UNRAID_MCP_TLS_CERT + UNRAID_MCP_TLS_KEY, or put "
            "this behind a TLS-terminating reverse proxy. Do not expose it directly.",
            settings.host,
        )

    if not settings.binds_localhost and not settings.allowed_hosts:
        log.warning(
            "Binding %s without UNRAID_MCP_ALLOWED_HOSTS: DNS-rebinding protection is "
            "off and only the bearer token guards access. Set UNRAID_MCP_ALLOWED_HOSTS "
            "for remote use.",
            settings.host,
        )

    scheme = "https" if settings.tls_enabled else "http"
    app = _build_http_app(mcp, settings, token)
    log.info("Serving streamable-HTTP on %s://%s:%s/mcp", scheme, settings.host, settings.port)
    # log_config=None lets uvicorn's loggers propagate to our root handler, so
    # they pass through the same stderr sink + secret-redaction filter.
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        log_config=None,
        **ssl_kwargs,
    )


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
    # Logging redaction covers the bearer token for every transport: libraries
    # (e.g. websockets at DEBUG) may log raw frames before our own scrubbing.
    if settings.transport == "streamable-http":
        try:
            settings = _with_bearer_token(settings)
        except UnraidConfigError as exc:
            log.error("%s", exc)
            return 1
    configure_logging(
        settings.log_level, settings.api_key.get_secret_value(), secrets=_log_secrets(settings)
    )
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
