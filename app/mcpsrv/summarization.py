"""
Summarization helpers and simple formatting.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
import logging

from config import settings


_PREFIX_MIN_COUNT = 3
_PREFIX_MIN_LENGTH = 15
_PREFIX_MAX_ITERATIONS = 10
_PREFIX_REF_OVERHEAD = 5
_PREFIX_EXPAND_MAX_DEPTH = 5

_CATEGORY_KEYS = frozenset({
    "category", "category_name",
    "owner_category", "source_category",
    "target_category", "result_category",
})


def _is_qn_key(key: str) -> bool:
    """Keys whose string values are qualified names routed to the @qn namespace."""
    return key == "qualified_name" or key == "qualified_name_prefix" or key.endswith("_qn")


def _expanded_length(candidate: str, prefixes: Dict[str, str]) -> int:
    """Return length of `candidate` with all leading @p:N tokens recursively expanded.

    Used by _extract_prefixes so that on second+ iterations the threshold/savings
    are evaluated against the raw path length, not against the already-shortened
    representation. Without this, a candidate like `@p:1/Attribute` (14 chars)
    is unfairly compared with raw first-iteration candidates of 80+ chars.
    """
    text = candidate
    for _ in range(_PREFIX_EXPAND_MAX_DEPTH):
        if not text.startswith("@p:"):
            break
        slash_idx = text.find("/")
        head = text if slash_idx == -1 else text[:slash_idx]
        rest = "" if slash_idx == -1 else text[slash_idx:]
        if head not in prefixes:
            break
        text = prefixes[head] + rest
    return len(text)


def _to_text(obj: Any) -> str:
    try:
        return str(obj)
    except Exception:
        return "<unserializable>"


def strip_empty_strings(obj: Any) -> Any:
    """
    Recursively remove dict entries whose value is an empty string "".
    Does not remove list items or entire objects; only prunes string-empty fields.
    """
    try:
        if isinstance(obj, dict):
            return {k: strip_empty_strings(v) for k, v in obj.items() if not (isinstance(v, str) and v == "")}
        elif isinstance(obj, list):
            return [strip_empty_strings(v) for v in obj]
        elif isinstance(obj, tuple):
            return tuple(strip_empty_strings(v) for v in obj)
        else:
            return obj
    except Exception:
        return obj


def strip_fields(obj: Any, exclude_override: Optional[list] = None) -> Any:
    """
    Recursively remove:
      - Any fields whose key is in exclude_override (if provided) or settings.metadata_summarize_exclude_fields.
      - Any fields whose key is in settings.metadata_summarize_exclude_field_values AND value equals any of the configured values.
    Pass exclude_override=[] in template-path to skip field-key exclusion entirely.
    """
    try:
        exclude_keys = set(
            exclude_override if exclude_override is not None
            else getattr(settings, "metadata_summarize_exclude_fields", []) or []
        )
        exclude_kv: Dict[str, List[Any]] = getattr(settings, "metadata_summarize_exclude_field_values", {}) or {}
        if isinstance(obj, dict):
            new_d = {}
            for k, v in obj.items():
                if k in exclude_keys:
                    continue
                if k in exclude_kv:
                    vals = exclude_kv.get(k) or []
                    if isinstance(v, (str, int, float, bool)) and v in vals:
                        continue
                new_d[k] = strip_fields(v, exclude_override)
            return new_d
        elif isinstance(obj, list):
            return [strip_fields(v, exclude_override) for v in obj]
        elif isinstance(obj, tuple):
            return tuple(strip_fields(v, exclude_override) for v in obj)
        else:
            return obj
    except Exception:
        return obj


def filter_for_summarization(data: Any, exclude_override: Optional[list] = None) -> Any:
    """
    Apply all configured summarization filters:
      - Drop empty-string fields if metadata_summarize_drop_empty_strings == True
      - Exclude fields by name and by exact (field,value) matches

    Pass exclude_override=[] in template-path calls to prevent stripping config_name/qualified_name.
    """
    try:
        out = data
        if getattr(settings, "metadata_summarize_drop_empty_strings", False):
            out = strip_empty_strings(out)
        if getattr(settings, "metadata_summarize_exclude_fields", None) or getattr(
            settings, "metadata_summarize_exclude_field_values", None
        ):
            out = strip_fields(out, exclude_override=exclude_override)
        return out
    except Exception:
        return data


def _walk_compact(obj: Any, ref_config_fn, ref_qn_fn, ref_adoption_fn=None, ref_right_fn=None, ref_type_fn=None, ref_category_fn=None) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if ref_adoption_fn is not None and k == "adoption" and isinstance(v, dict):
                out[k] = ref_adoption_fn(v)
            elif ref_type_fn is not None and k == "type" and isinstance(v, str) and v:
                out[k] = ref_type_fn(v)
            elif isinstance(v, dict):
                out[k] = _walk_compact(v, ref_config_fn, ref_qn_fn, ref_adoption_fn, ref_right_fn, ref_type_fn, ref_category_fn)
            elif v is None or v == "":
                out[k] = v
            elif isinstance(v, list) and (k == "config_name" or k.endswith("_config_name") or k.endswith("_config_names")):
                out[k] = [ref_config_fn(i) if isinstance(i, str) and i else i for i in v]
            elif isinstance(v, list) and _is_qn_key(k):
                out[k] = [ref_qn_fn(i) if isinstance(i, str) and i else i for i in v]
            elif isinstance(v, list) and ref_right_fn is not None and k == "right_ru":
                out[k] = [ref_right_fn(i) if isinstance(i, str) and i else i for i in v]
            elif isinstance(v, list) and ref_category_fn is not None and k in _CATEGORY_KEYS:
                out[k] = [ref_category_fn(i) if isinstance(i, str) and i else i for i in v]
            elif isinstance(v, list):
                out[k] = [_walk_compact(i, ref_config_fn, ref_qn_fn, ref_adoption_fn, ref_right_fn, ref_type_fn, ref_category_fn) for i in v]
            elif isinstance(v, str) and (k == "config_name" or k.endswith("_config_name")):
                out[k] = ref_config_fn(v)
            elif isinstance(v, str) and _is_qn_key(k):
                out[k] = ref_qn_fn(v)
            elif isinstance(v, str) and ref_right_fn is not None and k == "right_ru":
                out[k] = ref_right_fn(v)
            elif isinstance(v, str) and ref_category_fn is not None and k in _CATEGORY_KEYS:
                out[k] = ref_category_fn(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_walk_compact(i, ref_config_fn, ref_qn_fn, ref_adoption_fn, ref_right_fn, ref_type_fn, ref_category_fn) for i in obj]
    return obj


def _extract_prefixes(qn_table: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Iteratively extract repeated path prefixes from qn_table, replacing them with @p:N refs.

    Modifies qn_table in place. Each iteration picks the single most profitable
    candidate (savings = count * (length - ref_overhead)) and applies it. Stops
    when no candidate meets the count/length threshold or after a bounded number
    of iterations. Already-extracted prefixes are skipped via prefix_rev membership.
    """
    prefixes: Dict[str, str] = {}
    prefix_rev: Dict[str, str] = {}

    for _ in range(_PREFIX_MAX_ITERATIONS):
        prefix_count: Dict[str, int] = {}
        for val in qn_table.values():
            parts = val.split("/")
            for n in range(2, len(parts) + 1):
                candidate = "/".join(parts[:n])
                if candidate in prefix_rev:
                    continue
                prefix_count[candidate] = prefix_count.get(candidate, 0) + 1

        length_cache: Dict[str, int] = {}

        def _eff_len(c: str) -> int:
            if c not in length_cache:
                length_cache[c] = _expanded_length(c, prefixes)
            return length_cache[c]

        candidates = [
            (c, n) for c, n in prefix_count.items()
            if n >= _PREFIX_MIN_COUNT and _eff_len(c) >= _PREFIX_MIN_LENGTH
        ]
        if not candidates:
            break

        best_candidate, _best_count = max(
            candidates, key=lambda x: x[1] * (_eff_len(x[0]) - _PREFIX_REF_OVERHEAD)
        )
        key = f"@p:{len(prefixes) + 1}"
        prefixes[key] = best_candidate
        prefix_rev[best_candidate] = key

        suffix_marker = best_candidate + "/"
        for qn_key, val in list(qn_table.items()):
            if val == best_candidate:
                qn_table[qn_key] = key
            elif val.startswith(suffix_marker):
                qn_table[qn_key] = key + "/" + val[len(suffix_marker):]

    return prefixes, prefix_rev


