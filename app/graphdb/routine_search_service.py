"""
Service for searching routines by description using fulltext, vector, or hybrid search.

This service provides three search modes:
1. Fulltext-only: Uses ftx_routine_doc_description index
2. Vector-only: Uses vec_routine_doc_description index with embeddings
3. Hybrid: Combines fulltext + vector with weighted scoring

All search modes use canonical Cypher queries from routine_description_queries.py
to ensure consistency and avoid code duplication.
"""
from __future__ import annotations

import logging
import re
from typing import List, Dict, Any, Optional

from neo4j.exceptions import Neo4jError

from config import settings
from graphdb.routine_description_queries import (
    ROUTINE_DESCRIPTION_FULLTEXT_CYPHER,
    ROUTINE_DESCRIPTION_VECTOR_CYPHER,
    build_routine_description_search_cypher,
)
from graphdb.routine_description_reranker import build_routine_description_rerank_document
from graphdb.reranker import get_reranker
from graphdb.category_canon import canon_categories
from graphdb import embedding_usage_metrics as embedding_metrics
from graphdb.embedding_text_format import build_embedding_format_spec
from graphdb.fulltext_query import (
    build_fulltext_query_candidates,
    is_lucene_fulltext_parse_error,
)
from graphdb.vector_search_common import (
    compute_adaptive_min_sim,
    compute_per_leg_k,
    is_vector_search_capability_or_schema_error,
    _VECTOR_MODE,
)

logger = logging.getLogger(__name__)

_INDEX_KEY = "vec_routine_doc_description"


_routine_description_rerank_warned_unavailable: bool = False


def _warn_routine_description_rerank_unavailable_once() -> None:
    """One-shot misconfiguration warning."""
    global _routine_description_rerank_warned_unavailable
    if _routine_description_rerank_warned_unavailable:
        return
    _routine_description_rerank_warned_unavailable = True
    logger.warning(
        "routine description rerank requested (ROUTINE_DESCRIPTION_RERANK_ENABLED=true) but shared "
        "reranker unavailable: RERANK_API_KEY missing or invalid"
    )


def reset_routine_description_rerank_warning() -> None:
    """Test hook: reset the one-shot misconfig warning."""
    global _routine_description_rerank_warned_unavailable
    _routine_description_rerank_warned_unavailable = False

# Import audit_block for detailed logging to audit log file
try:
    from mcpsrv.audit import audit_block
    AUDIT_AVAILABLE = True
except ImportError:
    AUDIT_AVAILABLE = False
    audit_block = None


