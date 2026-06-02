"""Configuration for the Unraid MCP server, loaded from environment variables.

All settings come from ``UNRAID_*`` environment variables (optionally via a
``.env`` file). The API key is held as a ``SecretStr`` so it never leaks into
logs, ``repr()``, or error messages.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import UnraidConfigError

Transport = Literal["stdio", "streamable-http"]


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

    # ── Safety switches ────────────────────────────────────────────────
    allow_mutations: bool = Field(default=False, validation_alias="UNRAID_MCP_ALLOW_MUTATIONS")
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
                "UNRAID_API_URL must be an http(s) URL, e.g. https://tower.local/graphql"
            )
        if not parsed.netloc:
            raise ValueError("UNRAID_API_URL must include a host, e.g. https://tower.local/graphql")
        # Append the GraphQL path if the user gave only a base URL.
        if parsed.path in ("", "/"):
            value = value.rstrip("/") + "/graphql"
        return value

    @property
    def host_for_messages(self) -> str:
        """The bare host:port, safe to show in user-facing error messages."""
        return urlparse(self.api_url).netloc or self.api_url

    def tls_verify(self) -> bool | str:
        """Value for ``httpx`` ``verify``: a CA-bundle path if set, else the bool."""
        return self.ca_bundle if self.ca_bundle else self.verify_ssl


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
