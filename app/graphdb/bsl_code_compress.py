"""
Lexical-dedup compressor for BSL code embedding (optional pipeline).

Activated when settings.bsl_code_compression_strategy != "none". The compressor
takes a raw retrieval unit and produces a shorter text fit for embedding.

The result text is used ONLY for the vector embedding leg — search-side BM25,
field scoring, RLM and fragment slicing always work on raw text in SQLite +
Neo4j.Routine.body.

Supported strategies (1:1 with the validated reference compressor):

    lexdedup_terms_cap1_lines_normprefix
        include_chains=false, include_terms=true, drop_keywords=true,
        repeat_cap=1, layout=source-lines, selection_mode=shorter,
        normalize_prefix=true, max_line_chars=1000.

    lexdedup_cap1_nochainparts_lines_normprefix
        include_chains=true, include_terms=true, drop_keywords=true,
        repeat_cap=1, drop_chain_parts_from_terms=true,
        layout=source-lines, selection_mode=shorter,
        normalize_prefix=true, max_line_chars=1000.

Two threshold variants keep short units raw and dedup only the long ones — units
whose raw body is shorter than raw_below_chars go into the embedding as raw text
(same path as "none"), while longer units are deduped by the wrapped method:

    rawbelow1000_lexdedup_terms_cap1_lines_normprefix
        raw_below_chars=1000 over lexdedup_terms_cap1_lines_normprefix.

    rawbelow1000_lexdedup_cap1_nochainparts_lines_normprefix
        raw_below_chars=1000 over lexdedup_cap1_nochainparts_lines_normprefix.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional

from .bsl_code_embed_text import UnitContext, build_raw_embedding_text

_IDENT = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*"
_CHAIN_RE = re.compile(rf"{_IDENT}(?:\s*\.\s*{_IDENT})+")
_TOKEN_RE = re.compile(_IDENT)

_SIGNATURE_FIRST_LINE_RE = re.compile(r"^\s*(Процедура|Функция)\s", re.IGNORECASE)

_MAX_LINE_CHARS_DEFAULT = 1000

# Raw-below threshold: units whose raw body is shorter than this go into the
# embedding as raw text; only longer units are deduped. Baked into the strategy
# name (rawbelow1000_*), not a separate env var.
_RAW_BELOW_THRESHOLD_1000 = 1000

# BSL control-flow / declaration keywords that are stripped from the embedding
# text so identifier dedup is not flooded with structural noise.
_BSL_KEYWORDS: frozenset[str] = frozenset({
    "Если", "Тогда", "Иначе", "ИначеЕсли", "КонецЕсли",
    "Для", "Каждого", "Из", "Цикл", "КонецЦикла", "Пока",
    "Попытка", "Исключение", "КонецПопытки",
    "Возврат", "Продолжить", "Прервать",
    "Процедура", "Функция", "КонецПроцедуры", "КонецФункции",
    "Экспорт", "Новый",
    "Неопределено", "Истина", "Ложь", "NULL",
    "И", "ИЛИ", "НЕ", "AND", "OR", "NOT",
})


_STRATEGY_KNOWN = frozenset({
    "none",
    "lexdedup_terms_cap1_lines_normprefix",
    "lexdedup_cap1_nochainparts_lines_normprefix",
    "rawbelow1000_lexdedup_terms_cap1_lines_normprefix",
    "rawbelow1000_lexdedup_cap1_nochainparts_lines_normprefix",
})


@dataclass(frozen=True)
class _CompressArgs:
    repeat_cap: int
    include_chains: bool
    include_terms: bool
    drop_keywords: bool
    drop_chain_parts_from_terms: bool
    max_line_chars: int
    raw_below_chars: int = 0  # 0 = no threshold; units below this stay raw


_STRATEGY_ARGS: dict[str, _CompressArgs] = {
    "lexdedup_terms_cap1_lines_normprefix": _CompressArgs(
        repeat_cap=1,
        include_chains=False,
        include_terms=True,
        drop_keywords=True,
        drop_chain_parts_from_terms=False,
        max_line_chars=_MAX_LINE_CHARS_DEFAULT,
        raw_below_chars=0,
    ),
    "lexdedup_cap1_nochainparts_lines_normprefix": _CompressArgs(
        repeat_cap=1,
        include_chains=True,
        include_terms=True,
        drop_keywords=True,
        drop_chain_parts_from_terms=True,
        max_line_chars=_MAX_LINE_CHARS_DEFAULT,
        raw_below_chars=0,
    ),
    "rawbelow1000_lexdedup_terms_cap1_lines_normprefix": _CompressArgs(
        repeat_cap=1,
        include_chains=False,
        include_terms=True,
        drop_keywords=True,
        drop_chain_parts_from_terms=False,
        max_line_chars=_MAX_LINE_CHARS_DEFAULT,
        raw_below_chars=_RAW_BELOW_THRESHOLD_1000,
    ),
    "rawbelow1000_lexdedup_cap1_nochainparts_lines_normprefix": _CompressArgs(
        repeat_cap=1,
        include_chains=True,
        include_terms=True,
        drop_keywords=True,
        drop_chain_parts_from_terms=True,
        max_line_chars=_MAX_LINE_CHARS_DEFAULT,
        raw_below_chars=_RAW_BELOW_THRESHOLD_1000,
    ),
}


def is_compression_enabled() -> bool:
    from config import settings
    s = (settings.bsl_code_compression_strategy or "none").strip().lower()
    return s != "" and s != "none"


def _starts_with_bsl_signature(text: str) -> bool:
    for line in text.splitlines():
        if not line.strip():
            continue
        return bool(_SIGNATURE_FIRST_LINE_RE.match(line))
    return False


def compressed_prefix(ctx: UnitContext, body: str) -> str:
    """
    Build the normalized compressed prefix:

        //Объект: <type>.<obj>
        //Форма: <type>.<obj>.<form>
        //Процедура: <symbol>   (for routine_type=="procedure")
        //Функция: <symbol>     (for routine_type=="function")

    The symbol line is skipped when the body already opens with the routine
    signature (`Процедура X` / `Функция X`).
    """
    lines: List[str] = []
    meta = (ctx.metadata_type_ru or "").strip()
    obj = (ctx.object_name or "").strip()
    form = (ctx.form_name or "").strip()
    sym = (ctx.symbol_name or "").strip()
    routine_type = (ctx.routine_type or "").strip().lower()

    if meta and obj:
        object_ref = f"{meta}.{obj}"
        if form:
            lines.append(f"//Форма: {object_ref}.{form}")
        else:
            lines.append(f"//Объект: {object_ref}")

    if sym and not _starts_with_bsl_signature(body):
        if routine_type == "function":
            lines.append(f"//Функция: {sym}")
        else:
            lines.append(f"//Процедура: {sym}")

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def compress_unit(body: str, ctx: UnitContext, strategy: Optional[str] = None) -> str:
    """
    Apply lexical dedup to `body` and prepend the normalized compressed prefix.
    `strategy` falls back to settings.bsl_code_compression_strategy.

    On selection_mode=shorter fallback, returns compressed_prefix + body (the
    same prefix shape as the lexical-dedup branch, NOT raw metadata-context).
    """
    from config import settings
    eff = (strategy or settings.bsl_code_compression_strategy or "none").strip().lower()
    if eff not in _STRATEGY_KNOWN:
        raise ValueError(
            f"Unknown BSL_CODE_COMPRESSION_STRATEGY: {strategy!r}. "
            "Allowed: none, lexdedup_terms_cap1_lines_normprefix, "
            "lexdedup_cap1_nochainparts_lines_normprefix, "
            "rawbelow1000_lexdedup_terms_cap1_lines_normprefix, "
            "rawbelow1000_lexdedup_cap1_nochainparts_lines_normprefix."
        )

    if eff == "none":
        from .bsl_code_embed_text import metadata_context_prefix
        return metadata_context_prefix(body or "", ctx) + (body or "")

    args = _STRATEGY_ARGS[eff]

    # Raw-below threshold: short units go into the embedding as raw text (same
    # path/prefix as "none"); only longer units are deduped below.
    if args.raw_below_chars > 0 and len(body or "") < args.raw_below_chars:
        return build_raw_embedding_text(body or "", ctx)

    prefix = compressed_prefix(ctx, body or "")
    body_text = body or ""

    compact = _extract_lexical_text_source_lines(body_text, prefix, args)
    fallback_text = prefix + body_text if prefix else body_text

    if len(compact) >= len(fallback_text):
        return fallback_text
    return compact


# ---------- internal helpers (port of reference source-lines layout) ----------


def _normalize_chain(value: str) -> str:
    return re.sub(r"\s*\.\s*", ".", value.strip())


def _split_long_line(items: List[str], max_line_chars: int) -> List[str]:
    if max_line_chars <= 0:
        return [" ".join(items)] if items else []
    lines: List[str] = []
    current: List[str] = []
    current_len = 0
    for item in items:
        add_len = len(item) + (1 if current else 0)
        if current and current_len + add_len > max_line_chars:
            lines.append(" ".join(current))
            current = [item]
            current_len = len(item)
        else:
            current.append(item)
            current_len += add_len
    if current:
        lines.append(" ".join(current))
    return lines


def _line_items(
    line: str,
    args: _CompressArgs,
    global_chain_parts: Optional[set],
) -> List[str]:
    all_chain_matches = list(_CHAIN_RE.finditer(line))
    chains_all = [_normalize_chain(m.group(0)) for m in all_chain_matches]
    tokens_all = _TOKEN_RE.findall(line)

    # Build the term source set, mirroring reference filter_terms.
    chain_parts: set = set()
    if global_chain_parts is not None:
        chain_parts = global_chain_parts
    else:
        for chain in chains_all:
            chain_parts.update(p for p in chain.split(".") if p)

    if args.drop_keywords:
        tokens = [t for t in tokens_all if t not in _BSL_KEYWORDS]
    else:
        tokens = list(tokens_all)

    if args.drop_chain_parts_from_terms:
        terms_source = [t for t in tokens if t not in chain_parts]
    else:
        terms_source = tokens

    term_allowed = Counter(terms_source)

    items: List[tuple[int, int, str]] = []

    if args.include_chains:
        for m in all_chain_matches:
            items.append((m.start(), 0, _normalize_chain(m.group(0))))

    if args.include_terms:
        for m in _TOKEN_RE.finditer(line):
            token = m.group(0)
            if term_allowed[token] <= 0:
                continue
            term_allowed[token] -= 1
            items.append((m.start(), 1, token))

    items.sort(key=lambda item: (item[0], item[1], item[2]))
    return [it[2] for it in items]


def _extract_lexical_text_source_lines(
    body: str,
    prefix: str,
    args: _CompressArgs,
) -> str:
    """
    Source-lines layout: iterate body line by line, emit chains and tokens
    in source order, cap per-token repeats across the whole body at
    args.repeat_cap. Empty lines are dropped.
    """
    lines: List[str] = []
    if prefix:
        lines.append(prefix.rstrip())

    chain_counts: Counter = Counter()
    term_counts: Counter = Counter()

    global_chain_parts: Optional[set] = None
    if args.drop_chain_parts_from_terms:
        global_chain_parts = set()
        for m in _CHAIN_RE.finditer(body):
            for part in _normalize_chain(m.group(0)).split("."):
                if part:
                    global_chain_parts.add(part)

    for source_line in body.splitlines():
        items = _line_items(source_line, args, global_chain_parts)
        line_kept: List[str] = []
        for item in items:
            if "." in item:
                if chain_counts[item] >= args.repeat_cap:
                    continue
                chain_counts[item] += 1
            else:
                if term_counts[item] >= args.repeat_cap:
                    continue
                term_counts[item] += 1
            line_kept.append(item)
        if not line_kept:
            continue
        lines.extend(_split_long_line(line_kept, args.max_line_chars))

    return "\n".join(line for line in lines if line.strip())
