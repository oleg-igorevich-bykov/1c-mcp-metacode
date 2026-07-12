"""Build an `object_profile` dict from raw evidence.

The profile is what the LLM actually sees (after TOON encoding). Two stages:

1. `collect_evidence(driver, identity, ...)` — runs the Cypher queries from
   `graphdb.object_summary_queries` and returns a raw evidence dict.
2. `build_profile(evidence, size_policy)` — applies the compaction rules:
     * drop `Удалить*` items;
     * drop synonyms that just repeat the name in camelCase;
     * strip `АПК:*` comments;
     * compress primitive types (`Число(...)` → `Ч`) and reference types
       through `TYPE_ALIASES`;
     * group relationships per relation+category;
     * cap forms/commands/relationships/bsl routines by the active
       `SIZE_POLICY` (small / medium / large);
     * collapse numbered attribute series (`ВидВремени1..ВидВремени31` →
       `ВидВремени[1..31]`);
     * build `bsl_profile` via `bsl_selection.build_bsl_profile` with
       priority-based routine selection, decorator awareness, handler/flow
       grouping;
     * inline extension changes (own/modified elements + extension BSL)
       into `extension_context` of the base profile.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .bsl_selection import build_bsl_profile
from .constants import (
    PROFILE_SCHEMA_VERSION,
    TYPE_ALIASES,
    get_size_policy,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def clean_text(value: Any, *, multiline: bool = False, limit: Optional[int] = None) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if multiline:
        lines = [re.sub(r"[ \t]+$", "", line) for line in text.split("\n")]
        text = "\n".join(lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = re.sub(r"\s+", " ", text).strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


def is_deprecated_1c_metadata_name(value: Any) -> bool:
    return clean_text(value).startswith("Удалить")


def filter_deprecated_1c_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [it for it in items or [] if not is_deprecated_1c_metadata_name(it.get("name"))]


def is_redundant_synonym(name: Any, synonym: Any) -> bool:
    name_key = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", clean_text(name)).replace("ё", "е").lower()
    syn_key = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", clean_text(synonym)).replace("ё", "е").lower()
    return bool(name_key and syn_key and name_key == syn_key)


def compact_comment(value: Any) -> str:
    text = clean_text(value, multiline=True)
    if re.match(r"^\s*АПК\s*:", text, flags=re.IGNORECASE):
        return ""
    return text


def apply_type_alias(type_name: str) -> str:
    text = clean_text(type_name)
    if not text:
        return ""
    if text in TYPE_ALIASES:
        return TYPE_ALIASES[text]
    if "." not in text:
        return text
    prefix, rest = text.split(".", 1)
    alias = TYPE_ALIASES.get(prefix)
    if not alias:
        return text
    return f"{alias}.{rest}"


def compact_type_union(parts: Sequence[str]) -> str:
    groups: Dict[str, List[str]] = {}
    plain: List[str] = []
    for part in parts:
        if "." not in part:
            plain.append(part)
            continue
        prefix, name = part.split(".", 1)
        if not prefix or not name:
            plain.append(part)
            continue
        groups.setdefault(prefix, []).append(name)

    compacted: List[str] = []
    for prefix, names in groups.items():
        unique = list(dict.fromkeys(names))
        if len(unique) == 1:
            compacted.append(apply_type_alias(f"{prefix}.{unique[0]}"))
        else:
            alias = TYPE_ALIASES.get(prefix, prefix)
            compacted.append(f"{alias}.({'|'.join(unique)})")
    compacted.extend(apply_type_alias(p) for p in plain)
    return "|".join(compacted)


def compact_metadata_type(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        parts = [clean_text(item) for item in value if clean_text(item)]
    else:
        text = clean_text(value)
        quoted = re.findall(r"'([^']*)'|\"([^\"]*)\"", text)
        parts = [left or right for left, right in quoted if clean_text(left or right)] or [text]
    text = " ".join(parts)
    if not text:
        return ""

    primitive_hits: List[str] = []
    for primitive in ("Число", "Строка", "Дата"):
        if re.search(rf"(?<![А-Яа-яA-Za-z]){primitive}\s*\(", text):
            primitive_hits.append(primitive)
    if len(primitive_hits) == 1:
        return apply_type_alias(primitive_hits[0])
    if len(primitive_hits) > 1:
        return "|".join(apply_type_alias(p) for p in primitive_hits)

    cleaned = [clean_text(p).strip("[]'\" ,") for p in parts]
    cleaned = [p for p in cleaned if p]
    if len(cleaned) == 1:
        return apply_type_alias(cleaned[0])
    return compact_type_union(cleaned)


# ---------------------------------------------------------------------------
# Collapse helpers for numbered attribute series
# ---------------------------------------------------------------------------

def numbered_text_parts(text: Any) -> Optional[Tuple[str, int, str]]:
    """Return `(prefix, number, suffix)` if `text` contains exactly one digit run."""
    raw = clean_text(text)
    if not raw:
        return None
    matches = list(re.finditer(r"\d+", raw))
    if len(matches) != 1:
        return None
    match = matches[0]
    return raw[:match.start()], int(match.group(0)), raw[match.end():]


def format_number_ranges(numbers: Sequence[int]) -> str:
    """`[1,2,3,5,7,8,9] → "1..3|5|7..9"`."""
    if not numbers:
        return ""
    ordered = sorted(set(int(n) for n in numbers))
    ranges: List[str] = []
    start = prev = ordered[0]
    for number in ordered[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append(f"{start}..{prev}" if start != prev else str(start))
        start = prev = number
    ranges.append(f"{start}..{prev}" if start != prev else str(start))
    return "|".join(ranges)


def _collapsed_numbered_synonym(
    members: Sequence[Tuple[int, Dict[str, Any]]],
    *,
    ranges: str,
) -> Optional[str]:
    """Return the collapsed synonym for a series, or `None` to keep members separate.

    Rules:
      * empty everywhere → empty string;
      * synonym equals number itself (`"1"`, `"2"`, ...) → drop;
      * all synonyms identical → keep one;
      * synonyms share `(prefix, suffix)` with the same digit positions
        → collapse to `f"{prefix}[{ranges}]{suffix}"`;
      * otherwise → `None` (do not collapse).
    """
    synonyms = [clean_text(item.get("synonym")) for _num, item in members]
    if not any(synonyms):
        return ""

    if all(syn == str(num) for syn, (num, _item) in zip(synonyms, members)):
        return ""

    unique = {s for s in synonyms if s}
    if len(unique) == 1 and len(synonyms) == len(members):
        # All members share the exact same synonym text.
        return next(iter(unique))

    parts_by_member = []
    for num, item in members:
        parts = numbered_text_parts(item.get("synonym"))
        if parts is None or parts[1] != num:
            return None
        parts_by_member.append(parts)
    prefix0, _n0, suffix0 = parts_by_member[0]
    for prefix, _n, suffix in parts_by_member[1:]:
        if prefix != prefix0 or suffix != suffix0:
            return None
    return f"{prefix0}[{ranges}]{suffix0}"


def collapse_numbered_nodes(
    items: Iterable[Dict[str, Any]], *, min_count: int = 2,
) -> List[Dict[str, Any]]:
    """Collapse runs of `Имя1, Имя2, ..., ИмяN` into one entry `Имя[1..N]`.

    Only collapses groups whose non-name/non-synonym fields are identical
    (so a numbered series with mixed types/comments is preserved as separate
    rows).
    """
    items_list = list(items or [])
    if not items_list:
        return []

    groups: Dict[Tuple[str, str, str], List[Tuple[int, int, Dict[str, Any]]]] = {}
    order: List[Tuple[str, str, str]] = []

    for idx, item in enumerate(items_list):
        parts = numbered_text_parts(item.get("name"))
        if parts is None:
            order.append(("__solo__", str(idx), ""))
            groups[("__solo__", str(idx), "")] = [(idx, idx, item)]
            continue
        prefix, number, suffix = parts
        rest = {k: v for k, v in item.items() if k not in ("name", "synonym")}
        rest_key = json.dumps(rest, ensure_ascii=False, sort_keys=True, default=str)
        key = (prefix, suffix, rest_key)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((number, idx, item))

    output: List[Dict[str, Any]] = []
    used_idx: set[int] = set()

    for key in order:
        members = groups[key]
        if key[0] == "__solo__":
            idx, _, item = members[0]
            output.append((idx, item))  # type: ignore[arg-type]
            continue
        if len(members) < min_count:
            for _num, idx, item in members:
                output.append((idx, item))  # type: ignore[arg-type]
            continue

        prefix, suffix, _rest = key
        numbers = [num for num, _idx, _item in members]
        ranges = format_number_ranges(numbers)
        synonym = _collapsed_numbered_synonym(
            [(num, item) for num, _idx, item in members], ranges=ranges,
        )
        if synonym is None:
            for _num, idx, item in members:
                output.append((idx, item))  # type: ignore[arg-type]
            continue

        base = dict(members[0][2])
        base["name"] = f"{prefix}[{ranges}]{suffix}"
        if synonym:
            base["synonym"] = synonym
        else:
            base.pop("synonym", None)
        first_idx = min(idx for _num, idx, _item in members)
        output.append((first_idx, base))  # type: ignore[arg-type]
        used_idx.update(idx for _num, idx, _item in members)

    output.sort(key=lambda pair: pair[0])
    return [item for _idx, item in output]


# ---------------------------------------------------------------------------
# Profile construction
# ---------------------------------------------------------------------------

def _build_identity(evidence: Dict[str, Any]) -> Dict[str, Any]:
    raw = evidence.get("identity") or {}
    name = clean_text(raw.get("name"))
    synonym = clean_text(raw.get("synonym"))
    if is_redundant_synonym(name, synonym):
        synonym = ""
    return {
        "category": clean_text(raw.get("category")),
        "name": name,
        "synonym": synonym,
        "comment": compact_comment(raw.get("comment")),
        "config_name": clean_text(raw.get("config_name")),
    }


def _build_purpose_hints(evidence: Dict[str, Any]) -> Dict[str, str]:
    raw = evidence.get("identity") or {}
    description = compact_comment(raw.get("description") or raw.get("comment"))[:2000]
    help_text = compact_comment(raw.get("help") or raw.get("help_text"))[:5000]
    explanation = compact_comment(raw.get("explanation"))[:1000]
    out: Dict[str, str] = {}
    if description:
        out["description"] = description
    if help_text:
        out["help_text"] = help_text
    if explanation:
        out["explanation"] = explanation
    return out


def _compact_attribute_like(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": clean_text(item.get("name")),
        "synonym": "" if is_redundant_synonym(item.get("name"), item.get("synonym")) else clean_text(item.get("synonym")),
        "type": compact_metadata_type(item.get("type")),
        "comment": compact_comment(item.get("comment")),
    }


def _strip_empty(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in item.items() if v not in ("", None, [])}


def _is_meaningful_extension_node(item: Dict[str, Any]) -> bool:
    """Include only own elements or borrowed elements with modifications.

    A borrowed element marked only as `controlled` (read-only watcher) is not
    enough to claim that the extension changed behaviour.
    """
    ownership = str(item.get("ownership") or "").strip()
    modified = item.get("modified_properties") or []
    return ownership == "Собственный" or bool(modified)


def _compact_extension_node(item: Dict[str, Any]) -> Dict[str, Any]:
    base = _compact_attribute_like(item)
    ownership = clean_text(item.get("ownership"))
    modified = list(item.get("modified_properties") or [])
    controlled = list(item.get("controlled_properties") or [])
    if ownership:
        base["ownership"] = ownership
    if modified:
        base["modified"] = modified
    if controlled:
        base["controlled"] = controlled
    return base


def _build_structure(evidence: Dict[str, Any], policy: Dict[str, int]) -> Dict[str, Any]:
    raw = evidence.get("structure") or {}

    def section(name: str) -> List[Dict[str, Any]]:
        items = filter_deprecated_1c_items(raw.get(name) or [])
        compact = [_strip_empty(_compact_attribute_like(it)) for it in items]
        return collapse_numbered_nodes(compact)

    attrs = section("attributes")
    resources = section("resources")
    dimensions = section("dimensions")

    tabs: List[Dict[str, Any]] = []
    for tp in filter_deprecated_1c_items(raw.get("tabular_parts") or []):
        tp_attrs = [_strip_empty(_compact_attribute_like(a))
                    for a in filter_deprecated_1c_items(tp.get("attributes") or [])]
        tp_attrs = collapse_numbered_nodes(tp_attrs)
        tab = _strip_empty({
            "name": clean_text(tp.get("name")),
            "synonym": "" if is_redundant_synonym(tp.get("name"), tp.get("synonym")) else clean_text(tp.get("synonym")),
            "attributes": tp_attrs,
        })
        tabs.append(tab)

    forms = [_strip_empty({"name": clean_text(f.get("name")), "type": clean_text(f.get("type"))})
             for f in filter_deprecated_1c_items(raw.get("forms") or [])][: policy["max_forms"]]
    commands = [_strip_empty({"name": clean_text(c.get("name")),
                              "synonym": "" if is_redundant_synonym(c.get("name"), c.get("synonym")) else clean_text(c.get("synonym"))})
                for c in filter_deprecated_1c_items(raw.get("commands") or [])][: policy["max_commands"]]

    layouts = [_strip_empty({"name": clean_text(l.get("name")),
                             "type": clean_text(l.get("type")),
                             "synonym": "" if is_redundant_synonym(l.get("name"), l.get("synonym")) else clean_text(l.get("synonym")),
                             "comment": compact_comment(l.get("comment"))})
               for l in filter_deprecated_1c_items(raw.get("layouts") or [])]

    enum_values = [_strip_empty({"name": clean_text(v.get("name")),
                                 "synonym": "" if is_redundant_synonym(v.get("name"), v.get("synonym")) else clean_text(v.get("synonym")),
                                 "comment": compact_comment(v.get("comment"))})
                   for v in filter_deprecated_1c_items(raw.get("enum_values") or [])]

    predefined = [_strip_empty({"name": clean_text(p.get("name")),
                                "synonym": "" if is_redundant_synonym(p.get("name"), p.get("synonym")) else clean_text(p.get("synonym")),
                                "comment": compact_comment(p.get("comment"))})
                  for p in filter_deprecated_1c_items(raw.get("predefined") or [])]

    url_templates: List[Dict[str, Any]] = []
    for t in filter_deprecated_1c_items(raw.get("url_templates") or []):
        methods = [_strip_empty({"name": clean_text(m.get("name")),
                                 "http_method": clean_text(m.get("http_method"))})
                   for m in t.get("methods") or []]
        url_templates.append(_strip_empty({
            "name": clean_text(t.get("name")),
            "template": clean_text(t.get("template")),
            "methods": [m for m in methods if m],
        }))

    out: Dict[str, Any] = {}
    if attrs:
        out["attributes"] = attrs
    if resources:
        out["resources"] = resources
    if dimensions:
        out["dimensions"] = dimensions
    if tabs:
        out["tabular_parts"] = tabs
    if forms:
        out["forms"] = forms
    if commands:
        out["commands"] = commands
    if layouts:
        out["layouts"] = layouts
    if enum_values:
        out["enum_values"] = enum_values
    if predefined:
        out["predefined"] = predefined
    if url_templates:
        out["url_templates"] = url_templates
    return out


def _build_extension_structure(
    raw_structure: Dict[str, Any], policy: Dict[str, int],
) -> Dict[str, Any]:
    """Compact structure of an extension object, keeping only own/modified.

    The shape mirrors `_build_structure` but each element carries `ownership`,
    `modified` and `controlled` to make extension intent explicit. Size policy
    limits (`max_forms`, `max_commands`) are applied AFTER the own/modified
    filter — otherwise a meaningful change late in the alphabetical list could
    be evicted by trivial borrowed items.
    """

    def section(name: str) -> List[Dict[str, Any]]:
        items = filter_deprecated_1c_items(raw_structure.get(name) or [])
        items = [it for it in items if _is_meaningful_extension_node(it)]
        compact = [_strip_empty(_compact_extension_node(it)) for it in items]
        return collapse_numbered_nodes(compact)

    attrs = section("attributes")
    resources = section("resources")
    dimensions = section("dimensions")

    tabs: List[Dict[str, Any]] = []
    for tp in filter_deprecated_1c_items(raw_structure.get("tabular_parts") or []):
        own_tp = _is_meaningful_extension_node(tp)
        tp_attrs = [_strip_empty(_compact_extension_node(a))
                    for a in filter_deprecated_1c_items(tp.get("attributes") or [])
                    if _is_meaningful_extension_node(a)]
        tp_attrs = collapse_numbered_nodes(tp_attrs)
        if not own_tp and not tp_attrs:
            continue
        tab = _strip_empty({
            "name": clean_text(tp.get("name")),
            "synonym": "" if is_redundant_synonym(tp.get("name"), tp.get("synonym")) else clean_text(tp.get("synonym")),
            "ownership": clean_text(tp.get("ownership")),
            "modified": list(tp.get("modified_properties") or []),
            "controlled": list(tp.get("controlled_properties") or []),
            "attributes": tp_attrs,
        })
        tabs.append(tab)

    forms: List[Dict[str, Any]] = []
    for f in filter_deprecated_1c_items(raw_structure.get("forms") or []):
        if not _is_meaningful_extension_node(f):
            continue
        forms.append(_strip_empty({
            "name": clean_text(f.get("name")),
            "type": clean_text(f.get("type")),
            "ownership": clean_text(f.get("ownership")),
            "modified": list(f.get("modified_properties") or []),
        }))
    forms = forms[: policy["max_forms"]]

    commands: List[Dict[str, Any]] = []
    for c in filter_deprecated_1c_items(raw_structure.get("commands") or []):
        if not _is_meaningful_extension_node(c):
            continue
        commands.append(_strip_empty({
            "name": clean_text(c.get("name")),
            "synonym": "" if is_redundant_synonym(c.get("name"), c.get("synonym")) else clean_text(c.get("synonym")),
            "ownership": clean_text(c.get("ownership")),
            "modified": list(c.get("modified_properties") or []),
        }))
    commands = commands[: policy["max_commands"]]

    # `layouts/enum_values/predefined/url_templates` can be borrowed via
    # ADOPTED_FROM relations in the extension graph (see
    # `extension_relationships_builder`), so apply the same own/modified filter
    # as for attributes — otherwise borrowed-but-unchanged items would
    # falsely claim the extension changed object composition.
    def _own_or_modified(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [it for it in filter_deprecated_1c_items(items or [])
                if _is_meaningful_extension_node(it)]

    layouts = [_strip_empty({"name": clean_text(l.get("name")),
                             "type": clean_text(l.get("type")),
                             "synonym": "" if is_redundant_synonym(l.get("name"), l.get("synonym")) else clean_text(l.get("synonym")),
                             "comment": compact_comment(l.get("comment")),
                             "ownership": clean_text(l.get("ownership")),
                             "modified": list(l.get("modified_properties") or [])})
               for l in _own_or_modified(raw_structure.get("layouts"))]
    enum_values = [_strip_empty({"name": clean_text(v.get("name")),
                                 "synonym": "" if is_redundant_synonym(v.get("name"), v.get("synonym")) else clean_text(v.get("synonym")),
                                 "comment": compact_comment(v.get("comment")),
                                 "ownership": clean_text(v.get("ownership")),
                                 "modified": list(v.get("modified_properties") or [])})
                   for v in _own_or_modified(raw_structure.get("enum_values"))]
    predefined = [_strip_empty({"name": clean_text(p.get("name")),
                                "synonym": "" if is_redundant_synonym(p.get("name"), p.get("synonym")) else clean_text(p.get("synonym")),
                                "comment": compact_comment(p.get("comment")),
                                "ownership": clean_text(p.get("ownership")),
                                "modified": list(p.get("modified_properties") or [])})
                  for p in _own_or_modified(raw_structure.get("predefined"))]
    url_templates: List[Dict[str, Any]] = []
    for t in _own_or_modified(raw_structure.get("url_templates")):
        methods = [_strip_empty({"name": clean_text(m.get("name")),
                                 "http_method": clean_text(m.get("http_method"))})
                   for m in t.get("methods") or []]
        url_templates.append(_strip_empty({
            "name": clean_text(t.get("name")),
            "template": clean_text(t.get("template")),
            "ownership": clean_text(t.get("ownership")),
            "modified": list(t.get("modified_properties") or []),
            "methods": [m for m in methods if m],
        }))

    out: Dict[str, Any] = {}
    if attrs:
        out["attributes"] = attrs
    if resources:
        out["resources"] = resources
    if dimensions:
        out["dimensions"] = dimensions
    if tabs:
        out["tabular_parts"] = tabs
    if forms:
        out["forms"] = forms
    if commands:
        out["commands"] = commands
    if layouts:
        out["layouts"] = layouts
    if enum_values:
        out["enum_values"] = enum_values
    if predefined:
        out["predefined"] = predefined
    if url_templates:
        out["url_templates"] = url_templates
    return out


def _group_relationships(items: List[Dict[str, Any]], cap: int) -> List[Dict[str, Any]]:
    """Collapse per-edge rows into `{relation, category, targets[]}` groups."""
    groups: Dict[tuple, List[str]] = {}
    order: List[tuple] = []
    for it in items or []:
        rel = clean_text(it.get("relation"))
        cat = clean_text(it.get("category") or it.get("target_category"))
        name = clean_text(it.get("name") or it.get("target_name") or it.get("qualified_name"))
        if not name:
            continue
        key = (rel, cat)
        if key not in groups:
            groups[key] = []
            order.append(key)
        if name not in groups[key]:
            groups[key].append(name)
    out: List[Dict[str, Any]] = []
    total = 0
    for key in order:
        rel, cat = key
        targets = groups[key]
        out.append({"relation": rel, "category": cat, "targets": targets})
        total += len(targets)
        if total >= cap:
            break
    return out


def _build_relationships(evidence: Dict[str, Any], policy: Dict[str, int]) -> Dict[str, Any]:
    raw = evidence.get("relationships") or {}
    cap = policy["max_relationships_total"]
    return {
        "affects": _group_relationships(raw.get("affects") or [], cap),
        "affected_by": _group_relationships(raw.get("affected_by") or [], cap),
        "uses": _group_relationships(raw.get("uses") or [], cap),
        "used_by": _group_relationships(raw.get("used_by") or [], cap),
    }


def _build_bsl_block(
    *, category: str, evidence: Dict[str, Any], policy: Dict[str, int],
) -> Dict[str, Any]:
    return build_bsl_profile(
        category=category,
        routines=evidence.get("bsl_routines") or [],
        handlers=evidence.get("bsl_handlers") or [],
        call_edges=evidence.get("bsl_call_edges") or [],
        max_routines=policy["max_bsl_routines"],
    )


def _build_extension_context(
    evidence: Dict[str, Any], policy: Dict[str, int],
) -> Dict[str, Any]:
    """Inline own/modified structure + extension BSL for each adopting extension.

    Skip extensions whose own/modified structure is empty AND whose BSL has
    no selected routines: such adopters contribute nothing meaningful.
    """
    ctx = evidence.get("extension_context") or {}
    if not isinstance(ctx, dict):
        return {}

    mode = ctx.get("mode") or "none"
    summary = clean_text(ctx.get("summary"))
    raw_exts = ctx.get("extensions") or []
    category = clean_text((evidence.get("identity") or {}).get("category"))

    extensions: List[Dict[str, Any]] = []
    for ext in raw_exts:
        if not isinstance(ext, dict):
            continue
        ext_structure_raw = ext.get("structure") or {}
        ext_structure = _build_extension_structure(ext_structure_raw, policy)
        ext_bsl = build_bsl_profile(
            category=category,
            routines=ext.get("bsl_routines") or [],
            handlers=ext.get("bsl_handlers") or [],
            call_edges=ext.get("bsl_call_edges") or [],
            max_routines=policy["max_bsl_routines"],
        )
        bsl_has_content = bool(ext_bsl.get("routines") or ext_bsl.get("handlers"))
        if not ext_structure and not bsl_has_content:
            continue
        entry = _strip_empty({
            "config_name": clean_text(ext.get("config_name")),
            "qualified_name": clean_text(ext.get("qualified_name")),
            "object_structure": ext_structure,
            "bsl_profile": ext_bsl if bsl_has_content else {},
        })
        extensions.append(entry)

    # If no extension survived the own/modified filter, drop the block entirely:
    # claiming "base object changed by extensions" without facts of change is
    # noise for the LLM.
    if not extensions:
        return {}

    out: Dict[str, Any] = {"mode": mode}
    if summary:
        out["summary"] = summary
    out["extensions"] = extensions
    return out


def _build_type_aliases_block() -> Dict[str, str]:
    return {v: k for k, v in TYPE_ALIASES.items()}


# Bsl code-lines compress like `Отказ;Сообщить;>Имя;Записать:НаборЗаписей`.
# The legend is emitted alongside `bsl_profile` so the LLM never has to guess
# what the abbreviations mean.
_BSL_FORMAT_LEGEND: Dict[str, str] = {
    ";": "разделитель фактов",
    ">Имя": "вызов процедуры/функции",
    "set:Поле": "присваивание полю объекта",
    "Очистить:Поле": "очистка коллекции поля",
    "Добавить:Поле": "добавление в коллекцию поля",
    "Записать:Имя": "запись данных через объект/набор/менеджер",
    "Записывать:Имя": "включение записи (Записывать = Истина) для набора",
    "Провести:Имя": "запись с РежимЗаписиДокумента.Проведение",
    "ОтменитьПроведение:Имя": "запись с РежимЗаписиДокумента.ОтменаПроведения",
    "Прочитать:Имя": "чтение данных",
    "Удалить:Имя": "удаление данных",
    "ПометкаУдаления:Имя": "установка пометки удаления",
    "Загрузить:Имя": "загрузка из внешнего источника",
    "Выгрузить:Имя": "выгрузка во внешний приёмник",
    "Найти:Имя": "поиск (НайтиПоКоду/НайтиПоНаименованию/НайтиПоРеквизиту)",
    "Получить:Имя": "получение объекта по ссылке",
    "Установить:Имя": "установка значения",
    "НаборЗаписей:Регистр": "создание набора записей регистра",
    "МенеджерЗаписи:Регистр": "создание менеджера записи регистра",
    "Запрос:Источник": "источник в запросе (ИЗ/FROM/ПОМЕСТИТЬ/JOIN/СОЕДИНЕНИЕ)",
    "Движения.Имя": "ссылка на движения регистра",
    "Отказ": "процедура может остановить запись/проведение",
    "Сообщить": "вывод сообщения пользователю",
    "Исключение": "ВызватьИсключение",
    "HTTP": "обращение к HTTP-каналу",
    "Файл": "работа с файлами/текстом/XML",
    "COM": "обращение к COM-объекту",
    "WebСервис": "обращение к web-сервису или HTTP-сервису",
}


def build_profile(evidence: Dict[str, Any], *, size_policy: str = "medium") -> Dict[str, Any]:
    """Compact `evidence` into an LLM-ready `object_profile` dict."""
    policy = get_size_policy(size_policy)

    identity = _build_identity(evidence)
    purpose_hints = _build_purpose_hints(evidence)
    structure = _build_structure(evidence, policy)
    relationships = _build_relationships(evidence, policy)
    bsl_block = _build_bsl_block(category=identity.get("category", ""),
                                 evidence=evidence, policy=policy)
    extension_context = _build_extension_context(evidence, policy)
    warnings = evidence.get("warnings") or []

    profile: Dict[str, Any] = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "size_policy": size_policy,
        "object_identity": identity,
        "type_aliases": _build_type_aliases_block(),
        "object_structure": structure,
        "object_relationships": relationships,
    }
    if purpose_hints:
        profile["object_purpose_hints"] = purpose_hints
    bsl_has_content = bool(
        bsl_block.get("routines") or bsl_block.get("handlers") or bsl_block.get("flows")
    )
    if bsl_has_content:
        profile["bsl_format"] = _BSL_FORMAT_LEGEND
        profile["bsl_profile"] = bsl_block
    if extension_context:
        profile["extension_context"] = extension_context
    if warnings:
        profile["profile_warnings"] = list(warnings)
    return profile