class RoutineSearchService:
    """Service for searching routines by description"""

    def __init__(self, driver, embedding_service=None, request_id=None):
        """
        Initialize routine search service.

        Args:
            driver: Neo4j driver instance
            embedding_service: Optional EmbeddingService instance for vector search
            request_id: Optional request ID for audit logging
        """
        self.driver = driver
        self.embedding_service = embedding_service
        self.request_id = request_id

    def search_by_description_fulltext(
        self,
        text: str,
        limit: int = None,
        offset: int = 0,
        min_score: float = 0.1,
        owner_qn: str = None,
        owner_qn_prefix: str = None,
        routine_type: str = None,
        export: bool = None,
        is_ssl_api: bool = None,
        name: str = None,
        directive: str = None,
        config_name: str = None,
        project_name: str = None,
        owner_categories: Optional[List[str]] = None,
        module_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search routines by description using fulltext index.

        Args:
            text: Fulltext search query
            limit: Maximum number of results (default: routine_description_search_default_limit)
            offset: Skip first N results (for pagination)
            min_score: Minimum fulltext score threshold
            owner_qn: Filter by owner qualified name (exact match)
            owner_qn_prefix: Filter by owner qualified name prefix (STARTS WITH match)
            routine_type: Filter by routine type ('Procedure' or 'Function')
            export: Filter by export flag
            is_ssl_api: Filter by SSL/BSP flag
            name: Substring filter on routine name
            directive: Substring filter on directive

        Returns:
            List of routine dictionaries with fields: id, name, owner, module_type, form_name,
            owner_qn, signature, directives, doc_description, doc_params_text, doc_return_text, score
        """
        if limit is None:
            limit = settings.routine_description_search_default_limit

        candidates = build_fulltext_query_candidates(text)
        if not candidates:
            return []

        pn = project_name or settings.project_name
        params = {
            'limit': limit,
            'offset': offset,
            'min_score': min_score,
            'owner_qn': owner_qn if isinstance(owner_qn, str) and owner_qn.strip() else None,
            'owner_qn_prefix': owner_qn_prefix if isinstance(owner_qn_prefix, str) and owner_qn_prefix.strip() else None,
            'routine_type': routine_type if isinstance(routine_type, str) and routine_type.strip() else None,
            'export': export if isinstance(export, bool) else None,
            'is_ssl_api': is_ssl_api if isinstance(is_ssl_api, bool) else None,
            'name': name if isinstance(name, str) and name.strip() else None,
            'directive': directive if isinstance(directive, str) and directive.strip() else None,
            'project_name': pn,
            'project_prefix': pn + "/",
            'config_name': config_name if isinstance(config_name, str) and config_name.strip() else None,
            'module_type': module_type if isinstance(module_type, str) and module_type.strip() else None,
            'owner_categories': list(owner_categories) if owner_categories else [],
        }

        last_parse_error: Optional[BaseException] = None
        with self.driver.session(database=settings.neo4j_database) as session:
            for candidate in candidates:
                attempt_params = dict(params)
                attempt_params['text'] = candidate
                try:
                    result = session.run(ROUTINE_DESCRIPTION_FULLTEXT_CYPHER, attempt_params)
                    records = [dict(record) for record in result]
                except Neo4jError as e:
                    if is_lucene_fulltext_parse_error(e):
                        logger.warning(
                            "Lucene parse error on routine fulltext candidate %r: %s",
                            candidate, e,
                        )
                        last_parse_error = e
                        continue
                    raise
                logger.debug(f"Fulltext search returned {len(records)} routines for query: {text}")
                return records

        logger.warning(
            "All routine fulltext candidates failed Lucene parse for query %r (last error: %s)",
            text, last_parse_error,
        )
        return []

    def _run_vector_search(
        self,
        embedding: List[float],
        owner_cat_canon: List[str],
        project_name: str,
        config_name: Optional[str],
        module_type: Optional[str],
        routine_type: Optional[str],
        export: Optional[bool],
        is_ssl_api: Optional[bool],
        owner_qn: Optional[str],
        owner_qn_prefix: Optional[str],
        name: Optional[str],
        directive: Optional[str],
        per_leg_k: int,
    ) -> List[Dict[str, Any]]:
        """
        Execute the vector leg with index-level prefilter via Neo4j SEARCH.
        Fan-out by owner_categories (SEARCH does not allow IN); merge by routine id taking max
        similarity. No offset/limit slice — pagination happens after hybrid fusion.

        Capability/schema errors flip the process-wide `_VECTOR_MODE` to legacy
        `db.index.vector.queryNodes`. Other Neo4j errors trigger a request-local
        fallback without touching the cache; if that also fails, the original
        SEARCH error is re-raised.
        """
        mode = _VECTOR_MODE.get(_INDEX_KEY, "search")
        categories_for_fanout: List[Optional[str]] = list(owner_cat_canon) if owner_cat_canon else [None]

        def _legacy_call() -> List[Dict[str, Any]]:
            return self._run_vector_search_with_query_nodes(
                embedding=embedding,
                owner_cat_canon=owner_cat_canon,
                project_name=project_name,
                config_name=config_name,
                module_type=module_type,
                routine_type=routine_type,
                export=export,
                is_ssl_api=is_ssl_api,
                owner_qn=owner_qn,
                owner_qn_prefix=owner_qn_prefix,
                name=name,
                directive=directive,
                per_leg_k=per_leg_k,
            )

        if mode == "search":
            try:
                return self._run_vector_search_with_search(
                    embedding=embedding,
                    categories_for_fanout=categories_for_fanout,
                    project_name=project_name,
                    config_name=config_name,
                    module_type=module_type,
                    routine_type=routine_type,
                    export=export,
                    is_ssl_api=is_ssl_api,
                    owner_qn=owner_qn,
                    owner_qn_prefix=owner_qn_prefix,
                    name=name,
                    directive=directive,
                    per_leg_k=per_leg_k,
                )
            except Neo4jError as e:
                if is_vector_search_capability_or_schema_error(e):
                    logger.warning(
                        "Neo4j SEARCH for %s flipped to legacy queryNodes (capability/schema): %s",
                        _INDEX_KEY, e,
                    )
                    _VECTOR_MODE[_INDEX_KEY] = "queryNodes"
                    return _legacy_call()
                logger.warning(
                    "Neo4j SEARCH for %s failed (non-capability), trying request-local queryNodes: %s",
                    _INDEX_KEY, e,
                )
                try:
                    return _legacy_call()
                except Neo4jError as e2:
                    logger.error(
                        "Request-local queryNodes fallback for %s also failed: %s", _INDEX_KEY, e2,
                    )
                    raise e

        return _legacy_call()

    def _run_vector_search_with_search(
        self,
        embedding: List[float],
        categories_for_fanout: List[Optional[str]],
        project_name: str,
        config_name: Optional[str],
        module_type: Optional[str],
        routine_type: Optional[str],
        export: Optional[bool],
        is_ssl_api: Optional[bool],
        owner_qn: Optional[str],
        owner_qn_prefix: Optional[str],
        name: Optional[str],
        directive: Optional[str],
        per_leg_k: int,
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        with self.driver.session(database=settings.neo4j_database) as session:
            for owner_category in categories_for_fanout:
                cypher = build_routine_description_search_cypher(
                    owner_category=owner_category,
                    module_type=module_type,
                    routine_type=routine_type,
                    export=export,
                    is_ssl_api=is_ssl_api,
                    config_name=config_name,
                )
                params: Dict[str, Any] = {
                    "embedding": embedding,
                    "project_name": project_name,
                    "per_leg_k": per_leg_k,
                    "owner_qn": owner_qn if owner_qn else None,
                    "owner_qn_prefix": owner_qn_prefix if owner_qn_prefix else None,
                    "name": name if name else None,
                    "directive": directive if directive else None,
                }
                if config_name:
                    params["config_name"] = config_name
                if owner_category:
                    params["owner_category"] = owner_category
                if module_type:
                    params["module_type"] = module_type
                if routine_type:
                    params["routine_type"] = routine_type
                if export is not None:
                    params["export"] = export
                if is_ssl_api is not None:
                    params["is_ssl_api"] = is_ssl_api

                result = session.run(cypher, params)
                for record in result:
                    rec = dict(record)
                    rid = str(rec.get("id", ""))
                    if not rid:
                        continue
                    prev = merged.get(rid)
                    if prev is None or float(rec.get("similarity", 0.0) or 0.0) > float(prev.get("similarity", 0.0) or 0.0):
                        merged[rid] = rec

        records = list(merged.values())
        records.sort(key=lambda r: float(r.get("similarity", 0.0) or 0.0), reverse=True)
        return records

    def _run_vector_search_with_query_nodes(
        self,
        embedding: List[float],
        owner_cat_canon: List[str],
        project_name: str,
        config_name: Optional[str],
        module_type: Optional[str],
        routine_type: Optional[str],
        export: Optional[bool],
        is_ssl_api: Optional[bool],
        owner_qn: Optional[str],
        owner_qn_prefix: Optional[str],
        name: Optional[str],
        directive: Optional[str],
        per_leg_k: int,
    ) -> List[Dict[str, Any]]:
        # Caller-supplied prefix wins (template uses OR between exact owner_qn and prefix,
        # so both can coexist — e.g. exact owner + nested children under the same prefix).
        # If neither owner_qn nor owner_qn_prefix is set, fall back to the project guard
        # so we never leak across projects.
        if owner_qn_prefix:
            effective_prefix = owner_qn_prefix
        elif owner_qn:
            effective_prefix = None
        else:
            effective_prefix = project_name + "/"
        params = {
            'embedding': embedding,
            'limit': per_leg_k,
            'owner_qn': owner_qn if owner_qn else None,
            'owner_qn_prefix': effective_prefix,
            'routine_type': routine_type if routine_type else None,
            'export': export if isinstance(export, bool) else None,
            'is_ssl_api': is_ssl_api if isinstance(is_ssl_api, bool) else None,
            'name': name if name else None,
            'directive': directive if directive else None,
            'project_name': project_name,
            'project_prefix': project_name + "/",
            'config_name': config_name if config_name else None,
            'module_type': module_type if module_type else None,
            'owner_categories': list(owner_cat_canon) if owner_cat_canon else [],
        }
        with self.driver.session(database=settings.neo4j_database) as session:
            result = session.run(ROUTINE_DESCRIPTION_VECTOR_CYPHER, params)
            return [dict(record) for record in result]

    def search_by_description_vector(
        self,
        text: str,
        limit: int = None,
        offset: int = 0,
        owner_qn: str = None,
        owner_qn_prefix: str = None,
        routine_type: str = None,
        export: bool = None,
        is_ssl_api: bool = None,
        name: str = None,
        directive: str = None,
        config_name: str = None,
        project_name: str = None,
        owner_categories: Optional[List[str]] = None,
        module_type: Optional[str] = None,
        per_leg_k: Optional[int] = None,
    ) -> tuple[List[Dict[str, Any]], float, int]:
        """
        Search routines by description using vector index (Neo4j SEARCH with fallback).

        Returns (records, min_sim, n_tokens) — records already filtered by adaptive min_sim
        and sorted by similarity DESC. No offset/limit slice — caller paginates.
        """
        if not self.embedding_service:
            logger.warning("Vector search requested but embedding_service not initialized")
            return [], 0.0, 0

        if limit is None:
            limit = settings.routine_description_search_default_limit

        try:
            query_spec = build_embedding_format_spec(
                profile=self.embedding_service.text_format_profile,
                transport=self.embedding_service.transport,
                side="query",
                purpose="description",
                description_instruction=settings.embedding_description_query_instruction,
            )
            metric_started = embedding_metrics.started()
            try:
                batch_result = embedding_metrics.call_single_with_usage(
                    self.embedding_service, text, format_spec=query_spec
                )
            except Exception:
                embedding_metrics.record_failure(
                    event_type="routine_description.embedding.query",
                    embedding_service=self.embedding_service,
                    duration_ms=embedding_metrics.elapsed_ms(metric_started),
                )
                raise
            embedding_metrics.record_result(
                event_type="routine_description.embedding.query",
                embedding_service=self.embedding_service,
                result=batch_result,
                duration_ms=embedding_metrics.elapsed_ms(metric_started),
            )
            embedding = embedding_metrics.first_embedding(batch_result)
            if not embedding:
                logger.error("Embedding service returned empty embedding")
                return [], 0.0, 0
        except Exception as e:
            logger.error(f"Failed to generate embedding for query: {e}")
            return [], 0.0, 0

        owner_cat_canon = canon_categories(owner_categories) if owner_categories else []
        n_cats = max(1, len(owner_cat_canon))
        direct_call = per_leg_k is None
        if direct_call:
            per_leg_k = compute_per_leg_k(limit=limit, offset=offset, n_categories=n_cats)

        pn = project_name or settings.project_name

        records = self._run_vector_search(
            embedding=embedding,
            owner_cat_canon=owner_cat_canon,
            project_name=pn,
            config_name=config_name if isinstance(config_name, str) and config_name.strip() else None,
            module_type=module_type if isinstance(module_type, str) and module_type.strip() else None,
            routine_type=routine_type if isinstance(routine_type, str) and routine_type.strip() else None,
            export=export if isinstance(export, bool) else None,
            is_ssl_api=is_ssl_api if isinstance(is_ssl_api, bool) else None,
            owner_qn=owner_qn if isinstance(owner_qn, str) and owner_qn.strip() else None,
            owner_qn_prefix=owner_qn_prefix if isinstance(owner_qn_prefix, str) and owner_qn_prefix.strip() else None,
            name=name if isinstance(name, str) and name.strip() else None,
            directive=directive if isinstance(directive, str) and directive.strip() else None,
            per_leg_k=per_leg_k,
        )

        min_sim, n_tokens = compute_adaptive_min_sim(text)
        filtered: List[Dict[str, Any]] = []
        for r in records:
            try:
                sim = float(r.get("similarity", 0.0) or 0.0)
            except Exception:
                sim = 0.0
            if sim >= min_sim:
                filtered.append(r)

        # Direct vector-only callers expect at most `limit` rows starting from `offset`.
        # Hybrid leg (per_leg_k pre-supplied) sees the full oversampled set — fusion paginates later.
        if direct_call:
            start = offset or 0
            filtered = filtered[start:start + (limit or 0)]

        logger.debug(
            "Vector routine search returned %d routines; filtered by min_sim=%.3f (tokens=%d) -> %d for query: %s",
            len(records), min_sim, n_tokens, len(filtered), text,
        )
        return filtered, min_sim, n_tokens

    def search_by_description_hybrid(
        self,
        text: str,
        limit: int = None,
        offset: int = 0,
        min_score: float = 0.1,
        owner_qn: str = None,
        owner_qn_prefix: str = None,
        routine_type: str = None,
        export: bool = None,
        is_ssl_api: bool = None,
        name: str = None,
        directive: str = None,
        config_name: str = None,
        project_name: str = None,
        owner_categories: Optional[List[str]] = None,
        module_type: Optional[str] = None,
        with_pagination: bool = False,
    ):
        """
        Search routines by description using hybrid fulltext + vector search.

        Combines fulltext and vector search results with weighted scoring:
        - Fulltext weight: HYBRID_SEARCH_FULLTEXT_WEIGHT (default 0.3)
        - Vector weight: HYBRID_SEARCH_VECTOR_WEIGHT (default 0.7)

        Args:
            text: Search query text
            limit: Maximum number of results (default: routine_description_search_default_limit)
            min_score: Minimum fulltext score threshold
            owner_qn: Filter by owner qualified name (exact match)
            owner_qn_prefix: Filter by owner qualified name prefix (STARTS WITH match)
            routine_type: Filter by routine type ('Procedure' or 'Function')
            export: Filter by export flag
            is_ssl_api: Filter by SSL/BSP flag
            name: Substring filter on routine name
            directive: Substring filter on directive
            with_pagination: when True, return (rows, has_more) instead of rows. `rows`
                is exactly the top-`limit` page (the candidate/ranking budget `eff_k` stays
                derived from `limit`, so the top-`limit` ordering/scores are identical to
                the with_pagination=False call). `has_more` is derived from candidate-pool
                saturation, NOT from a slice lookahead — a slice can't reveal a row the
                bounded retrieval never fetched (eff_k ~= offset+limit per leg).

        Returns:
            with_pagination=False: list of routine dicts with hybrid_score, sorted DESC.
            with_pagination=True: (list, has_more: bool).
        """
        if limit is None:
            limit = settings.routine_description_search_default_limit

        owner_cat_canon = canon_categories(owner_categories) if owner_categories else []
        n_cats = max(1, len(owner_cat_canon))

        eff_k = compute_per_leg_k(limit=limit, offset=offset, n_categories=n_cats)

        # Description-rerank candidate pool widening. Honors the same caps as
        # `compute_per_leg_k`: HYBRID_EFF_K_CAP=0 keeps cap disabled, and
        # QUERY_MAX_RESULTS remains the absolute per-leg safety ceiling.
        if bool(getattr(settings, "routine_description_rerank_enabled", False)):
            rerank_top_k = max(1, int(getattr(settings, "routine_description_rerank_top_k", 50) or 50))
            eff_k = max(eff_k, rerank_top_k)
            hybrid_cap = int(getattr(settings, "hybrid_eff_k_cap", 0) or 0)
            if hybrid_cap > 0:
                eff_k = min(eff_k, hybrid_cap)
            qmax = int(getattr(settings, "query_max_results", 0) or 0)
            if qmax > 0:
                eff_k = min(eff_k, qmax)
            eff_k = max(1, eff_k)

        # Fulltext leg (single leg, owner_categories applied via IN; offset=0 for oversampling).
        # Isolate fulltext failures: vector leg must still run on Neo4jError here.
        try:
            fulltext_results = self.search_by_description_fulltext(
                text=text,
                limit=eff_k,
                offset=0,
                min_score=min_score,
                owner_qn=owner_qn,
                owner_qn_prefix=owner_qn_prefix,
                routine_type=routine_type,
                export=export,
                is_ssl_api=is_ssl_api,
                name=name,
                directive=directive,
                config_name=config_name,
                project_name=project_name,
                owner_categories=owner_cat_canon if owner_cat_canon else None,
                module_type=module_type,
            )
        except Neo4jError as e:
            logger.warning(
                "Routine hybrid: fulltext leg disabled for this query (code=%s): %s",
                getattr(e, 'code', '?'), e,
            )
            fulltext_results = []

        # Vector leg: fan-out by owner_categories inside _run_vector_search; per_leg_k pre-computed.
        vector_results, min_sim, n_tokens = self.search_by_description_vector(
            text=text,
            limit=eff_k,
            owner_qn=owner_qn,
            owner_qn_prefix=owner_qn_prefix,
            routine_type=routine_type,
            export=export,
            is_ssl_api=is_ssl_api,
            name=name,
            directive=directive,
            config_name=config_name,
            project_name=project_name,
            owner_categories=owner_cat_canon if owner_cat_canon else None,
            module_type=module_type,
            per_leg_k=eff_k,
        )

        # Helper: robust percentile and normalization
        def _compute_percentile(sorted_vals: List[float], p: float) -> float:
            n = len(sorted_vals)
            if n == 0:
                return 0.0
            if n == 1:
                return float(sorted_vals[0])
            idx = p * (n - 1)
            lo = int(idx)
            hi = lo + 1 if lo < n - 1 else lo
            frac = idx - lo
            return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac

        def _normalize_leg(records: List[Dict[str, Any]], value_key: str, strategy: str) -> Dict[str, float]:
            vals = [float(r.get(value_key, 0.0) or 0.0) for r in records]
            ids = [str(r.get('id', '')) for r in records]
            res: Dict[str, float] = {}
            if not ids:
                return res
            if strategy == 'p95':
                svals = sorted(vals)
                p50 = _compute_percentile(svals, 0.5)
                p95 = _compute_percentile(svals, 0.95)
                denom = p95 - p50
                if denom <= 1e-9:
                    vmax = max(svals) if svals else 1.0
                    denom2 = vmax if vmax > 0 else 1.0
                    for rid, v in zip(ids, vals):
                        res[rid] = max(0.0, min(1.0, v / denom2))
                else:
                    for rid, v in zip(ids, vals):
                        norm = (v - p50) / denom
                        if norm > 1.0:
                            norm = 1.0
                        if norm < 0.0:
                            norm = 0.0
                        res[rid] = norm
            else:
                vmax = max(vals) if vals else 1.0
                denom = vmax if vmax > 0 else 1.0
                for rid, v in zip(ids, vals):
                    res[rid] = max(0.0, min(1.0, v / denom))
            return res

        # Determine normalization and fusion strategies
        norm_strategy = str(getattr(settings, 'hybrid_normalization_strategy', 'max')).lower()
        fusion_mode = str(getattr(settings, 'hybrid_fusion_mode', 'weighted')).lower()

        # Token signal (for dynamic weights)
        try:
            token_count = len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text or ""))
        except Exception:
            token_count = 0

        # Normalized per-leg scores
        ft_norm_by_id = _normalize_leg(fulltext_results, 'score', norm_strategy)
        vec_norm_by_id = _normalize_leg(vector_results, 'similarity', norm_strategy)

        # Per-leg record lookup
        ft_rec_by_id = {str(r.get('id', '')): r for r in fulltext_results}
        vec_rec_by_id = {str(r.get('id', '')): r for r in vector_results}

        # Dynamic weights per query
        alpha = float(getattr(settings, 'hybrid_search_fulltext_weight', 0.3))
        beta = float(getattr(settings, 'hybrid_search_vector_weight', 0.7))
        s = alpha + beta
        if s > 0:
            alpha, beta = alpha / s, beta / s

        if bool(getattr(settings, 'hybrid_dynamic_weights_enabled', False)):
            ft_count = len(fulltext_results)
            vec_count = len(vector_results)

            short_thr = int(getattr(settings, 'ft_min_score_short_tokens', 2))
            if token_count <= short_thr:
                alpha = max(alpha, 0.7)

            if vec_count == 0 and ft_count > 0:
                alpha = 1.0
            elif ft_count == 0 and vec_count > 0:
                alpha = 0.0
            else:
                denom = float(max(1, eff_k))
                vec_frac = vec_count / denom
                ft_frac = ft_count / denom
                if vec_frac < 0.3:
                    alpha = min(alpha + 0.1, 1.0)
                if ft_frac < 0.3:
                    alpha = max(alpha - 0.1, 0.0)

            a_min = float(getattr(settings, 'hybrid_alpha_min', 0.2))
            a_max = float(getattr(settings, 'hybrid_alpha_max', 0.8))
            if a_max < a_min:
                a_max = a_min
            alpha = min(max(alpha, a_min), a_max)
            beta = 1.0 - alpha

        combined: Dict[str, Dict[str, Any]] = {}
        all_ids = set(ft_rec_by_id.keys()) | set(vec_rec_by_id.keys())

        if fusion_mode == 'rrf':
            # Weighted Reciprocal Rank Fusion
            try:
                k_const = int(getattr(settings, 'hybrid_rrf_k', 60))
            except Exception:
                k_const = 60
            ft_rank = {rid: idx for idx, (rid, _) in enumerate(ft_rec_by_id.items())}
            vec_rank = {rid: idx for idx, (rid, _) in enumerate(vec_rec_by_id.items())}
            for rid in all_ids:
                base = vec_rec_by_id.get(rid) or ft_rec_by_id.get(rid) or {}
                record = dict(base)
                rr = 0.0
                if rid in ft_rank:
                    rr += alpha / (k_const + ft_rank[rid] + 1.0)
                if rid in vec_rank:
                    rr += beta / (k_const + vec_rank[rid] + 1.0)
                record['fulltext_score'] = ft_norm_by_id.get(rid, 0.0)
                record['vector_score'] = vec_norm_by_id.get(rid, 0.0)
                record['hybrid_score'] = rr
                combined[rid] = record
        else:
            # Weighted normalized blending
            for rid in all_ids:
                base = vec_rec_by_id.get(rid) or ft_rec_by_id.get(rid) or {}
                record = dict(base)
                ft_s = ft_norm_by_id.get(rid, 0.0)
                vec_s = vec_norm_by_id.get(rid, 0.0)
                record['fulltext_score'] = ft_s
                record['vector_score'] = vec_s
                record['hybrid_score'] = alpha * ft_s + beta * vec_s
                combined[rid] = record

        # Sort with stable tie-breakers, optionally rerank, then paginate at the end
        sorted_all = sorted(
            combined.values(),
            key=lambda x: (-x.get('hybrid_score', 0.0), str(x.get('name', '')), str(x.get('owner_qn', '')))
        )
        sorted_all = self._apply_rerank_if_enabled(sorted_all, text)
        start = offset or 0
        end = start + (limit or len(sorted_all))
        sorted_results = sorted_all[start:end]

        if settings.enable_hybrid_logging:
            logger.info(
                "Hybrid search (eff_k=%s, offset=%s, limit=%s, fusion=%s, norm=%s, alpha=%.2f, beta=%.2f) ft=%s vec=%s combined=%s → returned %s",
                eff_k, offset, limit, fusion_mode, norm_strategy, alpha, beta,
                len(fulltext_results), len(vector_results), len(combined), len(sorted_results)
            )

        # Audit log for detailed tracking (sequential numbering to match other audit logs)
        if AUDIT_AVAILABLE and self.request_id and settings.enable_log:
            audit_block(
                "[3h.1] Fulltext search results",
                f"{len(fulltext_results)} routines (min_score={min_score})",
                self.request_id
            )
            audit_block(
                "[3h.2] Vector search results",
                f"{len(vector_results)} routines (min_sim={min_sim:.3f}, tokens={n_tokens})",
                self.request_id
            )
            audit_block(
                "[3h.3] Hybrid merge & fusion",
                f"{len(combined)} unique → {len(sorted_results)} returned (mode={fusion_mode}, α={alpha:.2f}, β={beta:.2f})",
                self.request_id
            )

        if with_pagination:
            # Bounded, deterministic has_more that never triggers a false-negative stop.
            # eff_k bounds each leg to ~offset+limit candidates, so a slice past offset+limit
            # cannot prove a next page. Instead: if neither leg hit its budget, the fused pool
            # is exhaustive and has_more is exact; if a leg saturated, more candidates may
            # exist -> report has_more=True conservatively (a false-positive only yields a
            # smaller/empty next page; a false-negative would silently drop results).
            page_end = (offset or 0) + (limit or 0)
            pool_exhaustive = (len(fulltext_results) < eff_k) and (len(vector_results) < eff_k)
            if pool_exhaustive:
                has_more = len(sorted_all) > page_end
            else:
                has_more = len(sorted_all) >= page_end
            return sorted_results, has_more

        return sorted_results

    def _apply_rerank_if_enabled(
        self,
        ranked: List[Dict[str, Any]],
        query: str,
    ) -> List[Dict[str, Any]]:
        """
        Optional cross-encoder rerank pass for routine description search.

        On success: reorders the top-K head with rerank scores and returns
        `reranked_head + head_without_rerank_score + original_tail`. The tail
        beyond top_k is preserved so MCP `search_bsl_routines` pagination
        stays stable when rerank is enabled.

        On any failure (disabled, reranker unavailable, no usable description
        text, rerank returns None) the original `ranked` list is returned
        unchanged.
        """
        if len(ranked) < 2:
            return ranked
        if not bool(getattr(settings, "routine_description_rerank_enabled", False)):
            return ranked

        reranker = get_reranker()
        if reranker is None:
            _warn_routine_description_rerank_unavailable_once()
            return ranked

        top_k = max(1, int(getattr(settings, "routine_description_rerank_top_k", 50) or 50))
        head = ranked[:top_k]
        tail = ranked[top_k:]

        documents: List[str] = []
        used: List[Dict[str, Any]] = []
        head_without_rerank_score: List[Dict[str, Any]] = []

        for row in head:
            document = build_routine_description_rerank_document(row)
            if not document:
                head_without_rerank_score.append(row)
                continue
            documents.append(document)
            used.append(row)

        if len(documents) < 2:
            logger.info("routine description rerank skipped: no usable description text")
            return ranked

        result = reranker.rerank(
            query, documents, top_n=len(documents),
            event_type="routine_description.rerank",
        )
        if result is None:
            return ranked

        reranked_head: List[Dict[str, Any]] = []
        seen_indices = set()
        for idx, score in result:
            if 0 <= idx < len(used):
                row = dict(used[idx])
                row["score"] = float(score)
                reranked_head.append(row)
                seen_indices.add(idx)

        for idx, row in enumerate(used):
            if idx not in seen_indices:
                head_without_rerank_score.append(row)

        if not reranked_head:
            return ranked

        logger.info(
            "routine description rerank applied: %d candidates reranked (from pool of %d)",
            len(reranked_head), len(ranked),
        )
        return reranked_head + head_without_rerank_score + tail
