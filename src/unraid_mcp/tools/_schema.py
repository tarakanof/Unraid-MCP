"""Trim the tool metadata published in ``tools/list``.

pydantic and the SDK pad every schema with boilerplate: an auto-generated
``title`` on each property (``"Warning Count"``), each ``$defs`` entry and each
root (``"HealthSummary"``, ``"get_diskArguments"``), and nullable fields as
``anyOf: [{"type": X}, {"type": "null"}]``. Tool descriptions keep the
docstring's source indentation and hard wraps. Clients without tool search load all of it up
front, so :func:`trim_published_tools` strips it once, after registration.

Only the *published* metadata changes. The SDK validates arguments against
each tool's pydantic argument model and results against its output model, not
against these dicts, so server-side validation is unaffected; clients that
validate ``structuredContent`` against the published ``outputSchema`` see an
equivalent schema.

This is the single post-processing hook for published tool schemas: put
further input/output transforms in :func:`trim_input_schema` /
:func:`trim_output_schema`.
"""

from __future__ import annotations

import inspect
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

# Keywords whose value is one subschema / a list of subschemas / a name ->
# subschema map. Everything else (enum, default, const, required, ...) is data
# and is never rewritten, so a property literally named "title" survives.
_SUBSCHEMA = ("items", "additionalProperties", "not", "contains", "if", "then", "else")
_SUBSCHEMA_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAP = ("properties", "$defs", "patternProperties")

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_NULL = {"type": "null"}
_KEEP_BREAK = re.compile(r"\s|[-*]\s|\d+[.)]\s")


def _auto_field_title(name: str) -> str:
    """pydantic's default field title: ``battery_pct`` -> ``Battery Pct``."""
    return name.title().replace("_", " ")


def _is_auto_title(title: Any, *, key: str | None, is_model: bool) -> bool:
    if not isinstance(title, str):
        return False
    if is_model:
        # Root and $defs titles are the Python class name or the SDK's
        # ``<tool>Arguments`` / ``<tool>Output``: always a bare identifier.
        return title == key or bool(_IDENTIFIER.match(title))
    return key is not None and title == _auto_field_title(key)


def _collapse_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """``anyOf: [{"type": X, ...}, {"type": "null"}]`` -> ``{"type": [X, "null"], ...}``.

    Only for typed branches: a ``$ref``/``const`` branch keeps the ``anyOf``
    because folding ``null`` into it would change what validates.
    """
    branches = node.get("anyOf")
    if not isinstance(branches, list) or len(branches) < 2 or branches[-1] != _NULL:
        return node
    typed = branches[:-1]
    if not all(isinstance(b, dict) and isinstance(b.get("type"), str) for b in typed):
        return node
    rest = {k: v for k, v in node.items() if k != "anyOf"}
    # ``int | float | None``: several bare types fold into one list.
    if all(set(b) == {"type"} for b in typed):
        types = [b["type"] for b in typed]
        if "number" in types and "integer" in types:  # integers are numbers
            types.remove("integer")
        return {"type": [*types, "null"], **rest}
    # One typed branch with its own keywords (``minimum``, ``items``...). Those
    # keywords only constrain their own type, so ``null`` still validates.
    (only,) = typed if len(typed) == 1 else (None,)
    if only is None or {"$ref", "const"} & set(only) or set(only) & set(rest):
        return node
    if "enum" in only:
        # ``Literal[...] | None``: null must join the enum too.
        only = {**only, "enum": [*only["enum"], None]}
    return {**only, "type": [only["type"], "null"], **rest}


def _walk(node: Any, *, key: str | None, is_model: bool, output: bool) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for kw, value in node.items():
        if kw in _SUBSCHEMA:
            out[kw] = _walk(value, key=None, is_model=False, output=output)
        elif kw in _SUBSCHEMA_LIST and isinstance(value, list):
            out[kw] = [_walk(v, key=None, is_model=False, output=output) for v in value]
        elif kw in _SUBSCHEMA_MAP and isinstance(value, dict):
            out[kw] = {
                name: _walk(sub, key=name, is_model=kw == "$defs", output=output)
                for name, sub in value.items()
            }
        else:
            out[kw] = value
    if _is_auto_title(out.get("title"), key=key, is_model=is_model):
        del out["title"]
    if output:
        # ``additionalProperties: true`` is the JSON Schema default.
        if out.get("additionalProperties") is True:
            del out["additionalProperties"]
        # ``items: {}`` (``list[Any]``) is the default too.
        if out.get("items") == {}:
            del out["items"]
        out = _collapse_nullable(out)
    return out


def trim_description(doc: str) -> str:
    """Dedent a docstring and unwrap its hard-wrapped lines.

    Paragraphs (blank-line separated) become single lines; a line that starts a
    list item (``-``, ``*``, ``1.``) or is indented keeps its line break.
    """
    paragraphs = []
    for para in inspect.cleandoc(doc).split("\n\n"):
        lines: list[str] = []
        for line in para.splitlines():
            if lines and line.strip() and not _KEEP_BREAK.match(line):
                lines[-1] = f"{lines[-1]} {line.strip()}"
            else:
                lines.append(line)
        paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def trim_input_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a tool input schema without auto-generated titles.

    Nullable ``anyOf`` wrappers stay: some hosts' function-calling validators
    reject ``"type": [...]`` arrays in parameter schemas.
    """
    return _walk(schema, key=None, is_model=True, output=False)


def trim_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return an equivalent, smaller copy of a tool output schema."""
    return _walk(schema, key=None, is_model=True, output=True)


def trim_published_tools(mcp: MCPServer) -> None:
    """Trim every registered tool's published description and schemas in place.

    Call once, after all tools are registered. The SDK has no public accessor
    for registered ``Tool`` objects, so this reaches into ``_tool_manager``;
    ``tests/test_schema_trim.py`` fails if that seam moves.
    ``FuncMetadata.output_schema`` is documented as read live, so reassigning
    it takes effect on the next ``tools/list``.
    """
    for tool in mcp._tool_manager.list_tools():
        tool.description = trim_description(tool.description)
        tool.parameters = trim_input_schema(tool.parameters)
        if tool.fn_metadata.output_schema is not None:
            tool.fn_metadata.output_schema = trim_output_schema(tool.fn_metadata.output_schema)
