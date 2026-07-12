"""
RLM (Routine Lexical Module) fallback for BSL code search.

Used when the vector path is not eligible (embeddings off, vector index missing,
vector_status != 'ready', vector_epoch != read_epoch, or vector cypher failed).

Pipeline (validated no-embedding production target):

    A. Base RLM scoring — sum of weighted, normalized FTS legs:
           search_fts      (weight 1.0)  — bsl_code_units_fts (chunk_search_text)
           module_fts      (weight 0.1)  — bsl_code_module_fts
           fuzzy_metadata  (weight 0.6)  — fuzzy match over symbol/object/form/
                                           metadata_type per candidate
       The reference winner also uses structural_fts (0.1) and module_local
       (0.05); they need an extra aggregate FTS table and a per-module lookup
       that are not yet ported — left for a follow-up since their combined
       contribution is bounded.

    B. Intent routing rerank — classify query into routes; for each, look up
       rerank weights for the 9 structural FTS tables; per matching table run
       BM25 restricted to the base candidate pool, min-max normalize, sum
       into routed_scores by `_ROUTE_WEIGHT`.

    C. Composite = base_score + base_weight*(1/rank) + routed contribution;
       top _RERANK_TOP_K is returned.

Window selection (window quotas, parent grouping) runs later in
BslCodeSearchService.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

from .bsl_code_scorers import (
    expand_tokens_with_1c_synonyms,
    fuzzy_token_key,
    tokenize,
)
from .bsl_code_sqlite import BslCodeSqlite, UnitMeta

logger = logging.getLogger(__name__)


# Intent-routing rule table. Each route is matched by `needles` substrings
# in the query; on match the route contributes `tables` weight to per-table
# rerank scores and `extra` tokens to the routed FTS query.
_ROUTES: Dict[str, Dict[str, object]] = {
    "query": {
        "needles": ("запрос", "выбира", "получа", "таблиц", "остат", "срез", "соединен"),
        "extra": ("запрос", "выбрать", "из", "где", "поместить", "соединение"),
        "tables": {
            "bsl_code_query_tables_fts": 0.45,
            "bsl_code_metadata_refs_fts": 0.12,
            "bsl_code_method_calls_fts": 0.06,
        },
    },
    "assignment": {
        "needles": ("заполня", "устанавли", "присва", "переда", "очища", "добавля", "изменя"),
        "extra": ("вставить", "установить", "добавить", "очистить", "заполнить"),
        "tables": {
            "bsl_code_assignments_fts": 0.42,
            "bsl_code_method_calls_fts": 0.12,
            "bsl_code_string_literals_fts": 0.05,
        },
    },
    "form": {
        "needles": ("форма", "форме", "выбор", "отбор", "команда", "открыва", "обработчик"),
        "extra": ("форма", "открытьформу", "получитьформу", "обработать", "выбор"),
        "tables": {
            "bsl_code_method_calls_fts": 0.30,
            "bsl_code_string_literals_fts": 0.10,
            "bsl_code_metadata_refs_fts": 0.06,
        },
    },
    "posting": {
        "needles": ("проведен", "проведение", "движени", "регистр", "запис", "набор запис"),
        "extra": ("движения", "добавить", "записать", "регистр", "наборзаписей"),
        "tables": {
            "bsl_code_metadata_refs_fts": 0.22,
            "bsl_code_method_calls_fts": 0.22,
            "bsl_code_assignments_fts": 0.10,
        },
    },
    "return": {
        "needles": ("возвраща", "получить список", "список", "результат"),
        "extra": ("возврат", "получить", "список", "результат"),
        "tables": {
            "bsl_code_method_calls_fts": 0.18,
            "bsl_code_assignments_fts": 0.06,
            "bsl_code_string_literals_fts": 0.05,
        },
    },
    "print": {
        "needles": ("печать", "печат", "макет", "табличный документ"),
        "extra": ("печать", "макет", "табличныйдокумент", "получитьмакет"),
        "tables": {
            "bsl_code_method_calls_fts": 0.24,
            "bsl_code_string_literals_fts": 0.12,
            "bsl_code_metadata_refs_fts": 0.05,
        },
    },
    "access": {
        "needles": ("доступ", "прав", "роль", "ограничен", "разреш"),
        "extra": ("доступ", "право", "роль", "разрешить", "ограничение"),
        "tables": {
            "bsl_code_string_literals_fts": 0.20,
            "bsl_code_method_calls_fts": 0.12,
            "bsl_code_metadata_refs_fts": 0.05,
        },
    },
    "exchange": {
        "needles": ("обмен", "синхрон", "отправ", "получ", "загруз", "выгруз", "эдо", "сэдо"),
        "extra": ("обмен", "отправить", "получить", "загрузить", "выгрузить", "эдо", "сэдо"),
        "tables": {
            "bsl_code_method_calls_fts": 0.22,
            "bsl_code_string_literals_fts": 0.12,
            "bsl_code_metadata_refs_fts": 0.05,
        },
    },
}

# Fallback structural weight is disabled in the production target. The
# entry stays for parity with the gated code path but contributes nothing.
_FALLBACK_TABLE = "bsl_code_module_fts"
_FALLBACK_WEIGHT = 0.0

# Validated production-target constants.
_BASE_WEIGHT = 1.0  # weight of the inverse-rank base score on top of leg sums
_ROUTE_WEIGHT = 0.4
_SOURCE_TOP_K = 100
_RERANK_TOP_K = 50
_FTS_PREFIX = True
_ROUTE_MAX_FTS_TERMS = 14

# Base RLM scoring weights.
_SEARCH_FTS_WEIGHT = 1.0
_MODULE_FTS_WEIGHT = 0.1
_MODULE_LOCAL_WEIGHT = 0.05
_STRUCTURAL_FTS_WEIGHT = 0.1
_FUZZY_METADATA_WEIGHT = 0.6

# Per-leg top-K for the source candidate pool union.
_SEARCH_FTS_TOP_K = 500
_MODULE_FTS_TOP_K = 20  # distinct modules at the module FTS leg
_MODULE_METADATA_SOURCE_TOP_K = 10  # top modules whose units enter the fuzzy pool
_MODULE_LOCAL_MODULE_TOP_K = 5
_MODULE_LOCAL_PER_MODULE_TOP_K = 3
_STRUCTURAL_FTS_TOP_K = 250
_METADATA_SOURCE_TOP_K = 120
_FUZZY_METADATA_THRESHOLD = 0.88
_FUZZY_METADATA_TOP_K = 200


def rlm_candidates(
    sqlite: BslCodeSqlite,
    scope: str,
    query: str,
    read_epoch: int,
    *,
    config_name: Optional[str] = None,
    module_type: Optional[str] = None,
    routine_type: Optional[str] = None,
    export: Optional[bool] = None,
    owner_categories: Optional[Sequence[str]] = None,
    excluded_owner_categories: Optional[Sequence[str]] = None,
    exclude_regulated_reports: bool = False,
    top_k: int = _RERANK_TOP_K,
) -> List[Tuple[UnitMeta, float]]:
    """
    Return ranked (UnitMeta, score) candidates from the RLM intent-routing
    pipeline. `score` is a higher-is-better composite of weighted base FTS
    legs plus inverse-rank base weight plus routed structural FTS rerank.

    Coverage policy:
    - `excluded_owner_categories` is the negative twin for `owner_categories`.
      When a positive scope is explicitly given, the negative twin is
      disabled — the positive list already purifies the search scope.
    - `exclude_regulated_reports` is an independent global filter (like
      routine_type/export). It is always applied when true, including
      together with an explicit `owner_categories`.
    Both negative filters land in the source SQL before `ORDER BY ... LIMIT`
    so the top-K pool is not poisoned by excluded rows.
    """
    original_tokens = tokenize(query)
    expanded_tokens = expand_tokens_with_1c_synonyms(
        original_tokens, weight=1, profile="platform",
    )
    fts_query_str = " ".join(expanded_tokens)

    positive_active = bool(owner_categories)
    neg_excluded: Tuple[str, ...] = (
        () if positive_active else tuple(excluded_owner_categories or ())
    )
    neg_regulated = bool(exclude_regulated_reports)

    # --- Base RLM legs ---------------------------------------------------
    # 1. search_fts (chunk_search_text BM25, weight 1.0).
    search_rows = sqlite.fts_bm25(
        scope=scope, query=fts_query_str, epoch=read_epoch,
        limit=_SEARCH_FTS_TOP_K,
        config_name=config_name,
        module_type=module_type,
        routine_type=routine_type,
        export=export,
        owner_categories=owner_categories,
        excluded_owner_categories=neg_excluded,
        exclude_regulated_reports=neg_regulated,
    )

    # 2. module_fts (module-level multi-column FTS, top distinct modules).
    # Module identity is rel_path so ObjectModule.bsl and ManagerModule.bsl of
    # the same metadata object remain separate. File-level MCP filters
    # (config_name, module_type, owner_categories) are applied BEFORE topK by
    # constraining the FTS search to eligible rel_paths. Routine-level filters
    # (routine_type, export) cannot be honored because the module aggregate
    # mixes routines of all types/export flags in one row; if either is
    # active we skip module_fts and module_local entirely to avoid scoring
    # by routine content that the filter would later drop.
    # Negative coverage filters are pushed through `eligible_rel_paths` so a
    # module with only excluded units is not ranked here either.
    module_filters_active = (
        _filters_active(config_name, module_type, None, None, owner_categories)
        or bool(neg_excluded) or neg_regulated
    )
    if _routine_level_filters_active(routine_type, export):
        module_rel_rows: List[Tuple[str, float]] = []
    elif module_filters_active:
        eligible_paths = sqlite.eligible_rel_paths(
            scope, read_epoch,
            config_name=config_name,
            module_type=module_type,
            owner_categories=owner_categories,
            excluded_owner_categories=neg_excluded,
            exclude_regulated_reports=neg_regulated,
        )
        if not eligible_paths:
            module_rel_rows = []
        else:
            module_rel_rows = sqlite.module_fts_search(
                scope=scope, query=fts_query_str, epoch=read_epoch,
                limit=_MODULE_FTS_TOP_K,
                rel_paths=eligible_paths,
            )
    else:
        module_rel_rows = sqlite.module_fts_search(
            scope=scope, query=fts_query_str, epoch=read_epoch,
            limit=_MODULE_FTS_TOP_K,
        )

    # 3. structural_fts (aggregated structural FTS, weight 0.1).
    structural_rows = sqlite.structural_bm25(
        scope=scope, table="bsl_code_structural_fts",
        query=fts_query_str, epoch=read_epoch,
        limit=_STRUCTURAL_FTS_TOP_K,
        config_name=config_name,
        module_type=module_type,
        routine_type=routine_type,
        export=export,
        owner_categories=owner_categories,
        excluded_owner_categories=neg_excluded,
        exclude_regulated_reports=neg_regulated,
    )

    # 4. module_local (weight 0.05): for top-K modules, restrict structural_fts
    # BM25 to the units under each module (per-module top-N). Skipped when
    # routine_type/export filters are active (see comment above module_fts).
    if _routine_level_filters_active(routine_type, export):
        module_local_rows: List[Tuple[str, float]] = []
    else:
        module_local_rows = _module_local_fts(
            sqlite, scope, read_epoch, fts_query_str,
            [rp for rp, _ in module_rel_rows],
            config_name=config_name,
            module_type=module_type,
            routine_type=routine_type,
            export=export,
            owner_categories=owner_categories,
            excluded_owner_categories=neg_excluded,
            exclude_regulated_reports=neg_regulated,
        )

    if not search_rows and not module_rel_rows and not structural_rows:
        return []

    # Expand module hits to a per-unit list (every unit under each top
    # rel_path gets the module's BM25 score).
    module_expanded_rows = _expand_module_paths_to_units(
        sqlite, scope, read_epoch, module_rel_rows,
        config_name=config_name,
        module_type=module_type,
        routine_type=routine_type,
        export=export,
        owner_categories=owner_categories,
        excluded_owner_categories=neg_excluded,
        exclude_regulated_reports=neg_regulated,
    )

    # Source candidate pool for fuzzy_metadata:
    # - union of top _METADATA_SOURCE_TOP_K hits per FTS leg
    #   (excluding module_expanded_rows which can be dominated by one module),
    # - PLUS all filtered units under the top _MODULE_METADATA_SOURCE_TOP_K
    #   module owner_qns (reference --module-metadata-source-top-k 10).
    candidate_pool: set = set()
    for rows in (search_rows, structural_rows, module_local_rows):
        for uid, _ in rows[:_METADATA_SOURCE_TOP_K]:
            candidate_pool.add(uid)
    for rel_path, _ in module_rel_rows[:_MODULE_METADATA_SOURCE_TOP_K]:
        sibling_ids = sqlite.units_by_rel_paths(scope, read_epoch, [rel_path])
        if not sibling_ids:
            continue
        candidate_pool.update(_filter_unit_ids(
            sqlite, scope, read_epoch, sibling_ids,
            config_name=config_name,
            module_type=module_type,
            routine_type=routine_type,
            export=export,
            owner_categories=owner_categories,
            excluded_owner_categories=neg_excluded,
            exclude_regulated_reports=neg_regulated,
        ))

    # 5. fuzzy_metadata (weight 0.6, in-memory iteration).
    fuzzy_rows = _fuzzy_metadata_rows(
        sqlite, scope, read_epoch, original_tokens, candidate_pool,
        _FUZZY_METADATA_TOP_K, _FUZZY_METADATA_THRESHOLD,
    )

    base_scores: Dict[str, float] = defaultdict(float)
    _add_weighted_normalized(base_scores, search_rows, _SEARCH_FTS_WEIGHT, invert=True)
    _add_weighted_normalized(base_scores, module_expanded_rows, _MODULE_FTS_WEIGHT, invert=True)
    _add_weighted_normalized(base_scores, structural_rows, _STRUCTURAL_FTS_WEIGHT, invert=True)
    _add_weighted_normalized(base_scores, module_local_rows, _MODULE_LOCAL_WEIGHT, invert=True)
    _add_weighted_normalized(base_scores, fuzzy_rows, _FUZZY_METADATA_WEIGHT, invert=False)

    if not base_scores:
        return []

    # Order candidates by base score; cap to _SOURCE_TOP_K for intent rerank.
    sorted_candidates = sorted(
        base_scores.items(), key=lambda item: item[1], reverse=True,
    )[:_SOURCE_TOP_K]
    candidates = [uid for uid, _ in sorted_candidates]
    base_rank = {uid: rank for rank, uid in enumerate(candidates, start=1)}

    # --- Intent routing rerank ------------------------------------------
    tags = _classify_routes(query)
    table_weights: Dict[str, float] = defaultdict(float)
    for tag in tags:
        route = _ROUTES[tag]
        for table, weight in route["tables"].items():  # type: ignore[union-attr]
            table_weights[table] += float(weight) * _ROUTE_WEIGHT
    if not table_weights and _FALLBACK_WEIGHT > 0:
        table_weights[_FALLBACK_TABLE] = _FALLBACK_WEIGHT

    routed_query = _routed_query_text(query, tags)

    routed_scores: Dict[str, float] = defaultdict(float)
    for table, weight in table_weights.items():
        per_table = sqlite.structural_bm25(
            scope=scope, table=table, query=routed_query,
            epoch=read_epoch, limit=len(candidates),
            unit_ids=candidates,
            prefix=_FTS_PREFIX,
            max_terms=_ROUTE_MAX_FTS_TERMS,
            config_name=config_name,
            module_type=module_type,
            routine_type=routine_type,
            export=export,
            owner_categories=owner_categories,
            excluded_owner_categories=neg_excluded,
            exclude_regulated_reports=neg_regulated,
        )
        normalized = _normalize_inverse(per_table)
        for uid, s in normalized.items():
            routed_scores[uid] += weight * s

    # Phase B composite uses only rank from the base result + routed score;
    # raw base_scores are NOT carried over so routed contribution can move
    # candidates as intended.
    composite: List[Tuple[str, float]] = []
    for uid in candidates:
        rank = base_rank[uid]
        score = _BASE_WEIGHT * (1.0 / rank) + routed_scores.get(uid, 0.0)
        composite.append((uid, score))
    composite.sort(key=lambda item: item[1], reverse=True)
    composite = composite[:top_k]

    metas = {
        m.unit_id: m
        for m in sqlite.fetch_unit_metadata([uid for uid, _ in composite], read_epoch)
    }
    out: List[Tuple[UnitMeta, float]] = []
    for uid, score in composite:
        meta = metas.get(uid)
        if meta is None:
            continue
        out.append((meta, score))
    return out


def _expand_module_paths_to_units(
    sqlite: BslCodeSqlite,
    scope: str,
    read_epoch: int,
    module_rows: List[Tuple[str, float]],
    *,
    config_name: Optional[str] = None,
    module_type: Optional[str] = None,
    routine_type: Optional[str] = None,
    export: Optional[bool] = None,
    owner_categories: Optional[Sequence[str]] = None,
    excluded_owner_categories: Optional[Sequence[str]] = None,
    exclude_regulated_reports: bool = False,
) -> List[Tuple[str, float]]:
    """
    Expand module-level FTS hits to every unit under each rel_path — mirrors
    reference add_module_component with --module-max-chunks 0. Returns rows
    sorted by raw BM25 (lower = better).
    """
    if not module_rows:
        return []
    score_by_path = {rp: float(raw) for rp, raw in module_rows}
    top_paths = list(score_by_path)

    sibling_ids = sqlite.units_by_rel_paths(scope, read_epoch, top_paths)
    if not sibling_ids:
        return []
    filtered = _filter_unit_ids(
        sqlite, scope, read_epoch, sibling_ids,
        config_name=config_name,
        module_type=module_type,
        routine_type=routine_type,
        export=export,
        owner_categories=owner_categories,
        excluded_owner_categories=excluded_owner_categories,
        exclude_regulated_reports=exclude_regulated_reports,
    )
    sibling_path = sqlite.rel_paths_for_units(filtered, read_epoch)
    expanded: Dict[str, float] = {}
    for sib_uid in filtered:
        rp = sibling_path.get(sib_uid, "")
        if not rp:
            continue
        score = score_by_path.get(rp)
        if score is None:
            continue
        prev = expanded.get(sib_uid)
        if prev is None or score < prev:
            expanded[sib_uid] = score
    return sorted(expanded.items(), key=lambda item: item[1])


def _filters_active(
    config_name: Optional[str],
    module_type: Optional[str],
    routine_type: Optional[str],
    export: Optional[bool],
    owner_categories: Optional[Sequence[str]],
) -> bool:
    return bool(config_name or module_type or routine_type or owner_categories) or (
        export is not None
    )


def _routine_level_filters_active(
    routine_type: Optional[str],
    export: Optional[bool],
) -> bool:
    """
    routine_type / export are per-routine filters. The module-level FTS
    aggregate mixes routines of all types/export flags in a single row, so
    these two filters cannot be applied at the module leg and module_local
    leg without false boosts.
    """
    return bool(routine_type) or (export is not None)


def _filter_unit_ids(
    sqlite: BslCodeSqlite,
    scope: str,
    read_epoch: int,
    unit_ids: Sequence[str],
    *,
    config_name: Optional[str] = None,
    module_type: Optional[str] = None,
    routine_type: Optional[str] = None,
    export: Optional[bool] = None,
    owner_categories: Optional[Sequence[str]] = None,
    excluded_owner_categories: Optional[Sequence[str]] = None,
    exclude_regulated_reports: bool = False,
) -> List[str]:
    """
    Return the subset of `unit_ids` that pass the MCP filters and the
    coverage policy. When no filter is active, returns the input unchanged.

    This is a post-topK guard for expansion / local routes — primary
    coverage filtering lives in the source SQL of fts_bm25 /
    structural_bm25 / eligible_rel_paths.
    """
    if not unit_ids:
        return []
    any_filter = (
        bool(config_name) or bool(module_type) or bool(routine_type)
        or bool(owner_categories) or bool(excluded_owner_categories)
        or exclude_regulated_reports or (export is not None)
    )
    if not any_filter:
        return list(unit_ids)
    metas = sqlite.fetch_unit_metadata(unit_ids, read_epoch)
    out: List[str] = []
    cat_set = set(owner_categories) if owner_categories else None
    excluded_set = (
        set(excluded_owner_categories) if excluded_owner_categories else None
    )
    for m in metas:
        if config_name and m.config_name != config_name:
            continue
        if module_type and m.module_type != module_type:
            continue
        if routine_type and m.routine_type != routine_type:
            continue
        if export is not None and bool(m.export) != bool(export):
            continue
        if cat_set is not None and m.owner_category not in cat_set:
            continue
        if excluded_set is not None and m.owner_category in excluded_set:
            continue
        if exclude_regulated_reports and getattr(m, "is_regulated_report", False):
            continue
        out.append(m.unit_id)
    return out


def _module_local_fts(
    sqlite: BslCodeSqlite,
    scope: str,
    read_epoch: int,
    fts_query_str: str,
    module_rel_paths: Sequence[str],
    *,
    config_name: Optional[str] = None,
    module_type: Optional[str] = None,
    routine_type: Optional[str] = None,
    export: Optional[bool] = None,
    owner_categories: Optional[Sequence[str]] = None,
    excluded_owner_categories: Optional[Sequence[str]] = None,
    exclude_regulated_reports: bool = False,
) -> List[Tuple[str, float]]:
    """
    Take the top-K module rel_paths, restrict structural_fts BM25 to the
    units under each rel_path (per-module top-N), and return the best score
    per chunk_id across the modules.
    """
    if not module_rel_paths:
        return []
    top_paths = list(module_rel_paths)[:_MODULE_LOCAL_MODULE_TOP_K]

    best_by_unit: Dict[str, float] = {}
    for rel_path in top_paths:
        sibling_ids = sqlite.units_by_rel_paths(scope, read_epoch, [rel_path])
        if not sibling_ids:
            continue
        sibling_ids = _filter_unit_ids(
            sqlite, scope, read_epoch, sibling_ids,
            config_name=config_name,
            module_type=module_type,
            routine_type=routine_type,
            export=export,
            owner_categories=owner_categories,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )
        if not sibling_ids:
            continue
        rows = sqlite.structural_bm25(
            scope=scope, table="bsl_code_structural_fts",
            query=fts_query_str, epoch=read_epoch,
            limit=_MODULE_LOCAL_PER_MODULE_TOP_K,
            unit_ids=sibling_ids,
            excluded_owner_categories=excluded_owner_categories,
            exclude_regulated_reports=exclude_regulated_reports,
        )
        for uid, raw in rows:
            prev = best_by_unit.get(uid)
            if prev is None or float(raw) < float(prev):  # BM25: lower is better
                best_by_unit[uid] = float(raw)
    return sorted(best_by_unit.items(), key=lambda item: item[1])


def _fuzzy_metadata_rows(
    sqlite: BslCodeSqlite,
    scope: str,
    read_epoch: int,
    original_tokens: Sequence[str],
    candidate_ids: set,
    limit: int,
    threshold: float,
) -> List[Tuple[str, float]]:
    """
    For every candidate unit, compute a weighted fuzzy match against the
    symbol/object/form/path metadata field tokens; keep `limit` best.
    """
    if not candidate_ids or not original_tokens:
        return []
    query_keys = [fuzzy_token_key(t) for t in original_tokens]
    field_map = sqlite.field_lookup(list(candidate_ids), read_epoch)
    scored: List[Tuple[str, float]] = []
    for uid, fields in field_map.items():
        score = _fuzzy_metadata_score(query_keys, fields, threshold)
        if score > 0:
            scored.append((uid, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def _fuzzy_metadata_score(
    query_keys: Sequence[str],
    fields: Dict[str, str],
    threshold: float,
) -> float:
    """
    Weighted fuzzy field overlap. Production weights — object 2.2, form 1.4,
    symbol 0.4, path 0.4. metadata_type is intentionally NOT used.
    """
    if not query_keys:
        return 0.0
    object_tokens = tokenize(fields.get("object", ""))
    form_tokens = tokenize(fields.get("form", ""))
    symbol_tokens = tokenize(fields.get("symbol", ""))
    path_tokens = tokenize(fields.get("path", ""))
    return (
        2.2 * _fuzzy_field_score(query_keys, object_tokens, threshold)
        + 1.4 * _fuzzy_field_score(query_keys, form_tokens, threshold)
        + 0.4 * _fuzzy_field_score(query_keys, symbol_tokens, threshold)
        + 0.4 * _fuzzy_field_score(query_keys, path_tokens, threshold)
    )


def _fuzzy_field_score(
    query_keys: Sequence[str],
    field_tokens: Sequence[str],
    threshold: float,
) -> float:
    if not field_tokens:
        return 0.0
    field_keys = [fuzzy_token_key(t) for t in field_tokens]
    matched = 0.0
    for field_key in field_keys:
        best = max(
            _fuzzy_token_similarity(field_key, query_key)
            for query_key in query_keys
        )
        if best >= threshold:
            matched += best
    return matched / len(field_keys)


def _fuzzy_token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left.startswith(right) or right.startswith(left):
        return 0.94
    return SequenceMatcher(None, left, right).ratio()


def _add_weighted_normalized(
    accumulator: Dict[str, float],
    rows: List[Tuple[str, float]],
    weight: float,
    *,
    invert: bool,
) -> None:
    """
    Add `weight * normalized(row)` into accumulator. When `invert=True` the
    raw rows are SQLite FTS5 BM25 (lower-is-better); we flip the sign before
    normalising to keep "higher-is-better" semantics consistent across legs.

    Single-hit or all-equal rows get normalized score 1.0 so the leg still
    contributes its full weight (matching the reference normalization).
    """
    if weight <= 0 or not rows:
        return
    if invert:
        rows = [(uid, -float(s)) for uid, s in rows]
    values = [v for _, v in rows]
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        for uid, _ in rows:
            accumulator[uid] += weight * 1.0
        return
    for uid, v in rows:
        accumulator[uid] += weight * ((v - lo) / (hi - lo))


def _classify_routes(query: str) -> List[str]:
    q = (query or "").casefold()
    tags: List[str] = []
    for tag, rule in _ROUTES.items():
        if any(needle in q for needle in rule["needles"]):  # type: ignore[union-attr]
            tags.append(tag)
    return tags


def _routed_query_text(query: str, tags: Sequence[str]) -> str:
    """
    Build the FTS query string used for all routed tables. Uses the base
    tokenize() (NOT 1c_light) so BSL terms like "возврат" survive in the
    FTS query — route extras intentionally include them.
    """
    base_tokens = expand_tokens_with_1c_synonyms(
        tokenize(query), weight=1, profile="platform",
    )
    extra_tokens: List[str] = []
    for tag in tags:
        for extra in _ROUTES[tag]["extra"]:  # type: ignore[union-attr]
            extra_tokens.extend(tokenize(str(extra)))
    return " ".join(base_tokens + extra_tokens)


def _normalize_inverse(rows: List[Tuple[str, float]]) -> Dict[str, float]:
    """
    FTS5 BM25 is lower-is-better. Convert to higher-is-better and min-max
    normalize across rows. Single-hit or all-equal rows get score 1.0
    (single-hit / all-equal rows fall back to score 1.0).
    """
    if not rows:
        return {}
    inverted = [(uid, -float(s)) for uid, s in rows]
    values = [v for _, v in inverted]
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        return {uid: 1.0 for uid, _ in inverted}
    return {uid: (v - lo) / (hi - lo) for uid, v in inverted}