def _path_prefix_eligible(val: Any) -> bool:
    """True if `val` is a 1С dot-path eligible for path_prefixes compaction.

    Only the scalar key `path` is fed here. File paths (containing "/" or "\\")
    are excluded so we never touch file_path-like values, and values with fewer
    than 3 dot-segments are too short to profit from a shared prefix.
    """
    if not isinstance(val, str) or not val:
        return False
    if "/" in val or "\\" in val:
        return False
    return val.count(".") >= 2


def _extract_path_prefixes(path_values: List[str]) -> Dict[str, str]:
    """Build a {@pathp:N -> prefix} table for repeated 1С dot-path prefixes.

    Mirrors _extract_prefixes thresholds (min count/length, bounded iterations)
    but uses the "." separator and keeps prefixes single-level: once a value is
    covered by a prefix it is excluded from further candidate counting, so an
    extracted prefix never nests inside another. Returns {} when nothing profits.
    """
    prefix_rev: Dict[str, str] = {}
    prefixes: Dict[str, str] = {}
    # Work on a mutable copy; covered values are marked so they stop contributing.
    values = [v for v in path_values if _path_prefix_eligible(v)]

    for _ in range(_PREFIX_MAX_ITERATIONS):
        prefix_count: Dict[str, int] = {}
        for val in values:
            if val.startswith("@pathp:"):
                continue
            parts = val.split(".")
            for n in range(2, len(parts) + 1):
                candidate = ".".join(parts[:n])
                if candidate in prefix_rev:
                    continue
                prefix_count[candidate] = prefix_count.get(candidate, 0) + 1

        candidates = [
            (c, n) for c, n in prefix_count.items()
            if n >= _PREFIX_MIN_COUNT and len(c) >= _PREFIX_MIN_LENGTH
        ]
        if not candidates:
            break

        best_candidate, _best_count = max(
            candidates, key=lambda x: x[1] * (len(x[0]) - _PREFIX_REF_OVERHEAD)
        )
        key = f"@pathp:{len(prefixes) + 1}"
        prefixes[key] = best_candidate
        prefix_rev[best_candidate] = key

        suffix_marker = best_candidate + "."
        for i, val in enumerate(values):
            if val == best_candidate:
                values[i] = key
            elif val.startswith(suffix_marker):
                values[i] = key + "." + val[len(suffix_marker):]

    return prefixes


