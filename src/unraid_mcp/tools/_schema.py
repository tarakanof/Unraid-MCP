"""Trim the tool metadata published in ``tools/list``.

pydantic and the SDK pad every schema with boilerplate: an auto-generated
``title`` on each property (``"Warning Count"``), each ``$defs`` entry and each
root (``"HealthSummary"``, ``"get_diskArguments"``), and nullable fields as
``anyOf: [{"type": X}, {"type": "null"}]``. Tool descriptions keep the
docstring's source indentation and hard wraps. Clients without tool search
load all of it up front, so :func:`trim_published_tools` strips it once, after
registration.

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
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

# Keywords whose value is one subschema / a list of subschemas / a name ->
# subschema map. Everything else (enum, default, const, required, ...) is data
# and is never rewritten, so a property literally named "title" survives.
# Not walked: dependentSchemas, propertyNames, unevaluated*, contentSchema
# (pydantic never emits them).
_SUBSCHEMA = ("items", "additionalProperties", "not", "contains", "if", "then", "else")
_SUBSCHEMA_LIST = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAP = ("properties", "$defs", "patternProperties")

_NULL = {"type": "null"}

# Keywords that only constrain instances of their own ``type`` (or are pure
# annotations), so a ``null`` instance passes them. A nullable branch is folded
# into ``"type": [X, "null"]`` only if it uses nothing else: ``$ref``, ``const``,
# ``not``, ``allOf``/``anyOf``/``oneOf`` and ``if``/``then``/``else`` also apply
# to ``null`` and folding them would change what validates.
_TYPE_SCOPED = frozenset(
    {
        "type",
        "enum",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "uniqueItems",
        "properties",
        "patternProperties",
        "required",
        "additionalProperties",
        "minProperties",
        "maxProperties",
        "title",
        "description",
        "default",
        "examples",
    }
)


def _auto_field_title(name: str) -> str:
    """pydantic's default field title: ``battery_pct`` -> ``Battery Pct``."""
    return name.title().replace("_", " ")


def _collapse_nullable(node: dict[str, Any]) -> dict[str, Any]:
    """``anyOf: [{"type": X, ...}, {"type": "null"}]`` -> ``{"type": [X, "null"], ...}``.

    Only when every non-null branch is a string ``type`` plus type-scoped
    keywords (:data:`_TYPE_SCOPED`); anything else keeps the ``anyOf``.
    """
    branches = node.get("anyOf")
    if not isinstance(branches, list) or len(branches) < 2 or branches[-1] != _NULL:
        return node
    typed = branches[:-1]
    if not all(
        isinstance(b, dict) and isinstance(b.get("type"), str) and set(b) <= _TYPE_SCOPED
        for b in typed
    ):
        return node
    rest = {k: v for k, v in node.items() if k != "anyOf"}
    if "type" in rest:  # a sibling ``type`` would clobber the folded list
        return node
    # ``int | float | None``: several bare types fold into one list.
    if all(set(b) == {"type"} for b in typed):
        types = [b["type"] for b in typed]
        if "number" in types and "integer" in types:  # integers are numbers
            types.remove("integer")
        return {"type": [*types, "null"], **rest}
    # One typed branch with its own keywords (``minimum``, ``items``...). Those
    # keywords only constrain their own type, so ``null`` still validates.
    if len(typed) != 1 or set(typed[0]) & set(rest):
        return node
    only = typed[0]
    if "enum" in only and None not in only["enum"]:
        # ``Literal[...] | None``: null must join the enum too.
        only = {**only, "enum": [*only["enum"], None]}
    return {**only, "type": [only["type"], "null"], **rest}


