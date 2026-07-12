"""
Routine description rerank document builder for `search_bsl_routines(mode="description")`.

HTTP transport, retry loop and singleton live in `graphdb.reranker`. This
module owns only the routine-description-specific document format: owner
header, signature, and the three doc-* blocks. BSL body is deliberately
NOT included — that is the scope of `search_bsl_code`.
"""
from __future__ import annotations

from typing import Any, Dict


def build_routine_description_rerank_document(row: Dict[str, Any]) -> str:
    """
    Build the document string for one routine description candidate.

    Format:
        <owner> / <module_type>
        <signature>
        Описание: <doc_description>
        Параметры: <doc_params_text>
        Возвращает: <doc_return_text>

    Empty fields are skipped (no `Описание: ` line if doc_description is blank).
    Returns "" when there is nothing meaningful for the reranker: neither
    signature nor any doc_* field. Signature alone is enough to keep the
    candidate (parameter names and types carry signal). Owner header alone
    is NOT — caller routes such candidates to the tail.
    """
    body_lines = []

    signature = _clean(row.get("signature"))
    if signature:
        body_lines.append(signature)

    for label, key in (
        ("Описание", "doc_description"),
        ("Параметры", "doc_params_text"),
        ("Возвращает", "doc_return_text"),
    ):
        value = _clean(row.get(key))
        if value:
            body_lines.append(f"{label}: {value}")

    if not body_lines:
        return ""

    header = _build_header(row.get("owner"), row.get("module_type"))
    lines = []
    if header:
        lines.append(header)
    lines.extend(body_lines)
    return "\n".join(lines)


def _build_header(owner: Any, module_type: Any) -> str:
    own = _clean(owner)
    mod = _clean(module_type)
    if own and mod:
        return f"{own} / {mod}"
    return own or mod


def _clean(value: Any) -> str:
    return str(value or "").strip()
