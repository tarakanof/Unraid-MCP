"""Parse every GraphQL operation string so syntax errors are caught at test time.

We can't validate against a live schema here, but `graphql-core`'s parser catches
malformed operations — the most likely hand-authoring mistake.
"""

from __future__ import annotations

import pytest
from graphql import parse

from unraid_mcp import queries

OPERATIONS = [
    (name, value)
    for name, value in vars(queries).items()
    if name.isupper() and not name.startswith("_") and isinstance(value, str)
]


def test_operation_set_is_substantial():
    assert len(OPERATIONS) >= 25


@pytest.mark.parametrize("name,query", OPERATIONS, ids=[n for n, _ in OPERATIONS])
def test_operation_parses(name, query):
    parse(query)  # raises GraphQLSyntaxError on malformed operations
