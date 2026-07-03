#!/usr/bin/env python3
"""Validate every GraphQL operation in ``queries.py`` against the upstream schema.

The Unraid API (``unraid/api``) evolves quickly. Our operations are string
literals that ``tests/test_queries_valid.py`` only checks for *syntax*. This
script downloads the upstream SDL and runs full ``graphql.validate`` against
each operation so renamed/removed fields surface in CI before they become
runtime tool failures.

Run locally (needs network):

    uv run python scripts/check_schema_drift.py

Exits non-zero if any operation fails validation. Intended for the weekly
``schema-drift`` workflow, not the per-PR CI (network dependency).
"""

from __future__ import annotations

import sys
import urllib.request

from graphql import GraphQLSchema, build_schema, parse, validate

from unraid_mcp import queries

SCHEMA_URL = "https://raw.githubusercontent.com/unraid/api/main/api/generated-schema.graphql"


def discover_operations(module: object = queries) -> list[tuple[str, str]]:
    """Return ``(name, operation)`` for every UPPER_CASE str constant in ``module``.

    Discovery is generic on purpose: any operation added to ``queries.py`` is
    picked up automatically, so there is no manual list to forget to update.
    Names starting with ``_`` (shared fragments/selections) are excluded.
    """
    return sorted(
        (name, value)
        for name, value in vars(module).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    )


def download_schema(url: str = SCHEMA_URL) -> GraphQLSchema:
    """Download the upstream SDL and build an executable schema.

    The upstream SDL declares all its custom scalars (``PrefixedID``, ``BigInt``,
    ``DateTime``, ``JSON``, ``Port``, ``URL``), so ``build_schema`` needs no
    scalar stubs.
    """
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 (trusted URL)
        sdl = response.read().decode("utf-8")
    return build_schema(sdl)


def check(schema: GraphQLSchema, operations: list[tuple[str, str]]) -> int:
    """Validate each operation; print PASS/FAIL and return the failure count."""
    failures = 0
    for name, operation in operations:
        errors = validate(schema, parse(operation))
        if errors:
            failures += 1
            print(f"FAIL {name}")
            for error in errors:
                print(f"     {error.message}")
        else:
            print(f"PASS {name}")
    return failures


def main() -> int:
    operations = discover_operations()
    print(f"Validating {len(operations)} operations against {SCHEMA_URL}\n")
    schema = download_schema()
    failures = check(schema, operations)
    print(f"\n{len(operations) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
