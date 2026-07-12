"""
Shared helpers for embedding pipeline: boundary-aware text chunking,
L2 normalization and weighted mean pooling.
"""

import logging
import math
from typing import List, Optional, Tuple


logger = logging.getLogger(__name__)


_SENTENCE_BOUNDARIES = (". ", "! ", "? ", "; ", ": ")


def split_text_for_embedding(
    text: str,
    max_chars: int,
    overlap_chars: int,
    max_chunks: int,
    min_chunk_chars: Optional[int] = None,
) -> Tuple[List[str], List[int]]:
    """
    Split text into overlapping chunks, preferring natural boundaries inside the
    sliding window: paragraph break, line break, sentence punctuation, whitespace,
    hard cut as last resort.

    Returns (chunks, lengths_in_chars).
    """
    text = (text or "").strip()
    if not text or max_chars <= 0 or max_chunks <= 0:
        return [], []

    if overlap_chars < 0:
        overlap_chars = 0
    if overlap_chars >= max_chars:
        overlap_chars = max(0, max_chars // 4)

    if min_chunk_chars is None:
        min_chunk_chars = min(1000, max_chars // 3)
    if min_chunk_chars < 0:
        min_chunk_chars = 0
    if min_chunk_chars >= max_chars:
        min_chunk_chars = max(0, max_chars // 3)

    chunks: List[str] = []
    lengths: List[int] = []
    n = len(text)
    start = 0
    produced = 0

    while start < n and produced < max_chunks:
        end_max = min(n, start + max_chars)

        if (n - start) <= max_chars:
            end = n
        else:
            end = _find_boundary(text, start, end_max, min_chunk_chars)

        chunk = text[start:end]
        if not chunk:
            break

        chunks.append(chunk)
        lengths.append(len(chunk))
        produced += 1

        if end >= n:
            break

        next_start = end - overlap_chars
        if next_start <= start:
            next_start = end
        start = next_start

    if produced >= max_chunks and start < n:
        logger.warning(
            "split_text_for_embedding: max_chunks=%d reached, truncating remaining %d chars (total text len=%d)",
            max_chunks, n - start, n
        )

    return chunks, lengths


def _find_boundary(text: str, start: int, end_max: int, min_chunk_chars: int) -> int:
    """
    Find the cut position inside the window [start + min_chunk_chars, end_max].
    Returns absolute index where the next chunk should start (== end of current chunk).
    Falls back to end_max if no natural boundary found.
    """
    window_start = start + min_chunk_chars
    if window_start >= end_max:
        return end_max

    para = text.rfind("\n\n", window_start, end_max)
    if para != -1:
        return para + 2

    nl = text.rfind("\n", window_start, end_max)
    if nl != -1:
        return nl + 1

    best_sent = -1
    for marker in _SENTENCE_BOUNDARIES:
        pos = text.rfind(marker, window_start, end_max)
        if pos > best_sent:
            best_sent = pos
    if best_sent != -1:
        return best_sent + 2

    ws = text.rfind(" ", window_start, end_max)
    if ws != -1:
        return ws + 1

    return end_max


def l2_normalize(vec: List[float]) -> List[float]:
    """L2-normalize a vector; returns original if norm is zero or error occurs."""
    try:
        norm = math.sqrt(sum((x * x for x in vec)))
        if norm > 0:
            return [x / norm for x in vec]
        return vec
    except Exception:
        return vec


def weighted_mean_pool(
    vectors: List[List[float]],
    weights: List[int],
    *,
    l2_chunks: bool = True,
    l2_final: bool = True,
) -> List[float]:
    """
    Weighted mean pooling of chunk embeddings.
    weights typically equal chunk lengths in characters.
    Applies optional per-chunk and final L2 normalization.
    """
    if not vectors:
        return []

    first_valid = next((v for v in vectors if v), None)
    if first_valid is None:
        return []
    dims = len(first_valid)

    if weights and len(weights) == len(vectors):
        eff_weights = [int(max(1, w)) for w in weights]
    else:
        eff_weights = [1] * len(vectors)
    total_w = float(sum(eff_weights))
    if total_w <= 0:
        eff_weights = [1] * len(vectors)
        total_w = float(len(vectors))

    acc = [0.0] * dims
    for vec, w in zip(vectors, eff_weights):
        if not vec:
            continue
        vv = l2_normalize(vec) if l2_chunks else vec
        if len(vv) != dims:
            m = min(dims, len(vv))
            vv = vv[:m]
            acc = acc[:m]
            dims = m
        wf = float(w)
        for i in range(dims):
            acc[i] += vv[i] * wf

    pooled = [x / total_w for x in acc]

    if l2_final:
        pooled = l2_normalize(pooled)
    return pooled
