"""Offline unit tests for the schema-drift checker's discovery function.

The network download and live validation are exercised by the weekly
``schema-drift`` workflow, not here — these tests must stay offline so
``uv run pytest -q`` is fast and hermetic.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import check_schema_drift  # noqa: E402

from unraid_mcp import queries  # noqa: E402


def test_discover_finds_all_current_operations():
    discovered = dict(check_schema_drift.discover_operations())

    expected = {
        name: value
        for name, value in vars(queries).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, str)
    }
    assert discovered == expected
    assert len(discovered) >= 25  # matches test_queries_valid's substantiality bar


def test_discover_excludes_private_and_non_string():
    class FakeModule:
        SOME_OP = "query { me { id } }"
        ANOTHER_OP = "mutation { noop }"
        _ARRAY_FIELDS = "id name"  # private helper, excluded
        NOT_A_STR = 42  # non-string, excluded
        lowercase = "query { skip }"  # not UPPER_CASE, excluded

    ops = check_schema_drift.discover_operations(FakeModule)
    assert ops == [
        ("ANOTHER_OP", "mutation { noop }"),
        ("SOME_OP", "query { me { id } }"),
    ]


def test_discover_returns_sorted():
    names = [name for name, _ in check_schema_drift.discover_operations()]
    assert names == sorted(names)
