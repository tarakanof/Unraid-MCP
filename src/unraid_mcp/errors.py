"""Exception hierarchy for the Unraid MCP server.

These are raised by the GraphQL client and configuration layers and are
translated into user-facing ``ToolError`` messages at the tool boundary.
None of these carry the API key or other secrets in their messages.
"""

from __future__ import annotations


class UnraidError(Exception):
    """Base class for all errors raised by this package."""


class UnraidConfigError(UnraidError):
    """Configuration/environment is missing or invalid."""


class UnraidConnectionError(UnraidError):
    """The Unraid server could not be reached (network error or timeout)."""


class UnraidAuthError(UnraidError):
    """Authentication/authorization failed (bad API key or insufficient roles)."""


class UnraidServerError(UnraidError):
    """The Unraid server returned a non-success HTTP status (5xx/4xx)."""


class UnraidGraphQLError(UnraidError):
    """The GraphQL response contained an ``errors`` array.

    The individual error messages from the server are kept in ``errors`` so
    callers can surface them; they never contain the API key.
    """

    def __init__(self, message: str, errors: list[dict] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict] = errors or []
