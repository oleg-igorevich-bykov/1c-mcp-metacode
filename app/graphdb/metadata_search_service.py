"""
Service for searching metadata objects by description using fulltext, vector, or hybrid search.

This service provides three search modes:
1. Fulltext-only: Uses ftx_metadataobject_description index
2. Vector-only: Uses vec_metadataobject_description index with embeddings
3. Hybrid: Combines fulltext + vector with weighted scoring

All search modes use canonical Cypher queries from metadata_description_queries.py
to ensure consistency and avoid code duplication.
"""
from __future__ import annotations

import logging
import re
from typing import List, Dict, Any, Optional

from neo4j.exceptions import Neo4jError

from config import settings
from graphdb.metadata_description_queries import (
    METADATA_DESCRIPTION_VECTOR_CYPHER,
    build_metadata_description_fulltext_cypher,
    build_metadata_description_search_cypher,
)
from graphdb.metadata_description_reranker import build_metadata_description_rerank_document
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

_INDEX_KEY = "vec_metadataobject_description"


_metadata_description_rerank_warned_unavailable: bool = False


def _warn_metadata_description_rerank_unavailable_once() -> None:
    """One-shot misconfiguration warning."""
    global _metadata_description_rerank_warned_unavailable
    if _metadata_description_rerank_warned_unavailable:
        return
    _metadata_description_rerank_warned_unavailable = True
    logger.warning(
        "metadata description rerank requested (METADATA_DESCRIPTION_RERANK_ENABLED=true) but shared "
        "reranker unavailable: RERANK_API_KEY missing or invalid"
    )


def reset_metadata_description_rerank_warning() -> None:
    """Test hook: reset the one-shot misconfig warning."""
    global _metadata_description_rerank_warned_unavailable
    _metadata_description_rerank_warned_unavailable = False

# Import audit_block for detailed logging to audit log file
try:
    from mcpsrv.audit import audit_block
    AUDIT_AVAILABLE = True
except ImportError:
    AUDIT_AVAILABLE = False
    audit_block = None


