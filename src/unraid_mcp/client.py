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
    UnraidGraphQLError,
    UnraidServerError,
)
from .logging import get_logger

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
    ) -> None:
        self._url = url
        self._key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._http = http_client
        self._host = host_label or urlparse(url).netloc or url

    async def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a GraphQL operation and return its ``data`` object.

        Raises an :class:`~unraid_mcp.errors.UnraidError` subclass on failure.
        The API key is never included in any error message.
        """
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
            payload = response.json()
        except ValueError as exc:
            raise UnraidServerError(
                f"Unraid returned a non-JSON response (HTTP {response.status_code}) "
                f"from {self._host}."
            ) from exc

        errors = payload.get("errors")
        data = payload.get("data")
        if errors:
            messages = "; ".join(str(e.get("message", "unknown error")) for e in errors)
            if data is None:
                raise UnraidGraphQLError(f"GraphQL error: {messages}", errors=errors)
            # Partial success — Unraid returned some data plus non-fatal errors
            # (e.g. an optional field unavailable on this build). Surface a
            # warning and return what we got.
            log.warning("GraphQL returned partial errors: %s", messages)

        return data or {}
