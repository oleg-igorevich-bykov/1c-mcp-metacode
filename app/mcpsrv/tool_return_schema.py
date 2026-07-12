"""Compile flat TOOL_RETURN_DOCS field lists into nested JSON-like schemas.

The single source of truth stays `TOOL_RETURN_DOCS` (human-oriented docs where
nesting is encoded in the field path: `items[].fragments[].fragment_id`,
`page.limit`, `structure.*[].name`, `<section_name>`). This module only compiles
those flat `fields` into a nested schema tree for programmatic consumption by the
`get_tool_return_schema` MCP tool. It imports `TOOL_RETURN_DOCS` and never mutates
it — dependency direction is one-way (`tool_return_schema` -> `tool_return_docs`).
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Tuple

from .tool_return_docs import TOOL_RETURN_DOCS

_DEFAULT_WHEN = "Всегда"
_SCALARS = {"string", "integer", "number", "boolean", "null"}


def compile_type(raw: str) -> Dict[str, Any]:
    """Compile a field `type` string into a schema node via a recursive grammar.

    Covers the full current vocabulary of `TOOL_RETURN_DOCS` — scalars, `object`,
    `any`, bare `array`, `array<INNER>` (recursive element), and pipe-unions of any
    arity — instead of enumerating specific strings, so new/rare types are still
    expanded structurally rather than left as a raw type string.
    """
    s = str(raw or "").strip()
    if not s:
        return {"type": "any"}
    # 1. Union: top-level '|' (no current type nests '|' inside '<...>').
    if "|" in s:
        return {"oneOf": [compile_type(part) for part in s.split("|")]}
    # 2. Array with element type: array<INNER> (recurse on INNER).
    if s.startswith("array<") and s.endswith(">"):
        return {"type": "array", "items": compile_type(s[len("array<"):-1])}
    # 3. Bare array (element type unspecified).
    if s == "array":
        return {"type": "array"}
    # 4. Object.
    if s == "object":
        return {"type": "object", "properties": {}}
    # 5. Any.
    if s == "any":
        return {"type": "any"}
    # 6. Scalar.
    if s in _SCALARS:
        return {"type": s}
    # 7. Fallback (raw). A composite string reaching here is a contract defect —
    #    the corpus test asserts no compiled `type` contains '<', '>' or '|'.
    return {"type": s}


def _apply_meta(node: Dict[str, Any], description: str, when: str) -> None:
    if description:
        node.setdefault("description", description)
    if when and when != _DEFAULT_WHEN:
        node.setdefault("when", when)


def _merge_type(node: Dict[str, Any], compiled: Dict[str, Any]) -> None:
    """Merge a compiled type node into `node`, preserving already-built children."""
    if "oneOf" in compiled:
        node["oneOf"] = compiled["oneOf"]
        return
    ctype = compiled.get("type")
    node["type"] = ctype
    if ctype == "array":
        citems = compiled.get("items")
        if citems is not None:
            _merge_type(node.setdefault("items", {}), citems)
    elif ctype == "object":
        node.setdefault("properties", {})


def _split_segments(name: str) -> List[Tuple[str, bool, bool]]:
    """Split a field path into (key, is_array, dynamic) segments.

    `dynamic` marks a wildcard `*` or `<placeholder>` segment that compiles to
    `additionalProperties` rather than a named property.
    """
    segs: List[Tuple[str, bool, bool]] = []
    for tok in name.split("."):
        is_array = tok.endswith("[]")
        base = tok[:-2] if is_array else tok
        dynamic = base == "*" or (base.startswith("<") and base.endswith(">"))
        segs.append((base, is_array, dynamic))
    return segs


def _child_node(holder: Dict[str, Any], key: str, dynamic: bool) -> Dict[str, Any]:
    """Get/create the child schema node for a segment within an object holder."""
    if dynamic:
        child = holder.get("additionalProperties")
        if child is None:
            child = {}
            holder["additionalProperties"] = child
        return child
    props = holder.setdefault("properties", {})
    child = props.get(key)
    if child is None:
        child = {}
        props[key] = child
    return child


def _as_container(node: Dict[str, Any], is_array: bool) -> Dict[str, Any]:
    """Make `node` an object (or array-of-object) container and return the object
    holder to descend into. Existing children are preserved."""
    if is_array:
        node["type"] = "array"
        items = node.setdefault("items", {})
        items["type"] = "object"
        items.setdefault("properties", {})
        return items
    node["type"] = "object"
    node.setdefault("properties", {})
    return node


def _insert_segments(
    holder: Dict[str, Any],
    segs: List[Tuple[str, bool, bool]],
    type_str: str,
    description: str,
    when: str,
) -> None:
    for i, (key, is_array, dynamic) in enumerate(segs):
        if key == "":
            continue
        child = _child_node(holder, key, dynamic)
        if i == len(segs) - 1:
            _merge_type(child, compile_type(type_str))
            _apply_meta(child, description, when)
        else:
            holder = _as_container(child, is_array)


def _element_holders(holder: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Item-holders of sibling sections that share a `<element>` element shape.

    Each array section contributes its `items`; each empty-object section (an
    object-of-arrays such as tabular_attributes/form_attributes) contributes its
    `additionalProperties.items`. Object sections that already have named
    properties (e.g. an `overview` card) are intentionally left untouched.
    """
    holders: List[Dict[str, Any]] = []
    for node in (holder.get("properties") or {}).values():
        t = node.get("type")
        if t == "array":
            items = node.setdefault("items", {})
            items.setdefault("type", "object")
            items.setdefault("properties", {})
            holders.append(items)
        elif t == "object" and not node.get("properties"):
            addl = node.get("additionalProperties")
            if addl is None:
                addl = {"type": "array", "items": {"type": "object", "properties": {}}}
                node["additionalProperties"] = addl
            items = addl.setdefault("items", {})
            items.setdefault("type", "object")
            items.setdefault("properties", {})
            holders.append(items)
    return holders