class MetadataSearchService:
    """Service for searching metadata objects by description"""

    def __init__(self, driver, embedding_service=None, request_id=None):
        """
        Initialize metadata search service.

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
        categories: list = None,
        limit: int = None,
        offset: int = 0,
        min_score: float = 0.1,
        project_name: str = None,
        config_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search metadata objects by description using fulltext index.

        Args:
            text: Fulltext search query
            categories: List of category names to filter by (e.g., ['Справочники', 'Документы'])
            limit: Maximum number of results (default: metadata_description_search_default_limit)
            offset: Skip first N results (for pagination)
            min_score: Minimum fulltext score threshold
            config_name: Optional config scope; when provided the builder injects
                AND m.config_name = $config_name (otherwise no config filter).

        Returns:
            List of metadata object dictionaries with fields: category, name, qualified_name,
            synonym, comment, help_text, explanation, score
        """
        if limit is None:
            limit = settings.metadata_description_search_default_limit

        # Canonicalize categories once (handles case and synonyms like "регистры")
        cat_canon = canon_categories(categories) if categories else []

        candidates = build_fulltext_query_candidates(text)
        if not candidates:
            return []

        pn = project_name or settings.project_name
        params = {
            'categories': cat_canon,
            'limit': limit,
            'offset': offset,
            'min_score': min_score,
            'project_name': pn,
            'project_prefix': pn + "/",
        }
        if config_name:
            params['config_name'] = config_name

        cypher = build_metadata_description_fulltext_cypher(config_name=config_name)

        last_parse_error: Optional[BaseException] = None
        with self.driver.session(database=settings.neo4j_database) as session:
            for candidate in candidates:
                attempt_params = dict(params)
                attempt_params['text'] = candidate
                try:
                    result = session.run(cypher, attempt_params)
                    records = [dict(record) for record in result]
                except Neo4jError as e:
                    if is_lucene_fulltext_parse_error(e):
                        logger.warning(
                            "Lucene parse error on metadata fulltext candidate %r: %s",
                            candidate, e,
                        )
                        last_parse_error = e
                        continue
                    raise
                logger.debug(
                    "Fulltext metadata search returned %d objects (config=%s) for query: %s",
                    len(records), config_name, text,
                )
                return records

        logger.warning(
            "All metadata fulltext candidates failed Lucene parse for query %r (last error: %s)",
            text, last_parse_error,
        )
        return []

    def _run_vector_search(
        self,
        embedding: List[float],
        cat_canon: List[str],
        project_name: str,
        config_name: Optional[str],
        per_leg_k: int,
    ) -> List[Dict[str, Any]]:
        """
        Execute the vector leg with index-level prefilter via Neo4j SEARCH.
        Fan-out: one SEARCH per category (SEARCH does not allow IN); merge by qualified_name
        taking max similarity. No offset/limit slice here — pagination happens after hybrid fusion.

        Capability/schema errors flip the process-wide `_VECTOR_MODE` to legacy
        `db.index.vector.queryNodes` so subsequent calls skip the failing SEARCH path.
        Other Neo4j errors fall back to legacy queryNodes for this request only,
        without poisoning the cache; if the request-local fallback also fails, the
        original SEARCH error is re-raised.
        """
        mode = _VECTOR_MODE.get(_INDEX_KEY, "search")
        categories_for_fanout: List[Optional[str]] = list(cat_canon) if cat_canon else [None]

        if mode == "search":
            try:
                return self._run_vector_search_with_search(
                    embedding, categories_for_fanout, project_name, config_name, per_leg_k,
                )
            except Neo4jError as e:
                if is_vector_search_capability_or_schema_error(e):
                    logger.warning(
                        "Neo4j SEARCH for %s flipped to legacy queryNodes (capability/schema): %s",
                        _INDEX_KEY, e,
                    )
                    _VECTOR_MODE[_INDEX_KEY] = "queryNodes"
                    return self._run_vector_search_with_query_nodes(
                        embedding, cat_canon, project_name, config_name, per_leg_k,
                    )
                logger.warning(
                    "Neo4j SEARCH for %s failed (non-capability), trying request-local queryNodes: %s",
                    _INDEX_KEY, e,
                )
                try:
                    return self._run_vector_search_with_query_nodes(
                        embedding, cat_canon, project_name, config_name, per_leg_k,
                    )
                except Neo4jError as e2:
                    logger.error(
                        "Request-local queryNodes fallback for %s also failed: %s", _INDEX_KEY, e2,
                    )
                    raise e

        return self._run_vector_search_with_query_nodes(
            embedding, cat_canon, project_name, config_name, per_leg_k,
        )

    def _run_vector_search_with_search(
        self,
        embedding: List[float],
        categories_for_fanout: List[Optional[str]],
        project_name: str,
        config_name: Optional[str],
        per_leg_k: int,
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        with self.driver.session(database=settings.neo4j_database) as session:
            for category in categories_for_fanout:
                cypher = build_metadata_description_search_cypher(
                    category_name=category,
                    config_name=config_name,
                )
                params: Dict[str, Any] = {
                    "embedding": embedding,
                    "project_name": project_name,
                    "per_leg_k": per_leg_k,
                }
                if config_name:
                    params["config_name"] = config_name
                if category:
                    params["category_name"] = category
                result = session.run(cypher, params)
                for record in result:
                    rec = dict(record)
                    qn = str(rec.get("qualified_name", ""))
                    if not qn:
                        continue
                    prev = merged.get(qn)
                    if prev is None or float(rec.get("similarity", 0.0) or 0.0) > float(prev.get("similarity", 0.0) or 0.0):
                        merged[qn] = rec

        records = list(merged.values())
        records.sort(key=lambda r: float(r.get("similarity", 0.0) or 0.0), reverse=True)
        return records

    def _run_vector_search_with_query_nodes(
        self,
        embedding: List[float],
        cat_canon: List[str],
        project_name: str,
        config_name: Optional[str],
        per_leg_k: int,
    ) -> List[Dict[str, Any]]:
        params = {
            'embedding': embedding,
            'limit': per_leg_k,
            'categories': cat_canon or [],
            'project_name': project_name,
            'project_prefix': project_name + "/",
            'config_name': config_name if config_name else None,
        }
        with self.driver.session(database=settings.neo4j_database) as session:
            result = session.run(METADATA_DESCRIPTION_VECTOR_CYPHER, params)
            return [dict(record) for record in result]

    def search_by_description_vector(
        self,
        text: str,
        categories: list = None,
        limit: int = None,
        offset: int = 0,
        skip_oversampling: bool = False,
        project_name: str = None,
        config_name: Optional[str] = None,
        per_leg_k: Optional[int] = None,
    ) -> tuple[List[Dict[str, Any]], float, int]:
        """
        Search metadata objects by description using vector index (Neo4j SEARCH with fallback).

        Args:
            text: Query text to generate embedding from
            categories: List of category names to filter by (fan-out: one SEARCH leg per category)
            limit: Maximum number of results (default: metadata_description_search_default_limit)
            offset: Used only for per_leg_k computation when called direct (not hybrid)
            skip_oversampling: When True (hybrid leg), `per_leg_k` is expected to be supplied
                by the caller and oversampling is NOT re-applied here.
            config_name: Optional config scope; goes into SEARCH prefilter + outer WHERE.
            per_leg_k: Pre-computed candidate budget from hybrid pipeline; when None it is
                computed here for direct vector-only callers, and the returned list is sliced
                to [offset:offset+limit] after the min_sim filter (so direct API still honours
                its `limit` contract). When supplied (hybrid leg), no slice is applied so that
                hybrid fusion sees the full oversampled candidate set.

        Returns:
            (records, min_sim, n_tokens) — records filtered by adaptive min_sim and sorted
            by similarity DESC. Sliced to `limit` only for direct vector-only callers.
        """
        if not self.embedding_service:
            logger.warning("Vector search requested but embedding_service not initialized")
            return [], 0.0, 0

        if limit is None:
            limit = settings.metadata_description_search_default_limit

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
                    event_type="metadata_description.embedding.query",
                    embedding_service=self.embedding_service,
                    duration_ms=embedding_metrics.elapsed_ms(metric_started),
                )
                raise
            embedding_metrics.record_result(
                event_type="metadata_description.embedding.query",
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

        cat_canon = canon_categories(categories) if categories else []
        n_categories = max(1, len(cat_canon))

        direct_call = per_leg_k is None
        if direct_call:
            per_leg_k = compute_per_leg_k(limit=limit, offset=offset, n_categories=n_categories)

        pn = project_name or settings.project_name

        records = self._run_vector_search(
            embedding=embedding,
            cat_canon=cat_canon,
            project_name=pn,
            config_name=config_name,
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

        if direct_call:
            start = offset or 0
            filtered = filtered[start:start + (limit or 0)]

        logger.debug(
            "Vector metadata search returned %d objects; filtered by min_sim=%.3f (tokens=%d) -> %d for query: %s",
            len(records), min_sim, n_tokens, len(filtered), text,
        )
        return filtered, min_sim, n_tokens

    def search_by_description_hybrid(
        self,
        text: str,
        categories: list = None,
        limit: int = None,
        offset: int = 0,
        min_score: float = 0.1,
        project_name: str = None,
        config_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search metadata objects by description using hybrid fulltext + vector search.

        Combines fulltext and vector search results with weighted scoring:
        - Fulltext weight: hybrid_search_fulltext_weight (default 0.3)
        - Vector weight: hybrid_search_vector_weight (default 0.7)

        Args:
            text: Search query text
            categories: List of category names to filter by
            limit: Maximum number of results (default: metadata_description_search_default_limit)
            offset: Skip first N results (for pagination)
            min_score: Minimum fulltext score threshold

        Returns:
            List of metadata object dictionaries with hybrid_score field, sorted by hybrid_score DESC
        """
        if limit is None:
            limit = settings.metadata_description_search_default_limit

        # Canonicalize categories once to know fan-out shape for per_leg_k
        cat_canon = canon_categories(categories) if categories else []
        n_categories = max(1, len(cat_canon))

        # Compute effective depth per leg (oversampling + per-leg cap + global safety cap).
        # NOTE: same formula is reused for both fulltext (single leg) and each vector fan-out leg.
        eff_k = compute_per_leg_k(limit=limit, offset=offset, n_categories=n_categories)

        # Description-rerank candidate pool widening. Honors the same caps as
        # `compute_per_leg_k`: HYBRID_EFF_K_CAP=0 keeps cap disabled, and
        # QUERY_MAX_RESULTS remains the absolute per-leg safety ceiling that
        # rerank_top_k must not bypass.
        if bool(getattr(settings, "metadata_description_rerank_enabled", False)):
            rerank_top_k = max(1, int(getattr(settings, "metadata_description_rerank_top_k", 50) or 50))
            eff_k = max(eff_k, rerank_top_k)
            hybrid_cap = int(getattr(settings, "hybrid_eff_k_cap", 0) or 0)
            if hybrid_cap > 0:
                eff_k = min(eff_k, hybrid_cap)
            qmax = int(getattr(settings, "query_max_results", 0) or 0)
            if qmax > 0:
                eff_k = min(eff_k, qmax)
            eff_k = max(1, eff_k)

        # Fulltext leg (always offset=0 for oversampling; config_name + categories in WHERE).
        # Isolate fulltext failures: vector leg must still run on Neo4jError here.
        try:
            fulltext_results = self.search_by_description_fulltext(
                text=text,
                categories=categories,
                limit=eff_k,
                offset=0,
                min_score=min_score,
                project_name=project_name,
                config_name=config_name,
            )
        except Neo4jError as e:
            logger.warning(
                "Metadata hybrid: fulltext leg disabled for this query (code=%s): %s",
                getattr(e, 'code', '?'), e,
            )
            fulltext_results = []

        # Vector leg: per_leg_k computed above; skip_oversampling so the service doesn't re-oversample.
        vector_results, min_sim, n_tokens = self.search_by_description_vector(
            text=text,
            categories=categories,
            limit=eff_k,
            skip_oversampling=True,
            project_name=project_name,
            config_name=config_name,
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
            ids = [str(r.get('qualified_name', '')) for r in records]
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

        # Per-leg record lookup (using qualified_name as unique ID)
        ft_rec_by_id = {str(r.get('qualified_name', '')): r for r in fulltext_results}
        vec_rec_by_id = {str(r.get('qualified_name', '')): r for r in vector_results}

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
            key=lambda x: (-x.get('hybrid_score', 0.0), str(x.get('category', '')), str(x.get('name', '')))
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

        # Audit log for detailed tracking
        if AUDIT_AVAILABLE and self.request_id and settings.enable_log:
            audit_block(
                "[3h.1] Fulltext search results",
                f"{len(fulltext_results)} metadata objects (min_score={min_score})",
                self.request_id
            )
            audit_block(
                "[3h.2] Vector search results",
                f"{len(vector_results)} metadata objects (min_sim={min_sim:.3f}, tokens={n_tokens})",
                self.request_id
            )
            audit_block(
                "[3h.3] Hybrid merge & fusion",
                f"{len(combined)} unique → {len(sorted_results)} returned (mode={fusion_mode}, α={alpha:.2f}, β={beta:.2f})",
                self.request_id
            )

        return sorted_results

    def _apply_rerank_if_enabled(
        self,
        ranked: List[Dict[str, Any]],
        query: str,
    ) -> List[Dict[str, Any]]:
        """
        Optional cross-encoder rerank pass for metadata description search.

        On success: reorders the top-K head with rerank scores and returns
        `reranked_head + head_without_rerank_score + original_tail`. The tail
        beyond top_k is preserved so MCP `find_metadata_objects` pagination
        stays stable when rerank is enabled.

        On any failure (disabled, reranker unavailable, no usable description
        text, rerank returns None) the original `ranked` list is returned
        unchanged.
        """
        if len(ranked) < 2:
            return ranked
        if not bool(getattr(settings, "metadata_description_rerank_enabled", False)):
            return ranked

        reranker = get_reranker()
        if reranker is None:
            _warn_metadata_description_rerank_unavailable_once()
            return ranked

        top_k = max(1, int(getattr(settings, "metadata_description_rerank_top_k", 50) or 50))
        head = ranked[:top_k]
        tail = ranked[top_k:]

        documents: List[str] = []
        used: List[Dict[str, Any]] = []
        head_without_rerank_score: List[Dict[str, Any]] = []

        for row in head:
            document = build_metadata_description_rerank_document(row)
            if not document:
                head_without_rerank_score.append(row)
                continue
            documents.append(document)
            used.append(row)

        if len(documents) < 2:
            logger.info("metadata description rerank skipped: no usable description text")
            return ranked

        result = reranker.rerank(
            query, documents, top_n=len(documents),
            event_type="metadata_description.rerank",
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

        # Partial response from provider: keep unranked `used` candidates
        # in head_without_rerank_score with their original hybrid score.
        for idx, row in enumerate(used):
            if idx not in seen_indices:
                head_without_rerank_score.append(row)

        if not reranked_head:
            return ranked

        logger.info(
            "metadata description rerank applied: %d candidates reranked (from pool of %d)",
            len(reranked_head), len(ranked),
        )
        return reranked_head + head_without_rerank_score + tail
