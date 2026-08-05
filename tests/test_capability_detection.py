"""Tests for the API capability-detection foundation (issue #15).

Covers the shared helpers in ``tools/_base`` that issues #16–#19 build on:
``unsupported_field_error``, ``feature_unsupported``, and the canonical
degrading-fetch pattern they compose into.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from unraid_mcp.errors import UnraidConnectionError, UnraidGraphQLError
from unraid_mcp.tools._base import (
    feature_unsupported,
    unsupported_field_error,
)


def test_unsupported_field_error_true_from_message():
    exc = UnraidGraphQLError(
        'GraphQL error: Cannot query field "metrics" on type "Query".',
        errors=[{"message": 'Cannot query field "metrics" on type "Query".'}],
    )
    assert unsupported_field_error(exc) is True


def test_unsupported_field_error_true_when_only_in_errors_list():
    # str(exc) is generic but the structured errors carry the phrase.
    exc = UnraidGraphQLError(
        "GraphQL error: validation failed",
        errors=[{"message": 'Cannot query field "logs" on type "Docker".'}],
    )
    assert unsupported_field_error(exc) is True


def test_unsupported_field_error_false_for_unrelated_graphql_error():
    exc = UnraidGraphQLError(
        "GraphQL error: Authentication required",
        errors=[{"message": "Authentication required"}],
    )
    assert unsupported_field_error(exc) is False


def test_unsupported_field_error_false_for_non_graphql_error():
    assert unsupported_field_error(UnraidConnectionError("network down")) is False


def test_feature_unsupported_full_message():
    err = feature_unsupported("live system metrics", requires="7.2+", api_version="7.1.0")
    assert isinstance(err, ToolError)
    msg = str(err)
    assert "does not support" in msg
    assert "live system metrics" in msg
    assert "Server reports API 7.1.0" in msg
    assert "requires 7.2+" in msg
    assert msg.endswith("Upgrade Unraid or the Connect plugin.")


def test_feature_unsupported_omits_clauses_when_none():
    msg = str(feature_unsupported("live system metrics"))
    assert "does not support live system metrics" in msg
    assert "Server reports" not in msg
    assert "requires" not in msg
    assert "Upgrade Unraid or the Connect plugin." in msg


async def test_degrading_fetch_pattern_maps_unknown_field_to_feature_unsupported():
    """Sanity-check the pattern #16–#19 copy: unknown-field errors become a
    friendly ToolError; unrelated GraphQL errors propagate untouched."""

    async def fetch_metrics(fail_with: UnraidGraphQLError, *, api_version=None):
        try:
            raise fail_with
        except UnraidGraphQLError as exc:
            if unsupported_field_error(exc):
                raise feature_unsupported(
                    "live system metrics", requires="7.2+", api_version=api_version
                ) from None
            raise

    unknown = UnraidGraphQLError(
        'GraphQL error: Cannot query field "metrics" on type "Query".',
        errors=[{"message": 'Cannot query field "metrics" on type "Query".'}],
    )
    with pytest.raises(ToolError) as ei:
        await fetch_metrics(unknown, api_version="7.1.0")
    assert "does not support live system metrics" in str(ei.value)

    other = UnraidGraphQLError("GraphQL error: Authentication required")
    with pytest.raises(UnraidGraphQLError):
        await fetch_metrics(other, api_version="7.1.0")