def _apply_path_prefix(val: str, prefix_rev: Dict[str, str]) -> str:
    """Replace the longest matching dot-prefix of `val` with its @pathp:N ref."""
    best: Optional[str] = None
    for prefix in prefix_rev:
        if val == prefix or val.startswith(prefix + "."):
            if best is None or len(prefix) > len(best):
                best = prefix
    if best is None:
        return val
    ref = prefix_rev[best]
    # val[len(best):] keeps the leading "." (e.g. ".Реквизиты.Организация").
    return ref if val == best else ref + val[len(best):]


def _collect_path_values(obj: Any, acc: List[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "path" and _path_prefix_eligible(v):
                acc.append(v)
            else:
                _collect_path_values(v, acc)
    elif isinstance(obj, list):
        for i in obj:
            _collect_path_values(i, acc)


def _rewrite_path_values(obj: Any, prefix_rev: Dict[str, str]) -> Any:
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if k == "path" and _path_prefix_eligible(v):
                out[k] = _apply_path_prefix(v, prefix_rev)
            else:
                out[k] = _rewrite_path_values(v, prefix_rev)
        return out
    if isinstance(obj, list):
        return [_rewrite_path_values(i, prefix_rev) for i in obj]
    return obj


def _build_path_prefixes(payload: Any) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Return (path_prefixes_table, prefix_rev) for `path` scalars in `payload`."""
    path_values: List[str] = []
    _collect_path_values(payload, path_values)
    prefixes = _extract_path_prefixes(path_values)
    prefix_rev = {v: k for k, v in prefixes.items()}
    return prefixes, prefix_rev


def compact_refs(rows: List[Dict], compact_types: bool = False) -> Dict:
    """
    Replace repeated config_name/*_config_name and qualified_name/*_qn string values
    with short symbolic references (@config:N, @qn:N) to reduce token usage.
    Common Project/Config prefixes in qn values are further extracted into @p:N refs.
    Adoption dict-values are stored once in `adoptions` and replaced with @a:N refs.
    Access-right names (right_ru) are interned into `right_names` and replaced with @rn:N refs.
    Category names (whitelisted keys: category, category_name, owner_category,
    source_category, target_category, result_category) are interned into
    `category_names` and replaced with @cat:N refs.
    When compact_types=True, scalar `type` strings (single atom or "|"-joined atoms) are
    interned into `types` with @t:N refs; composites become "@t:N|@t:M|..." in the row.
    Returns {"configs": {...}, "prefixes": {...}, "qn": {...}, "adoptions": {...},
             "right_names": {...}, "category_names": {...}, "types": {...}, "rows": [...]}.

    Applied when response_compact_refs=true and response_format is json or toon.
    None and "" values are passed through without compaction.
    """
    configs: Dict[str, str] = {}
    qn_table: Dict[str, str] = {}
    adoptions: Dict[str, Any] = {}
    right_names: Dict[str, str] = {}
    category_names: Dict[str, str] = {}
    types: Dict[str, str] = {}
    configs_rev: Dict[str, str] = {}
    qn_rev: Dict[str, str] = {}
    adoptions_rev: Dict[str, str] = {}
    right_names_rev: Dict[str, str] = {}
    category_names_rev: Dict[str, str] = {}
    types_rev: Dict[str, str] = {}

    def _ref_config(val: str) -> str:
        if val in configs_rev:
            return configs_rev[val]
        key = f"@config:{len(configs_rev) + 1}"
        configs_rev[val] = key
        configs[key] = val
        return key

    def _ref_qn(val: str) -> str:
        if val in qn_rev:
            return qn_rev[val]
        key = f"@qn:{len(qn_rev) + 1}"
        qn_rev[val] = key
        qn_table[key] = val
        return key

    def _ref_right(val: str) -> str:
        if val in right_names_rev:
            return right_names_rev[val]
        key = f"@rn:{len(right_names_rev) + 1}"
        right_names_rev[val] = key
        right_names[key] = val
        return key

    def _ref_category(val: str) -> str:
        if val in category_names_rev:
            return category_names_rev[val]
        key = f"@cat:{len(category_names_rev) + 1}"
        category_names_rev[val] = key
        category_names[key] = val
        return key

    def _ref_type(val: str) -> str:
        atoms = [a for a in val.split("|") if a]
        if not atoms:
            return val
        refs = []
        for atom in atoms:
            if atom in types_rev:
                refs.append(types_rev[atom])
                continue
            key = f"@t:{len(types_rev) + 1}"
            types_rev[atom] = key
            types[key] = atom
            refs.append(key)
        return "|".join(refs)

    def _ref_adoption(val: Any) -> Any:
        if val is None or val == "" or not isinstance(val, dict):
            return val
        compacted = _walk_compact(val, _ref_config, _ref_qn, None, _ref_right, None, _ref_category)
        key_str = json.dumps(compacted, sort_keys=True, ensure_ascii=False)
        if key_str in adoptions_rev:
            return adoptions_rev[key_str]
        ref = f"@a:{len(adoptions_rev) + 1}"
        adoptions_rev[key_str] = ref
        adoptions[ref] = compacted
        return ref

    type_cb = _ref_type if compact_types else None
    compact_rows = [_walk_compact(r, _ref_config, _ref_qn, _ref_adoption, _ref_right, type_cb, _ref_category) if isinstance(r, dict) else r for r in rows]
    prefixes, _ = _extract_prefixes(qn_table)
    result: Dict[str, Any] = {"configs": configs, "prefixes": prefixes, "qn": qn_table, "adoptions": adoptions, "right_names": right_names, "category_names": category_names}
    if compact_types:
        result["types"] = types
    path_prefixes, path_prefix_rev = _build_path_prefixes(compact_rows)
    if path_prefixes:
        compact_rows = _rewrite_path_values(compact_rows, path_prefix_rev)
        result["path_prefixes"] = path_prefixes
    result["rows"] = compact_rows
    return result


def compact_refs_dict(data: Dict, compact_types: bool = False, compact_property_names: bool = False, compact_section_kind_names: bool = False) -> Dict:
    """
    Like compact_refs but takes a Dict and preserves its shape.
    Lists stay as lists (no {rows: [...]} wrapper).
    Ref tables are hoisted to top-level keys alongside the section keys:
      {"configs": {...}, "prefixes": {...}, "qn": {...}, "adoptions": {...},
       "right_names": {...}, "category_names": {...}, "types": {...},
       <section>: <original_shape>, ...}
    Category names (whitelisted keys: category, category_name, owner_category,
    source_category, target_category, result_category) are interned into
    `category_names` and replaced with @cat:N refs.
    When compact_types=True, scalar `type` strings are interned into `types` with @t:N refs.
    When compact_property_names=True, the `property` field of rows in the top-level
    `properties`, `property_changes` and `complex_property_values` sections is
    interned into a `property_names` {@prop:N -> name} table (created only when
    there is more than one eligible property row across those sections, with a
    shared @prop:N numbering). `property` fields nested inside rows of other
    sections stay untouched.
    When compact_section_kind_names=True, the `section` field of top-level
    `counts` and `metadata_changes` rows is interned into a `section_names`
    {@sec:N -> name} table, and the `kind` field of top-level `counts`,
    `metadata_changes` and `code_changes` rows into a `kind_names`
    {@kind:N -> name} table. Each table is created only when more than one
    eligible row carries the field, with stable numbering by first appearance
    and a shared ref for equal values across sections. `section`/`kind` fields
    nested inside rows of other sections stay untouched.
    None and "" values are passed through without compaction.
    """
    configs: Dict[str, str] = {}
    qn_table: Dict[str, str] = {}
    adoptions: Dict[str, Any] = {}
    right_names: Dict[str, str] = {}
    category_names: Dict[str, str] = {}
    types: Dict[str, str] = {}
    configs_rev: Dict[str, str] = {}
    qn_rev: Dict[str, str] = {}
    adoptions_rev: Dict[str, str] = {}
    right_names_rev: Dict[str, str] = {}
    category_names_rev: Dict[str, str] = {}
    types_rev: Dict[str, str] = {}

    def _ref_config(val: str) -> str:
        if val in configs_rev:
            return configs_rev[val]
        key = f"@config:{len(configs_rev) + 1}"
        configs_rev[val] = key
        configs[key] = val
        return key

    def _ref_qn(val: str) -> str:
        if val in qn_rev:
            return qn_rev[val]
        key = f"@qn:{len(qn_rev) + 1}"
        qn_rev[val] = key
        qn_table[key] = val
        return key

    def _ref_right(val: str) -> str:
        if val in right_names_rev:
            return right_names_rev[val]
        key = f"@rn:{len(right_names_rev) + 1}"
        right_names_rev[val] = key
        right_names[key] = val
        return key

    def _ref_category(val: str) -> str:
        if val in category_names_rev:
            return category_names_rev[val]
        key = f"@cat:{len(category_names_rev) + 1}"
        category_names_rev[val] = key
        category_names[key] = val
        return key

    def _ref_type(val: str) -> str:
        atoms = [a for a in val.split("|") if a]
        if not atoms:
            return val
        refs = []
        for atom in atoms:
            if atom in types_rev:
                refs.append(types_rev[atom])
                continue
            key = f"@t:{len(types_rev) + 1}"
            types_rev[atom] = key
            types[key] = atom
            refs.append(key)
        return "|".join(refs)

    def _ref_adoption(val: Any) -> Any:
        if val is None or val == "" or not isinstance(val, dict):
            return val
        compacted_val = _walk_compact(val, _ref_config, _ref_qn, None, _ref_right, None, _ref_category)
        key_str = json.dumps(compacted_val, sort_keys=True, ensure_ascii=False)
        if key_str in adoptions_rev:
            return adoptions_rev[key_str]
        ref = f"@a:{len(adoptions_rev) + 1}"
        adoptions_rev[key_str] = ref
        adoptions[ref] = compacted_val
        return ref

    type_cb = _ref_type if compact_types else None
    compacted = {k: _walk_compact(v, _ref_config, _ref_qn, _ref_adoption, _ref_right, type_cb, _ref_category) for k, v in data.items()}
    prefixes, _ = _extract_prefixes(qn_table)
    head: Dict[str, Any] = {"configs": configs, "prefixes": prefixes, "qn": qn_table, "adoptions": adoptions, "right_names": right_names, "category_names": category_names}
    if compact_types:
        head["types"] = types
    path_prefixes, path_prefix_rev = _build_path_prefixes(compacted)
    if path_prefixes:
        compacted = _rewrite_path_values(compacted, path_prefix_rev)
        head["path_prefixes"] = path_prefixes
    if compact_property_names:
        eligible = []
        for section in ("properties", "property_changes", "complex_property_values"):
            prop_rows = compacted.get(section)
            if isinstance(prop_rows, list):
                eligible.extend(
                    r for r in prop_rows
                    if isinstance(r, dict) and isinstance(r.get("property"), str) and r.get("property")
                )
        if len(eligible) > 1:
            property_names: Dict[str, str] = {}
            property_names_rev: Dict[str, str] = {}
            for r in eligible:
                name = r["property"]
                if name not in property_names_rev:
                    key = f"@prop:{len(property_names_rev) + 1}"
                    property_names_rev[name] = key
                    property_names[key] = name
                r["property"] = property_names_rev[name]
            head["property_names"] = property_names
    if compact_section_kind_names:
        def _intern_top_level_field(sections, field_name, table_name, ref_prefix):
            eligible = []
            for section in sections:
                rows = compacted.get(section)
                if isinstance(rows, list):
                    eligible.extend(
                        r for r in rows
                        if isinstance(r, dict)
                        and isinstance(r.get(field_name), str) and r.get(field_name)
                    )
            if len(eligible) <= 1:
                return
            table: Dict[str, str] = {}
            rev: Dict[str, str] = {}
            for r in eligible:
                val = r[field_name]
                if val not in rev:
                    key = f"{ref_prefix}:{len(rev) + 1}"
                    rev[val] = key
                    table[key] = val
                r[field_name] = rev[val]
            head[table_name] = table

        _intern_top_level_field(("counts", "metadata_changes"), "section", "section_names", "@sec")
        _intern_top_level_field(("counts", "metadata_changes", "code_changes"), "kind", "kind_names", "@kind")
    return {**head, **compacted}


def format_results_simple(results: List[Dict], max_results: int) -> str:
    """Simple formatting fallback for results."""
    if not results:
        return "No results found."

    formatted = [f"Found {len(results)} results:"]
    for i, result in enumerate(results[:max_results], 1):
        try:
            formatted.append(f"\n{i}. " + " | ".join(f"{k}: {v}" for k, v in result.items() if v is not None))
        except Exception as e:
            logging.debug("Formatting error for result %s: %s", i, _to_text(e))
            formatted.append(f"\n{i}. <unprintable>")

    if len(results) > max_results:
        formatted.append(f"\n... and {len(results) - max_results} more results")

    return "\n".join(formatted)


__all__ = [
    "strip_empty_strings",
    "strip_fields",
    "filter_for_summarization",
    "compact_refs",
    "compact_refs_dict",
    "format_results_simple",
]