def _walk(
    node: Any, *, key: str | None, is_def: bool, root_titles: Collection[str], output: bool
) -> Any:
    """Trim one schema node.

    ``key`` is the node's property or ``$defs`` name (None at the root and
    inside ``items``/``anyOf``/...). ``root_titles`` are the titles pydantic or
    the SDK generate for the root (model name, ``<fn>Arguments``, ...).
    """
    if not isinstance(node, dict):
        return node

    def sub(value: Any, name: str | None = None, *, is_def: bool = False) -> Any:
        return _walk(value, key=name, is_def=is_def, root_titles=(), output=output)

    out: dict[str, Any] = {}
    for kw, value in node.items():
        if kw in _SUBSCHEMA:
            out[kw] = sub(value)
        elif kw in _SUBSCHEMA_LIST and isinstance(value, list):
            out[kw] = [sub(v) for v in value]
        elif kw in _SUBSCHEMA_MAP and isinstance(value, dict):
            out[kw] = {name: sub(v, name, is_def=kw == "$defs") for name, v in value.items()}
        else:
            out[kw] = value
    title = out.get("title")
    if key is None:
        auto = title in root_titles
    elif is_def:
        auto = title == key  # pydantic titles a definition with its class name
    else:
        auto = title == _auto_field_title(key)
    if auto:
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


_FENCE = re.compile(r"\s*(```|~~~)")
# Lines that always start a line of their own: list items and headings.
_STARTS_LINE = re.compile(r"[-*+]\s|\d+[.)]\s|#")


def trim_description(doc: str) -> str:
    """Dedent a docstring and unwrap its hard-wrapped prose.

    Consecutive prose lines join into one. Fenced code blocks stay verbatim;
    table rows (``|``), indented lines, blank lines, headings and list items
    keep their line breaks.
    """
    out: list[str] = []
    in_fence = joinable = False
    for line in inspect.cleandoc(doc).splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            joinable = False
        elif in_fence or not line.strip() or line[0].isspace() or line.startswith("|"):
            out.append(line)
            joinable = False
        elif joinable and not _STARTS_LINE.match(line):
            out[-1] = f"{out[-1]} {line.strip()}"
        else:
            out.append(line)
            joinable = not line.startswith("#")
    return "\n".join(out)


def trim_input_schema(
    schema: dict[str, Any], *, root_titles: Collection[str] = ()
) -> dict[str, Any]:
    """Return a copy of a tool input schema without auto-generated titles.

    Nullable ``anyOf`` wrappers stay: some hosts' function-calling validators
    reject ``"type": [...]`` arrays in parameter schemas.
    """
    return _walk(schema, key=None, is_def=False, root_titles=root_titles, output=False)


def trim_output_schema(
    schema: dict[str, Any], *, root_titles: Collection[str] = ()
) -> dict[str, Any]:
    """Return an equivalent, smaller copy of a tool output schema."""
    return _walk(schema, key=None, is_def=False, root_titles=root_titles, output=True)


def trim_published_tools(mcp: MCPServer) -> None:
    """Trim every registered tool's published description and schemas in place.

    Call once, after all tools are registered. The SDK has no public accessor
    for registered ``Tool`` objects, so this reaches into ``_tool_manager``;
    ``tests/test_schema_trim.py`` fails if that seam moves.
    ``FuncMetadata.output_schema`` is documented as read live, so reassigning
    it takes effect on the next ``tools/list``.
    """
    for tool in mcp._tool_manager.list_tools():
        meta = tool.fn_metadata
        fn_name = getattr(tool.fn, "__name__", tool.name)
        tool.description = trim_description(tool.description)
        tool.parameters = trim_input_schema(tool.parameters, root_titles={meta.arg_model.__name__})
        if meta.output_schema is not None:
            # The output root is titled with the declared model's name, or the
            # SDK's ``<fn>Output`` / ``<fn>DictOutput`` wrapper name.
            roots = {f"{fn_name}Output", f"{fn_name}DictOutput"}
            model_name = getattr(meta.output_model, "__name__", None)
            if isinstance(model_name, str):
                roots.add(model_name)
            meta.output_schema = trim_output_schema(meta.output_schema, root_titles=roots)
