"""Configuration for the Unraid MCP server, loaded from environment variables.

All settings come from ``UNRAID_*`` environment variables (optionally via a
``.env`` file). The API key is held as a ``SecretStr`` so it never leaks into
logs, ``repr()``, or error messages.
"""

from __future__ import annotations

import ssl
from typing import Literal
from urllib.parse import urlparse, urlunparse

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import UnraidConfigError

Transport = Literal["stdio", "streamable-http"]
MIN_BEARER_TOKEN_LENGTH = 32

_INSECURE_BEARER_TOKENS = {
    "change-me",
    "change-me-to-a-long-random-token",
    "changeme",
    "replace-me",
    "token",
    "your-token",
    "your-bearer-token",
    "your-unraid-mcp-bearer-token",
}


class Settings(BaseSettings):
    """Validated runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Unraid connection ──────────────────────────────────────────────
    api_url: str = Field(validation_alias="UNRAID_API_URL")
    api_key: SecretStr = Field(validation_alias="UNRAID_API_KEY")

    # ── TLS ────────────────────────────────────────────────────────────
    verify_ssl: bool = Field(default=True, validation_alias="UNRAID_VERIFY_SSL")
    ca_bundle: str | None = Field(default=None, validation_alias="UNRAID_CA_BUNDLE")

    # ── Transport ──────────────────────────────────────────────────────
    transport: Transport = Field(default="stdio", validation_alias="UNRAID_MCP_TRANSPORT")
    host: str = Field(default="127.0.0.1", validation_alias="UNRAID_MCP_HOST")
    port: int = Field(default=6750, validation_alias="UNRAID_MCP_PORT")
    bearer_token: SecretStr | None = Field(default=None, validation_alias="UNRAID_MCP_BEARER_TOKEN")
    # Comma-separated Host / Origin allow-lists for DNS-rebinding protection on
    # the HTTP transport. Required when binding to a non-localhost address.
    allowed_hosts: str | None = Field(default=None, validation_alias="UNRAID_MCP_ALLOWED_HOSTS")
    allowed_origins: str | None = Field(default=None, validation_alias="UNRAID_MCP_ALLOWED_ORIGINS")
    # Serve the HTTP transport over TLS directly. Both must be set to enable it;
    # otherwise terminate TLS at a reverse proxy in front of this server.
    tls_cert: str | None = Field(default=None, validation_alias="UNRAID_MCP_TLS_CERT")
    tls_key: str | None = Field(default=None, validation_alias="UNRAID_MCP_TLS_KEY")

    # ── Safety switches ────────────────────────────────────────────────
    allow_mutations: bool = Field(default=False, validation_alias="UNRAID_MCP_ALLOW_MUTATIONS")
    # Third tier for high-blast-radius mutations (array topology, container
    # removal). Only takes effect when ``allow_mutations`` is ALSO true — enabling
    # this alone unlocks nothing (see tools.register_all).
    allow_dangerous: bool = Field(default=False, validation_alias="UNRAID_MCP_ALLOW_DANGEROUS")
    allow_raw_query: bool = Field(default=False, validation_alias="UNRAID_MCP_ALLOW_RAW_QUERY")

    # ── Misc ───────────────────────────────────────────────────────────
    timeout: float = Field(default=30.0, validation_alias="UNRAID_MCP_TIMEOUT")
    log_level: str = Field(default="INFO", validation_alias="UNRAID_MCP_LOG_LEVEL")

    @field_validator("api_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                "UNRAID_API_URL must be an http(s) URL, e.g. https://yourhash.myunraid.net/graphql"
            )
        if not parsed.netloc:
            raise ValueError(
                "UNRAID_API_URL must include a host, e.g. https://yourhash.myunraid.net/graphql"
            )
        # Append the GraphQL path if the user gave only a base URL.
        if parsed.path in ("", "/"):
            value = value.rstrip("/") + "/graphql"
        return value

    @field_validator("bearer_token", mode="before")
    @classmethod
    def _validate_bearer_token(cls, value: object) -> object:
        if value is None:
            return None
        token = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        if not token:
            return None
        if token.strip() != token:
            raise ValueError("UNRAID_MCP_BEARER_TOKEN must not have leading/trailing whitespace")
        if token.lower() in _INSECURE_BEARER_TOKENS:
            raise ValueError(
                "UNRAID_MCP_BEARER_TOKEN must be a long random value, not a placeholder"
            )
        if len(token) < MIN_BEARER_TOKEN_LENGTH:
            raise ValueError(
                f"UNRAID_MCP_BEARER_TOKEN must be at least {MIN_BEARER_TOKEN_LENGTH} characters"
            )
        return value

    @property
    def host_for_messages(self) -> str:
        """The bare host:port, safe to show in user-facing error messages."""
        return urlparse(self.api_url).netloc or self.api_url

    def tls_verify(self) -> bool | str:
        """Value for ``httpx`` ``verify``: a CA-bundle path if set, else the bool."""
        return self.ca_bundle if self.ca_bundle else self.verify_ssl

    def ws_url(self) -> str:
        """The ``ws(s)://`` endpoint derived from ``api_url``.

        Subscriptions use the same host/path as the HTTP GraphQL endpoint but a
        websocket scheme: ``wss`` from ``https``, ``ws`` from ``http``.
        """
        parsed = urlparse(self.api_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunparse((scheme, parsed.netloc, parsed.path or "/graphql", "", "", ""))

    def ssl_context(self) -> ssl.SSLContext | None:
        """``ssl.SSLContext`` for the websocket path, mirroring :meth:`tls_verify`.

        ``httpx`` accepts ``verify: bool | cafile``; ``websockets`` needs an
        ``SSLContext``, so this reproduces the same three cases in lock-step with
        the HTTP client:

          * ``ca_bundle`` set   → a verifying context trusting that bundle
            (takes precedence over ``verify_ssl``, exactly like ``tls_verify``);
          * ``verify_ssl`` False → verification disabled;
          * otherwise           → the default verifying context.

        Returns ``None`` for a plaintext ``ws://`` endpoint (no TLS).
        """
        if urlparse(self.ws_url()).scheme != "wss":
            return None
        if self.ca_bundle:
            return ssl.create_default_context(cafile=self.ca_bundle)
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @property
    def binds_localhost(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    @property
    def tls_enabled(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    def http_allowed_hosts(self) -> list[str]:
        """Host header allow-list for DNS-rebinding protection (HTTP transport)."""
        hosts: list[str] = []
        if self.allowed_hosts:
            hosts += [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]
        for host in (self.host, "127.0.0.1", "localhost"):
            if ":" in host:  # bare IPv6 literal — Host headers carry it bracketed
                host = f"[{host}]"
            hosts.append(f"{host}:{self.port}")
        return sorted(set(hosts))

    def http_allowed_origins(self) -> list[str]:
        """Origin allow-list for DNS-rebinding protection (HTTP transport)."""
        if self.allowed_origins:
            return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        return [f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"]


def load_settings(**overrides: object) -> Settings:
    """Build :class:`Settings`, converting validation errors into a clear,
    secret-free :class:`UnraidConfigError`."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        problems: list[str] = []
        for err in exc.errors():
            loc = err.get("loc", ())
            field = str(loc[0]) if loc else "config"
            if err.get("type") == "missing":
                problems.append(f"{field} is required but was not set")
            else:
                # Use the message only — never the offending input value.
                problems.append(f"{field}: {err.get('msg', 'invalid value')}")
        raise UnraidConfigError("Invalid configuration:\n  - " + "\n  - ".join(problems)) from None