def _insert_field(
    root_holder: Dict[str, Any], name: str, type_str: str, description: str, when: str
) -> None:
    segs = _split_segments(name)
    # A leading empty-key array segment ('[]') is redundant: the enclosing array
    # root already represents the element.
    if segs and segs[0][0] == "":
        segs = segs[1:]
    if not segs:
        return
    key0, _is_array0, dynamic0 = segs[0]
    # A `<placeholder>` as the FIRST segment (get_metadata_element_type
    # `<element>.name`) describes a field shared by every sibling element section,
    # not a fresh dynamic dict. Distribute the remainder into each sibling element
    # holder so the concrete sections get the element schema.
    if dynamic0 and key0.startswith("<") and len(segs) > 1:
        holders = _element_holders(root_holder)
        if holders:
            for h in holders:
                _insert_segments(h, segs[1:], type_str, description, when)
            return
    _insert_segments(root_holder, segs, type_str, description, when)


def _safe_insert(holder: Dict[str, Any], f: Dict[str, str], unparsed: List[Dict[str, Any]]) -> None:
    """Insert one field; on any parse failure, record it as unparsed instead of
    raising (graceful runtime fallback for unexpected future patterns)."""
    try:
        _insert_field(
            holder,
            f["name"],
            f.get("type", ""),
            f.get("description", ""),
            f.get("when", _DEFAULT_WHEN),
        )
    except Exception:
        unparsed.append({k: f.get(k) for k in ("name", "type", "description", "when")})


