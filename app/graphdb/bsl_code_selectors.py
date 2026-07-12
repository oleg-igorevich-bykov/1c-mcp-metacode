"""
Window selectors for BSL code search.

`compact_windows` implements the validated parent-window strategy: sort parent
chunks by composite score, find the best contiguous window of `top_k` parts
inside each top parent, then distribute slots between windows by the chosen
`quota`. The three profile selectors below preset the constants for the
vector and RLM paths.

Each part dict must have:
    part_id        : str  (unit_id)
    parent_id      : str  (routine_id)
    part_index     : int  (0-based)
    score          : float
Optional: line_start, line_end, body_hash — carried through to the output.

Three profile selectors are exposed:
    select_vector_window     — always_window_2-2-1, parent_top_n=5, all penalties 0.
                               Used by the vector hybrid path.
    select_rlm_window_3600   — rlm_always_window_parents5_round, count_weight=0.05.
                               Used by RLM fallback when split=3600.
    select_rlm_window_2200   — rlm_always_window_parents1_3-1-1.
                               Used by RLM fallback when split=2200.

`gate0.3` in the reference profile name maps to `gate_threshold` used by
gated_window selectors only — for `always_window` it does not apply.
"""
from __future__ import annotations

import itertools
from collections import defaultdict
from typing import Dict, List, Sequence


def _normalize_scores(parts: Sequence[dict]) -> Dict[str, float]:
    if not parts:
        return {}
    scores = {p["part_id"]: float(p["score"]) for p in parts}
    lo = min(scores.values())
    hi = max(scores.values())
    if hi <= lo:
        return {pid: 1.0 for pid in scores}
    return {pid: (s - lo) / (hi - lo) for pid, s in scores.items()}


