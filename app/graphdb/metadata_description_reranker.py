"""
Metadata description rerank document builder for `find_metadata_objects(search_by="description")`.

HTTP transport, retry loop and singleton live in `graphdb.reranker`. This
module owns only the metadata-description-specific document format: object
header plus four description fields exposed by the fulltext/vector cypher
(`synonym`, `comment`, `help_text`, `explanation`).
"""
from __future__ import annotations

from typing import Any, Dict


def build_metadata_description_rerank_document(row: Dict[str, Any]) -> str:
    """
    Build the document string for one metadata description candidate.

    Format:
        <category>.<name>
        Синоним: <synonym>
        Комментарий: <comment>
        Справка: <help_text>
        Пояснение: <explanation>

    Empty fields are skipped (no `Синоним: ` line if synonym is blank).
    Header alone (`<category>.<name>`) does NOT count as meaningful text:
    if all four description fields are blank, returns "" so the caller
    keeps the candidate in the tail with its hybrid score.
    """
    body_lines = []
    for label, key in (
        ("Синоним", "synonym"),
        ("Комментарий", "comment"),
        ("Справка", "help_text"),
        ("Пояснение", "explanation"),
    ):
        value = _clean(row.get(key))
        if value:
            body_lines.append(f"{label}: {value}")

    if not body_lines:
        return ""

    header = _build_header(row.get("category"), row.get("name"))
    lines = []
    if header:
        lines.append(header)
    lines.extend(body_lines)
    return "\n".join(lines)


def _build_header(category: Any, name: Any) -> str:
    cat = _clean(category)
    nm = _clean(name)
    if cat and nm:
        return f"{cat}.{nm}"
    return cat or nm


def _clean(value: Any) -> str:
    return str(value or "").strip()
