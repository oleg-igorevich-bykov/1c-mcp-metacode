"""
BSL-specific rerank document builder for `search_bsl_code`.

The HTTP transport, retry loop, response parsing and singleton live in the
shared module `graphdb.reranker`. This module keeps only the BSL-specific
document format: normalized `//...` metadata prefix + raw code unit text.

Body must be passed in already sliced by the caller (char_start/char_end with
line fallback); the reranker never sees full parent routine for split-routine
candidates and never sees compressed embedding text.
"""
from __future__ import annotations

import re
from typing import List


def build_rerank_document(
    *,
    metadata_type_ru: str = "",
    object_name: str = "",
    form_name: str = "",
    routine_type: str,
    routine_name: str,
    body_text: str,
) -> str:
    """
    Build the document string sent to the reranker for one BSL candidate.

    Body must already be sliced to the candidate's code unit by the caller —
    we never reconstruct from full parent routine here.
    """
    body = body_text or ""
    if _already_has_metadata_context(body):
        return body

    prefix_lines: List[str] = []
    meta_type = (metadata_type_ru or "").strip()
    obj = (object_name or "").strip()
    form = (form_name or "").strip()
    if meta_type and obj:
        object_ref = f"{meta_type}.{obj}"
        prefix_lines.append(f"//{object_ref}.{form}" if form else f"//{object_ref}")

    symbol_name = (routine_name or "").strip()
    if symbol_name and not _starts_with_symbol_signature(body, routine_type, symbol_name):
        prefix_lines.append(f"//{symbol_name}")

    if not prefix_lines:
        return body
    return "\n".join(prefix_lines) + "\n" + body


def _already_has_metadata_context(text: str) -> bool:
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    if not first_line.startswith("//"):
        return False
    return not first_line.startswith((
        "//ПроцедураФункция:",
        "//Объект:",
        "//Форма:",
        "//Процедура:",
        "//Функция:",
    ))


def _starts_with_symbol_signature(text: str, kind: str, symbol_name: str) -> bool:
    if not symbol_name:
        return False
    labels = ["Процедура", "Функция"]
    kind_l = (kind or "").strip().lower()
    if kind_l in ("procedure", "процедура"):
        labels = ["Процедура"]
    elif kind_l in ("function", "функция"):
        labels = ["Функция"]
    label_expr = "|".join(re.escape(label) for label in labels)
    pattern = rf"^\s*(?:{label_expr})\s+{re.escape(symbol_name)}\b"
    return re.search(pattern, text or "", re.I) is not None
