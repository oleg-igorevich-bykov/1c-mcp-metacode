"""Hybrid search across object_summary fields with graceful fallback.

Strategy:
  * If both `vec_object_summary_embedding` and `ftx_object_summary_search_text`
    are available, run vector + fulltext and merge scores by a simple weighted
    sum (`hybrid_search_*_weight` from settings).
  * If only one index is available, return its result with normalised scores.
  * If neither is available, return an empty list with a descriptive log.

This service does not own embedding building — the MCP-tool resolves the
embedding for the query through the shared `EmbeddingService` first.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import settings

from graphdb import embedding_usage_metrics as embedding_metrics
from graphdb import object_summary_queries as q
from graphdb.object_summary_reranker import build_object_summary_rerank_document
from graphdb.reranker import get_reranker
from object_summary.storage import read_json

logger = logging.getLogger(__name__)


_object_summary_rerank_warned_unavailable: bool = False


def _warn_object_summary_rerank_unavailable_once() -> None:
    """One-shot misconfiguration warning: object_summary rerank enabled but shared reranker unavailable."""
    global _object_summary_rerank_warned_unavailable
    if _object_summary_rerank_warned_unavailable:
        return
    _object_summary_rerank_warned_unavailable = True
    logger.warning(
        "object_summary rerank requested (OBJECT_SUMMARY_RERANK_ENABLED=true) but shared "
        "reranker unavailable: RERANK_API_KEY missing or invalid"
    )


def reset_object_summary_rerank_warning() -> None:
    """Test hook: reset the one-shot misconfig warning."""
    global _object_summary_rerank_warned_unavailable
    _object_summary_rerank_warned_unavailable = False


def _index_exists(driver, index_name: str) -> bool:
    try:
        with driver.session(database=settings.neo4j_database) as session:
            rec = session.run(
                "SHOW INDEXES YIELD name WHERE name = $name RETURN count(name) AS n",
                name=index_name,
            ).single()
            return bool(rec and rec["n"] > 0)
    except Exception as exc:
        logger.warning("Index existence check failed for %s: %s", index_name, exc)
        return False


def _embed_query(query: str) -> Optional[List[float]]:
    try:
        from graphdb.embedding_service import get_embedding_service
        from graphdb.embedding_text_format import build_embedding_format_spec

        svc = get_embedding_service()
        if svc is None:
            return None
        spec = build_embedding_format_spec(
            profile=svc.text_format_profile,
            transport=svc.transport,
            side="query",
            purpose="description",
            description_instruction=settings.embedding_description_query_instruction or "",
        )
        metric_started = embedding_metrics.started()
        try:
            batch_result = embedding_metrics.call_single_with_usage(
                svc, query, format_spec=spec
            )
        except Exception:
            embedding_metrics.record_failure(
                event_type="object_summary.embedding.query",
                embedding_service=svc,
                duration_ms=embedding_metrics.elapsed_ms(metric_started),
            )
            raise
        embedding_metrics.record_result(
            event_type="object_summary.embedding.query",
            embedding_service=svc,
            result=batch_result,
            duration_ms=embedding_metrics.elapsed_ms(metric_started),
        )
        return embedding_metrics.first_embedding(batch_result)
    except Exception as exc:
        logger.warning("Query embedding failed: %s", exc)
        return None


def _normalise(rows: List[Dict[str, Any]], key: str = "score") -> List[Dict[str, Any]]:
    if not rows:
        return rows
    scores = [float(r.get(key) or 0.0) for r in rows]
    hi = max(scores) if scores else 0.0
    if hi <= 0.0:
        return rows
    out = []
    for r in rows:
        rr = dict(r)
        rr[key] = float(r.get(key) or 0.0) / hi
        out.append(rr)
    return out


def search_objects_by_summary(
    driver,
    *,
    project_name: str,
    query: str,
    categories: Optional[List[str]] = None,
    config_name: Optional[str] = None,
    limit: int = 10,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    has_vector = _index_exists(driver, "vec_object_summary_embedding")
    has_fulltext = _index_exists(driver, "ftx_object_summary_search_text")

    if not has_vector and not has_fulltext:
        logger.warning(
            "Neither vec_object_summary_embedding nor ftx_object_summary_search_text exists; "
            "search_objects_by_summary returns empty"
        )
        return []

    top_k = max(limit + offset, limit) * max(1, int(getattr(settings, "hybrid_oversample_factor", 3) or 3))
    if bool(getattr(settings, "object_summary_rerank_enabled", False)):
        top_k = max(top_k, max(1, int(getattr(settings, "object_summary_rerank_top_k", 50) or 50)))
    top_k = min(top_k, int(getattr(settings, "hybrid_eff_k_cap", 300) or 300))

    vector_rows: List[Dict[str, Any]] = []
    fulltext_rows: List[Dict[str, Any]] = []

    if has_vector:
        embedding = _embed_query(query)
        if embedding:
            try:
                vector_rows = q.vector_search_summary(
                    driver,
                    project_name=project_name,
                    embedding=embedding,
                    categories=categories,
                    config_name=config_name,
                    top_k=top_k,
                )
            except Exception:
                logger.exception("Vector search failed")
                vector_rows = []

    if has_fulltext:
        try:
            fulltext_rows = q.fulltext_search_summary(
                driver,
                project_name=project_name,
                query=query,
                categories=categories,
                config_name=config_name,
                top_k=top_k,
            )
        except Exception:
            logger.exception("Fulltext search failed")
            fulltext_rows = []

    if not vector_rows and not fulltext_rows:
        return []

    if vector_rows and not fulltext_rows:
        ranked = _normalise(vector_rows)
    elif fulltext_rows and not vector_rows:
        ranked = _normalise(fulltext_rows)
    else:
        v_norm = _normalise(vector_rows)
        f_norm = _normalise(fulltext_rows)
        v_weight = float(getattr(settings, "hybrid_search_vector_weight", 0.7) or 0.7)
        f_weight = float(getattr(settings, "hybrid_search_fulltext_weight", 0.3) or 0.3)
        merged: Dict[str, Dict[str, Any]] = {}
        for row in v_norm:
            qn = row["qualified_name"]
            merged[qn] = {**row, "score": float(row.get("score") or 0.0) * v_weight}
        for row in f_norm:
            qn = row["qualified_name"]
            ft_score = float(row.get("score") or 0.0) * f_weight
            if qn in merged:
                merged[qn]["score"] += ft_score
            else:
                merged[qn] = {**row, "score": ft_score}
        ranked = sorted(merged.values(), key=lambda r: r["score"], reverse=True)

    # TODO: consider moving rerank text to `m.object_summary_rerank_text` derived
    # field in Neo4j (analogous to object_summary_search_text) to avoid file I/O
    # in the search path; see plan section 4 tradeoff.
    ranked = _apply_rerank_if_enabled(ranked, query)

    return ranked[offset: offset + limit]


def _apply_rerank_if_enabled(
    ranked: List[Dict[str, Any]],
    query: str,
) -> List[Dict[str, Any]]:
    """
    Optional cross-encoder rerank pass for object_summary search.

    On success: reorders the top-K head with rerank scores and returns
    `reranked_head + head_without_rerank_score + original_tail`. The tail beyond
    top_k is preserved (unlike BSL): MCP `find_objects_by_summary` exposes
    `offset/limit` and pagination must not break when rerank is enabled.

    On any failure (disabled, reranker unavailable, no usable summaries, rerank
    returns None) the original `ranked` list is returned unchanged. The total
    number of candidates never decreases due to rerank.
    """
    if len(ranked) < 2:
        return ranked
    if not bool(getattr(settings, "object_summary_rerank_enabled", False)):
        return ranked

    reranker = get_reranker()
    if reranker is None:
        _warn_object_summary_rerank_unavailable_once()
        return ranked

    top_k = max(1, int(getattr(settings, "object_summary_rerank_top_k", 50) or 50))
    head = ranked[:top_k]
    tail = ranked[top_k:]

    documents: List[str] = []
    used: List[Dict[str, Any]] = []
    head_without_rerank_score: List[Dict[str, Any]] = []

    t0 = time.perf_counter()
    n_read = 0
    for row in head:
        path_str = row.get("path")
        payload = read_json(Path(path_str)) if path_str else None
        if payload is None:
            head_without_rerank_score.append(row)
            continue
        n_read += 1
        document = build_object_summary_rerank_document(
            payload,
            category=row.get("category") or "",
            name=row.get("name") or "",
            config_name=row.get("config_name") or "",
        )
        if not document:
            head_without_rerank_score.append(row)
            continue
        documents.append(document)
        used.append(row)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    logger.info(
        "object_summary rerank: read %d summary.json files in %.1fms",
        n_read, elapsed_ms,
    )

    if len(documents) < 2:
        logger.info("object_summary rerank skipped: no usable summary text")
        return ranked

    result = reranker.rerank(
        query, documents, top_n=len(documents),
        event_type="object_summary.rerank",
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

    # Partial response from provider: candidates in `used` whose index is not
    # in the result are preserved with their original hybrid score.
    for idx, row in enumerate(used):
        if idx not in seen_indices:
            head_without_rerank_score.append(row)

    if not reranked_head:
        return ranked

    logger.info(
        "object_summary rerank applied: %d candidates reranked (from pool of %d)",
        len(reranked_head), len(ranked),
    )
    return reranked_head + head_without_rerank_score + tail
