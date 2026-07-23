"""
In-memory progress state for the bootstrap/incremental indexing pipeline,
surfaced by the anonymous `GET /api/console/metrics/index` Prometheus
exposition endpoint (TASK-index-progress.md, monitoring phase 2 follow-up
to `/api/console/health/index`).

Several indexing pipelines can genuinely run concurrently in this process —
`mcpsrv/server.py` starts vector embedding, the BSL code-search sidecar and
object-summary generation as independent daemon threads without waiting on
each other, and the incremental scheduler can tick while any of those are
still finishing up. The metrics contract wants exactly one active phase
reported at a time, so this module tracks the *set* of phases currently in
progress and reports the oldest still-running one — the longest-running
phase is the most plausible bottleneck for overall readiness, and picking a
stable choice (rather than whichever last called begin_phase) avoids the
reported phase flapping every time a short-lived phase starts or ends.

Item-level counters are pull-based: callers pass zero-arg getters that read
already-maintained in-memory state (e.g. `BSLProcessor.bsl_parsed_count`)
rather than pushing an update per item, so scraping this module never adds
work proportional to the amount of data processed — only the scrape itself
calls the getters, at most once per request.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

_lock = threading.RLock()


@dataclass
class _PhaseState:
    started_at: float
    processed_getter: Optional[Callable[[], Optional[float]]] = None
    total_getter: Optional[Callable[[], Optional[float]]] = None
    total: Optional[float] = None  # static fallback when total_getter is None


_active: Dict[str, _PhaseState] = {}


def begin_phase(
    phase: str,
    *,
    processed_getter: Optional[Callable[[], Optional[float]]] = None,
    total_getter: Optional[Callable[[], Optional[float]]] = None,
    total: Optional[float] = None,
) -> None:
    """Mark `phase` as in progress from now on.

    Safe to call again for a phase that's already active (e.g. to attach
    getters once the relevant workers exist) — this restarts its start time,
    which is fine: it only affects which concurrently-active phase is picked
    as "oldest" and the ETA calculation for THIS phase, not correctness.
    """
    with _lock:
        _active[phase] = _PhaseState(
            started_at=time.monotonic(),
            processed_getter=processed_getter,
            total_getter=total_getter,
            total=total,
        )


def end_phase(phase: str) -> None:
    """Mark `phase` as no longer in progress.

    No-op if `phase` isn't active — always safe to call unconditionally from
    a `finally` block even on a path that never reached `begin_phase`.
    """
    with _lock:
        _active.pop(phase, None)


def is_phase_active(phase: str) -> bool:
    with _lock:
        return phase in _active


def snapshot() -> dict:
    """Read live progress for the current bottleneck phase (the oldest one
    still active), calling its getters now.

    Returns a dict with keys `phase`, `processed`, `total`, `ratio`,
    `eta_seconds` — any of them may be None (nothing active / not tracked /
    not computable yet). Getter exceptions are swallowed: a broken getter
    must never turn a metrics scrape into a 500.
    """
    with _lock:
        if not _active:
            return {"phase": None, "processed": None, "total": None, "ratio": None, "eta_seconds": None}
        phase, state = min(_active.items(), key=lambda kv: kv[1].started_at)
        started_at = state.started_at
        processed_getter = state.processed_getter
        total_getter = state.total_getter
        static_total = state.total

    processed: Optional[float] = None
    if processed_getter is not None:
        try:
            processed = processed_getter()
        except Exception:
            logger.warning("index_progress: processed_getter raised, ignoring this sample", exc_info=True)
            processed = None

    total: Optional[float] = static_total
    if total_getter is not None:
        try:
            total = total_getter()
        except Exception:
            logger.warning("index_progress: total_getter raised, ignoring this sample", exc_info=True)
            total = static_total

    ratio: Optional[float] = None
    eta_seconds: Optional[int] = None
    if (
        isinstance(processed, (int, float))
        and isinstance(total, (int, float))
        and total > 0
    ):
        ratio = max(0.0, min(1.0, processed / total))
        elapsed = time.monotonic() - started_at
        if processed > 0 and elapsed > 1.0:
            rate = processed / elapsed
            if rate > 0:
                remaining = max(0.0, total - processed)
                eta_seconds = int(remaining / rate)

    return {
        "phase": phase,
        "processed": processed,
        "total": total,
        "ratio": ratio,
        "eta_seconds": eta_seconds,
    }
