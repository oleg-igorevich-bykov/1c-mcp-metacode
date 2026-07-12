"""
Shared helpers for vector / hybrid search services.

- `compute_adaptive_min_sim(text)` returns adaptive similarity threshold based on token count
  (same formula previously inlined in both MetadataSearchService and RoutineSearchService).
- `compute_per_leg_k(...)` returns the per-leg vector-index candidate budget for fan-out searches.
- `_VECTOR_MODE` is a process-wide cache of which Cypher style ('search' vs 'queryNodes') is
  currently usable for each index, so that the first Neo4jError on a SEARCH query switches the
  whole process to the legacy `db.index.vector.queryNodes` fallback.
"""
from __future__ import annotations

import re
from typing import Dict, Tuple

from config import settings


def compute_adaptive_min_sim(text: str) -> Tuple[float, int]:
    """
    Return (min_sim, n_tokens) for adaptive vector similarity threshold.
    Same thresholds previously inlined in metadata_search_service.py and routine_search_service.py.
    """
    try:
        tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text or "")
        n_tokens = len(tokens)
        short_tokens = int(getattr(settings, "vec_min_sim_short_tokens", 2))
        short_value = float(getattr(settings, "vec_min_sim_short_value", 0.25))
        medium_tokens = int(getattr(settings, "vec_min_sim_medium_tokens", 5))
        medium_value = float(getattr(settings, "vec_min_sim_medium_value", 0.20))
        default_value = float(getattr(settings, "vec_min_sim_default", 0.15))
        if n_tokens <= short_tokens:
            min_sim = short_value
        elif n_tokens <= medium_tokens:
            min_sim = medium_value
        else:
            min_sim = default_value
        return min_sim, n_tokens
    except Exception:
        return float(getattr(settings, "vec_min_sim_default", 0.15)), 0


def compute_per_leg_k(limit: int, offset: int, n_categories: int) -> int:
    """
    Per-leg vector candidate budget for fan-out searches.

    HYBRID_EFF_K_CAP=0 means "cap disabled" (preserves the current semantics in
    metadata_search_service.py:248 / routine_search_service.py:279), so a 0 here MUST NOT
    collapse per_leg_k to 0.

    QUERY_MAX_RESULTS remains the absolute per-leg safety cap.

    With fan-out (n_categories > 1) each leg gets at least HYBRID_EFF_K_CAP // 2 candidates,
    so that small categories still have decent recall, capped by per-leg cap and global safety cap.
    """
    try:
        oversample_factor = int(getattr(settings, "hybrid_oversample_factor", 1))
    except Exception:
        oversample_factor = 1
    if oversample_factor < 1:
        oversample_factor = 1

    try:
        hybrid_eff_k_cap = int(getattr(settings, "hybrid_eff_k_cap", 0))
    except Exception:
        hybrid_eff_k_cap = 0

    try:
        query_max_results = int(getattr(settings, "query_max_results", 0))
    except Exception:
        query_max_results = 0

    base_k = (limit or 0) + (offset or 0)
    if base_k <= 0:
        base_k = limit or 1
    desired = oversample_factor * base_k

    n_cats = max(1, int(n_categories or 1))
    if n_cats > 1 and hybrid_eff_k_cap > 0:
        fair_share = hybrid_eff_k_cap // n_cats
        per_leg_floor = hybrid_eff_k_cap // 2
        per_leg_k = max(per_leg_floor, min(desired, fair_share))
    else:
        per_leg_k = desired

    if hybrid_eff_k_cap > 0:
        per_leg_k = min(per_leg_k, hybrid_eff_k_cap)
    if query_max_results > 0:
        per_leg_k = min(per_leg_k, query_max_results)
    return max(1, per_leg_k)


# Process-wide cache: index_name -> 'search' (new SEARCH path) or 'queryNodes' (legacy fallback).
# Flipped to 'queryNodes' only on capability/schema errors classified by
# `is_vector_search_capability_or_schema_error`. Transient / runtime failures keep the cache
# untouched and use a request-local fallback instead.
_VECTOR_MODE: Dict[str, str] = {}


_VECTOR_CAPABILITY_CODES = {
    "Neo.ClientError.Statement.FeatureNotSupported",
}

_VECTOR_SYNTAX_MARKERS = ("search", "vector index", "nearest neighbors")

_VECTOR_CAPABILITY_MESSAGE_MARKERS = (
    "filterable",
    "not supported",
    "no such index",
    "there is no such vector",
    "invalid input 'search'",
)


def is_vector_search_capability_or_schema_error(exc: BaseException) -> bool:
    """Classify a Neo4j error as a vector-SEARCH capability/schema problem.

    Returns True only for signals that mean "this Neo4j build/index can't run
    the SEARCH-style vector cypher and never will until config changes" — i.e.
    safe reasons to flip `_VECTOR_MODE` for the whole process. Transient
    failures (network, timeout, generic runtime) return False so the caller
    does a request-local fallback without poisoning the cache.
    """
    code = getattr(exc, "code", "") or ""
    msg_lower = (str(exc) or "").lower()

    if code in _VECTOR_CAPABILITY_CODES:
        return True

    if code == "Neo.ClientError.Statement.SyntaxError" and any(
        m in msg_lower for m in _VECTOR_SYNTAX_MARKERS
    ):
        return True

    if any(m in msg_lower for m in _VECTOR_CAPABILITY_MESSAGE_MARKERS):
        return True

    return False
