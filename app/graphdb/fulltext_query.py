"""Lucene-safe input helpers for Neo4j fulltext queries.

MCP `search_text` is treated as plain text, not as Lucene DSL. This module
produces sanitized query candidates for `db.index.fulltext.queryNodes` and
classifies Lucene parse errors so the calling service can retry with a safer
candidate or fall back gracefully.
"""

from __future__ import annotations

import re
from typing import List


_LUCENE_RESERVED_CHARS = set('+-!(){}[]^"~*?:\\/')
_LUCENE_BOOLEAN_WORDS = {"AND", "OR", "NOT", "TO"}

_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")

_PARSE_ERROR_MARKERS = (
    "lucene",
    "queryparser",
    "parseexception",
    "cannot parse",
    "encountered",
    "was expecting",
)


def _escape_token(token: str) -> str:
    out: List[str] = []
    for ch in token:
        if ch in _LUCENE_RESERVED_CHARS or ch == "&" or ch == "|":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _build_escaped_candidate(raw: str) -> str:
    parts: List[str] = []
    for tok in raw.split():
        # Lucene boolean operators are case-sensitive uppercase; lowercasing
        # neutralizes them as literal terms while keeping the original word.
        if tok.upper() in _LUCENE_BOOLEAN_WORDS and tok.isalpha():
            parts.append(_escape_token(tok.lower()))
            continue
        esc = _escape_token(tok)
        if esc:
            parts.append(esc)
    return " ".join(parts).strip()


def _build_plain_candidate(raw: str) -> str:
    parts: List[str] = []
    for tok in _TOKEN_RE.findall(raw):
        if tok.upper() in _LUCENE_BOOLEAN_WORDS and tok.isalpha():
            parts.append(tok.lower())
        else:
            parts.append(tok)
    return " ".join(parts).strip()


def build_fulltext_query_candidates(raw: str | None) -> List[str]:
    """Return Lucene-safe candidates for `db.index.fulltext.queryNodes`.

    Order: escaped form first, plain-token fallback second. Returns an empty
    list when `raw` has no usable tokens after stripping/sanitizing.
    """
    if raw is None:
        return []
    text = raw.strip()
    if not text:
        return []

    candidates: List[str] = []

    escaped = _build_escaped_candidate(text)
    if escaped:
        candidates.append(escaped)

    plain = _build_plain_candidate(text)
    if plain and plain not in candidates:
        candidates.append(plain)

    return candidates


def is_lucene_fulltext_parse_error(exc: BaseException) -> bool:
    """Heuristic classifier for Lucene QueryParser errors from fulltext procs.

    True only when the error carries the `ProcedureCallFailed` Neo4j code and
    the message contains a Lucene/QueryParser marker. Duck-typed so the helper
    has no hard dependency on the `neo4j` package.
    """
    code = getattr(exc, "code", "") or ""
    if code != "Neo.ClientError.Procedure.ProcedureCallFailed":
        return False
    msg = (str(exc) or "").lower()
    return any(marker in msg for marker in _PARSE_ERROR_MARKERS)