def _parent_scores(
    parts: Sequence[dict],
    scores: Dict[str, float],
    count_weight: float,
    avg_weight: float,
) -> List[tuple[str, float]]:
    grouped: Dict[str, List[float]] = defaultdict(list)
    for p in parts:
        grouped[p["parent_id"]].append(scores[p["part_id"]])
    out: List[tuple[str, float]] = []
    for parent_id, values in grouped.items():
        ordered = sorted(values, reverse=True)
        max_score = ordered[0]
        avg_score = sum(ordered[:3]) / min(3, len(ordered))
        score = max_score + count_weight * len(values) + avg_weight * avg_score
        out.append((parent_id, score))
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def _best_window_for_parent(
    parent_id: str,
    top_parts: Sequence[dict],
    all_by_parent: Dict[str, List[dict]],
    scores: Dict[str, float],
    top_k: int,
    outside_penalty: float,
    span_penalty: float,
) -> tuple[float, List[dict]]:
    parent_parts = all_by_parent.get(parent_id) or []
    source_indices = {
        int(p["part_index"])
        for p in top_parts
        if p["parent_id"] == parent_id
    }
    if not parent_parts or not source_indices:
        return -1e9, []

    index_to_pos: Dict[int, int] = {
        int(p["part_index"]): pos for pos, p in enumerate(parent_parts)
    }
    candidate_starts = set()
    for idx in source_indices:
        pos = index_to_pos.get(int(idx))
        if pos is None:
            continue
        for shift in range(top_k):
            start = max(0, min(len(parent_parts) - top_k, pos - shift))
            candidate_starts.add(start)

    best_score = -1e9
    best_parts: List[dict] = []
    for start in candidate_starts:
        window = parent_parts[start: start + top_k]
        if not window:
            continue
        in_source_count = 0
        score = 0.0
        for offset, part in enumerate(window):
            value = scores.get(part["part_id"])
            if value is None:
                score -= outside_penalty
                continue
            in_source_count += 1
            score += value - span_penalty * abs(offset - (top_k // 2))
        if in_source_count == 0:
            continue
        if score > best_score:
            best_score = score
            best_parts = list(window)
    return best_score, best_parts


def compact_windows(
    parts: Sequence[dict],
    top_k: int,
    all_by_parent: Dict[str, List[dict]],
    *,
    parent_top_n: int,
    count_weight: float,
    avg_weight: float,
    outside_penalty: float,
    span_penalty: float,
    quota: str,
    keep_direct: int = 0,
) -> List[dict]:
    """
    Return up to `top_k` parts. Sorts parents by composite score (max + count
    + avg), picks the best window inside each top parent, then distributes
    `top_k` slots between the top windows by the `quota` strategy.

    quota values:
        "single" -> all from the best window
        "2-2-1"  -> 2 from best, 2 from second, 1 from third
        "3-1-1"  -> 3 from best, 1 from second, 1 from third
        "round"  -> round-robin one-by-one across windows
    """
    if not parts:
        return []

    scores = _normalize_scores(parts)
    parents = _parent_scores(parts, scores, count_weight, avg_weight)[:parent_top_n]

    parent_windows: List[tuple[str, float, List[dict]]] = []
    for parent_id, parent_score in parents:
        window_score, window = _best_window_for_parent(
            parent_id,
            parts,
            all_by_parent,
            scores,
            top_k,
            outside_penalty,
            span_penalty,
        )
        if window:
            parent_windows.append((parent_id, parent_score + window_score, window))
    parent_windows.sort(key=lambda item: item[1], reverse=True)
    if not parent_windows:
        return list(parts[:top_k])

    selected: List[dict] = []
    seen: set = set()

    for part in parts[:keep_direct]:
        if part["part_id"] in seen:
            continue
        selected.append(part)
        seen.add(part["part_id"])
        if len(selected) >= top_k:
            return selected

    if quota == "single":
        for part in parent_windows[0][2]:
            if part["part_id"] in seen:
                continue
            selected.append(part)
            seen.add(part["part_id"])
            if len(selected) >= top_k:
                return selected
        for part in parts:
            if part["part_id"] in seen:
                continue
            selected.append(part)
            seen.add(part["part_id"])
            if len(selected) >= top_k:
                return selected
        return selected

    quotas = [3, 1, 1] if quota == "3-1-1" else [2, 2, 1] if quota == "2-2-1" else [1] * top_k
    for parent_window, quota_size in itertools.zip_longest(parent_windows, quotas, fillvalue=None):
        if parent_window is None or quota_size is None:
            break
        _parent_id, _score, window = parent_window
        for part in window[:quota_size]:
            if part["part_id"] in seen:
                continue
            selected.append(part)
            seen.add(part["part_id"])
            if len(selected) >= top_k:
                return selected

    for part in parts:
        if part["part_id"] in seen:
            continue
        selected.append(part)
        seen.add(part["part_id"])
        if len(selected) >= top_k:
            return selected
    return selected


def select_vector_window(
    parts: Sequence[dict],
    all_by_parent: Dict[str, List[dict]],
    top_k: int,
) -> List[dict]:
    """
    always_window_2-2-1_keep0_gate0.3 profile from
    the validated mixed-flat winner (parent_top_n=5, outside_penalty=0.0,
    span_penalty=0.0). The `gate0.3` suffix in the profile name belongs to a
    gated_window variant — the always_window flavor used here ignores it.
    """
    return compact_windows(
        parts, top_k, all_by_parent,
        parent_top_n=5,
        count_weight=0.0,
        avg_weight=0.0,
        outside_penalty=0.0,
        span_penalty=0.0,
        quota="2-2-1",
        keep_direct=0,
    )


def select_rlm_window_3600(
    parts: Sequence[dict],
    all_by_parent: Dict[str, List[dict]],
    top_k: int,
) -> List[dict]:
    """rlm_always_window_parents5_cnt0.05_avg0_out0_span0_round_keep0."""
    return compact_windows(
        parts, top_k, all_by_parent,
        parent_top_n=5,
        count_weight=0.05,
        avg_weight=0.0,
        outside_penalty=0.0,
        span_penalty=0.0,
        quota="round",
        keep_direct=0,
    )


def select_rlm_window_2200(
    parts: Sequence[dict],
    all_by_parent: Dict[str, List[dict]],
    top_k: int,
) -> List[dict]:
    """rlm_always_window_parents1_cnt0_avg0_out0_span0_3-1-1_keep0."""
    return compact_windows(
        parts, top_k, all_by_parent,
        parent_top_n=1,
        count_weight=0.0,
        avg_weight=0.0,
        outside_penalty=0.0,
        span_penalty=0.0,
        quota="3-1-1",
        keep_direct=0,
    )
