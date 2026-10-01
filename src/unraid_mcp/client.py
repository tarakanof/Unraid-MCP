"""Async GraphQL client for the Unraid API.

This is intentionally generic: it knows how to authenticate, POST a GraphQL
operation, and map transport/HTTP/GraphQL failures onto the package's
exception hierarchy. It has no knowledge of specific Unraid operations — tool
modules own the queries and response shaping.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from .errors import (
    UnraidAuthError,
    UnraidConnectionError,
    UnraidError,
    UnraidGraphQLError,
    UnraidServerError,
)
from .logging import get_logger, redact

log = get_logger(__name__)


class UnraidClient:
    """Executes GraphQL operations against an Unraid server.

    The shared ``httpx.AsyncClient`` (with TLS/timeout settings) is supplied by
    the caller so it can be managed by the server lifespan and reused across
    requests for connection pooling.
    """

    def __init__(
        self,
        url: str,
        api_key: SecretStr | str,
        http_client: httpx.AsyncClient,
        *,
        host_label: str | None = None,
        bearer_token: SecretStr | str | None = None,
    ) -> None:
        self._url = url
        self._key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        token = (
            bearer_token.get_secret_value() if isinstance(bearer_token, SecretStr) else bearer_token
        )
        self._secrets = (self._key, token)
        self._http = http_client
        self._host = host_label or urlparse(url).netloc or url

    @property
    def secrets(self) -> tuple[str | None, ...]:
        """Configured secrets (API key, bearer token) for scrubbing output."""
        return self._secrets

    async def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL operation and return its ``data`` object.

        Raises an :class:`~unraid_mcp.errors.UnraidError` subclass on failure.
        Configured secrets are scrubbed from data and errors.
        """
        data, _ = await self.execute_with_errors(query, variables)
        return data

    async def execute_with_errors(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Return data and redacted partial errors; raise on total query failure."""
        try:
            return await self._execute(query, variables)
        except UnraidError as exc:
            message = redact(str(exc), self._secrets)
            if isinstance(exc, UnraidGraphQLError):
                raise UnraidGraphQLError(
                    message, errors=redact(exc.errors, self._secrets)
                ) from None
            raise type(exc)(message) from None

    async def _execute(
        self, query: str, variables: dict[str, Any] | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            response = await self._http.post(
                self._url,
                json={"query": query, "variables": variables or {}},
                headers={"x-api-key": self._key, "content-type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise UnraidConnectionError(
                f"Timed out talking to Unraid at {self._host}. Is the server up and reachable?"
            ) from exc
        except httpx.TransportError as exc:
            raise UnraidConnectionError(
                f"Could not connect to Unraid at {self._host}. "
                "Check UNRAID_API_URL and the network."
            ) from exc

        if 300 <= response.status_code < 400:
            location = response.headers.get("location")
            hint = "If this is an http→https redirect, change UNRAID_API_URL to https://..."
            if location:
                parsed = urlparse(location)
                if parsed.scheme and parsed.netloc:
                    raise UnraidConnectionError(
                        f"Unraid responded with a redirect (HTTP {response.status_code}) to "
                        f"{parsed.scheme}://{parsed.netloc}. This client does not follow "
                        f"redirects. {hint}"
                    )
            raise UnraidConnectionError(
                f"Unraid responded with a redirect (HTTP {response.status_code}) from "
                f"{self._host}. This client does not follow redirects. {hint}"
            )
        if response.status_code in (401, 403):
            raise UnraidAuthError(
                "Authentication failed (HTTP "
                f"{response.status_code}). Check UNRAID_API_KEY and that the key's "
                "roles/permissions allow this operation."
            )
        if response.status_code >= 500:
            raise UnraidServerError(
                f"Unraid returned a server error (HTTP {response.status_code}) from {self._host}."
            )
        if response.status_code >= 400:
            raise UnraidServerError(f"Unexpected HTTP {response.status_code} from {self._host}.")

        try:
            payload = redact(response.json(), self._secrets)
        except ValueError as exc:
            raise UnraidServerError(
                f"Unraid returned a non-JSON response (HTTP {response.status_code}) "
                f"from {self._host}."
            ) from exc

        envelope_hint = "Check the Unraid API response and server logs."
        if not isinstance(payload, dict):
            raise UnraidServerError(
                f"Invalid GraphQL envelope: expected a JSON object. {envelope_hint}"
            )
        data = payload.get("data")
        if data is not None and not isinstance(data, dict):
            raise UnraidServerError(
                f"Invalid GraphQL envelope: data must be an object or null. {envelope_hint}"
            )
        raw_errors = payload.get("errors")
        if raw_errors is None:
            raw_errors = []
        if not isinstance(raw_errors, list):
            raise UnraidServerError(
                f"Invalid GraphQL envelope: errors must be a list. {envelope_hint}"
            )
        errors: list[dict[str, Any]] = []
        if raw_errors:
            # The whole response was scrubbed above, including structured error
            # details; coercion runs on that scrubbed payload.
            errors = [e if isinstance(e, dict) else {"message": str(e)} for e in raw_errors]
            messages = "; ".join(str(e.get("message", "unknown error")) for e in errors)
            if data is None or all(value is None for value in data.values()):
                raise UnraidGraphQLError(f"GraphQL error: {messages}", errors=errors)
            # Partial success — Unraid returned some data plus non-fatal errors
            # (e.g. an optional field unavailable on this build). Surface a
            # warning and return what we got.
            log.warning("GraphQL returned partial errors: %s", messages)

        return data or {}, errors
