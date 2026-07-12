"""Helpers for building :ConsoleSearchable search-fields used by the web console."""
from __future__ import annotations

import re
from typing import Any, Dict, Mapping, Optional

_SECTION_BY_KIND: Dict[str, str] = {
    "object": "objects",
    "attribute": "attributes",
    "standard_attribute": "standard_attributes",
    "tabular_part": "tabular_parts",
    "tabular_part_attribute": "tabular_part_attributes",
    "resource": "resources",
    "dimension": "dimensions",
    "form": "forms",
    "command": "commands",
    "layout": "layouts",
    "journal_graph": "journal_graphs",
    "enum_value": "enum_values",
    "predefined": "predefined",
    "module": "modules",
    "form_attribute": "form_attributes",
    "form_control": "form_controls",
}

_CAMEL_SPLIT_RE = re.compile(r"(?<=[а-яa-z])(?=[А-ЯA-Z])|(?<=[А-ЯA-Z])(?=[А-ЯA-Z][а-яa-z])")


def _split_camel(text: str) -> str:
    if not text:
        return ""
    parts = _CAMEL_SPLIT_RE.split(text)
    return " ".join(p for p in parts if p)


def _norm(text: str) -> str:
    if not text:
        return ""
    return (
        text.lower()
        .replace("ё", "е")
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def _coalesce_props(props: Optional[Mapping[str, Any]], keys: tuple[str, ...]) -> str:
    if not props:
        return ""
    for k in keys:
        v = props.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v is not None and not isinstance(v, (list, dict)):
            s = str(v).strip()
            if s:
                return s
    return ""


def build_console_search(
    name: Optional[str],
    props: Optional[Mapping[str, Any]],
    kind: str,
) -> Dict[str, str]:
    """Build the seven :ConsoleSearchable fields for one node.

    Args:
        name: node identifier name (from row-level field like form_name/cmd_name/...).
        props: node properties dict (Синоним/Заголовок/тип/...).
        kind: one of the keys in _SECTION_BY_KIND.

    Returns dict with:
        console_search_section
        console_search_name, console_search_synonym, console_search_type
        console_search_name_norm, console_search_synonym_norm, console_search_type_norm
    """
    section = _SECTION_BY_KIND.get(kind, "")

    raw_name = (name or "").strip()
    name_tokens = raw_name
    split = _split_camel(raw_name)
    if split and split != raw_name:
        name_tokens = f"{raw_name} {split}"

    synonym = _coalesce_props(props, ("Синоним", "Заголовок"))
    type_text = _coalesce_props(props, ("ТипФормы", "Действие", "module_type"))

    return {
        "console_search_section": section,
        "console_search_name": name_tokens,
        "console_search_synonym": synonym,
        "console_search_type": type_text,
        "console_search_name_norm": _norm(name_tokens),
        "console_search_synonym_norm": _norm(synonym),
        "console_search_type_norm": _norm(type_text),
    }


CONSOLE_SEARCH_KEYS: tuple[str, ...] = (
    "console_search_section",
    "console_search_name",
    "console_search_synonym",
    "console_search_type",
    "console_search_name_norm",
    "console_search_synonym_norm",
    "console_search_type_norm",
)