def _compile_oneof_case(
    shape_s: str, fields: List[Dict[str, str]], unparsed: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Compile a shape like `array<object> или object.forms` / `... object с
    массивом по имени section` into a oneOf of the array variant and the object
    variant (named property or dynamic additionalProperties)."""
    item_obj: Dict[str, Any] = {"type": "object", "properties": {}}
    dyn_meta = None
    for f in fields:
        base0 = f["name"].split(".")[0]
        if base0.startswith("<") and base0.endswith(">"):
            dyn_meta = (f.get("description", ""), f.get("when", _DEFAULT_WHEN))
            continue
        _safe_insert(item_obj, f, unparsed)

    array_variant = {"type": "array", "items": item_obj}
    inner_array: Dict[str, Any] = {"type": "array", "items": copy.deepcopy(item_obj)}
    if dyn_meta:
        _apply_meta(inner_array, dyn_meta[0], dyn_meta[1])

    obj_variant: Dict[str, Any] = {"type": "object"}
    m = re.search(r"object\.([A-Za-z_]\w*)", shape_s)
    if m:
        obj_variant["properties"] = {m.group(1): inner_array}
    else:
        obj_variant["additionalProperties"] = inner_array

    return {"oneOf": [array_variant, obj_variant]}


def _compile_return_case(
    shape: str, fields: List[Dict[str, str]]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    shape_s = str(shape or "").strip()
    unparsed: List[Dict[str, Any]] = []

    if "или object" in shape_s:
        schema = _compile_oneof_case(shape_s, fields, unparsed)
    elif shape_s.startswith("array"):
        item_obj: Dict[str, Any] = {"type": "object", "properties": {}}
        schema = {"type": "array", "items": item_obj}
        for f in fields:
            _safe_insert(item_obj, f, unparsed)
    else:
        schema = {"type": "object", "properties": {}}
        for f in fields:
            _safe_insert(schema, f, unparsed)

    return schema, unparsed


def _resolve_tool_name(name: str) -> str:
    """Map a possibly namespace-prefixed tool name to a canonical TOOL_RETURN_DOCS key.

    Bare names win first (exact match). Then handle common MCP client prefixes such
    as mcp__server__tool, mcp_server__tool, server.tool and server/tool by taking
    the longest known key that is a suffix on a `__` / `.` / `/` boundary. A single
    `_` is intentionally NOT treated as a boundary — it is an ordinary part of tool
    names (find_metadata_objects) and would match too broadly. Unknown names are
    returned unchanged so the caller raises KeyError with the original input.
    """
    if name in TOOL_RETURN_DOCS:
        return name
    if "__" in name:
        tail = name.rsplit("__", 1)[-1]
        if tail in TOOL_RETURN_DOCS:
            return tail
    best = None
    for key in TOOL_RETURN_DOCS:
        if any(name.endswith(sep + key) for sep in ("__", ".", "/")):
            if best is None or len(key) > len(best):
                best = key
    return best or name


def build_tool_return_schema(tool_name: str) -> Dict[str, Any]:
    """Return the nested return schema for a documented MCP tool.

    Compiled from `TOOL_RETURN_DOCS[tool_name]`; a namespace-prefixed name from an
    MCP client is resolved to its canonical key first. Raises KeyError for an
    unknown tool. Serves the documented shape of every tool in `TOOL_RETURN_DOCS`
    regardless of whether that tool is currently registered/visible — this is
    documentation of the response shape, not an availability guarantee.
    """
    tool_name = _resolve_tool_name(tool_name)
    doc = TOOL_RETURN_DOCS[tool_name]  # KeyError propagates for unknown tool_name.
    out_returns: List[Dict[str, Any]] = []
    for entry in doc.get("returns", []):
        shape = entry.get("shape", "")
        schema, unparsed = _compile_return_case(shape, entry.get("fields", []) or [])
        ret: Dict[str, Any] = {
            "case": entry.get("case"),
            "shape": shape,
            "schema": schema,
        }
        if unparsed:
            # Explicit degradation: the compiled tree is partial. Kept off the
            # happy path so successful payloads stay clean.
            schema["unparsed_fields"] = unparsed
            ret["schema_complete"] = False
        out_returns.append(ret)
    return {"tool_name": tool_name, "returns": out_returns}


__all__ = ["build_tool_return_schema", "compile_type"]
