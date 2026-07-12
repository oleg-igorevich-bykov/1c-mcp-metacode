"""Process-wide startup readiness state for the MCP server.

Owns a tiny lifecycle gate used by the web console (manual object_summary
job runner, status endpoint). The readiness barrier is independent from
the incremental scheduler barrier — it always flips to "ready" once all
registered startup tasks complete, regardless of INCREMENTAL_LOADING_*.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional


_lock = threading.RLock()
_status: str = "starting"
_active: set[str] = set()
_ready_at: Optional[str] = None
_reason: str = "startup indexers are still running"

# Current-state degraded reasons keyed by feature. Non-fatal: status stays
# "ready" even when non-empty. Each key has one writer (startup probe) and one
# clearer (the owning consumer on a successful pass), so recovery within the
# same process removes the reason rather than leaving it stale.
_degraded_reasons: dict[str, str] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def register_startup_task(name: str) -> None:
    with _lock:
        _active.add(name)


def unregister_startup_task(name: str) -> None:
    with _lock:
        _active.discard(name)


def mark_startup_ready(reason: str = "") -> None:
    global _status, _ready_at, _reason
    with _lock:
        if _status == "failed":
            # failed терминален в рамках процесса: барьер не должен затирать
            # уже зафиксированный outer-guard'ом failure-сигнал.
            return
        _status = "ready"
        _ready_at = _now_iso()
        _reason = reason or ""


def mark_startup_failed(reason: str) -> None:
    """Перевод lifecycle в терминальный сбой; вызывается outer-guard daemon-нити.

    Не блокирует обычные MCP tools — это публичный operator-сигнал, который
    оператор видит через `/api/console/health`. После failed обратно в
    starting/ready не возвращаемся в рамках одного процесса (см. mark_startup_ready).
    """
    global _status, _ready_at, _reason
    with _lock:
        _status = "failed"
        _ready_at = _now_iso()
        _reason = reason or "startup pipeline failed"


def set_degraded_reason(key: str, reason: str) -> None:
    """Record a current degraded reason for `key` (deduped by key).

    Non-fatal observability signal surfaced via the health payload; does not
    change readiness. Used for embedding phases that lack their own persistent
    status (routine/metadata descriptions, object summary). BSL is intentionally
    excluded — its degradation is owned by the sidecar `vector_status`.
    """
    with _lock:
        _degraded_reasons[key] = reason or ""


def clear_degraded_reason(key: str) -> None:
    """Clear a degraded reason on a successful pass (recovery)."""
    with _lock:
        _degraded_reasons.pop(key, None)


def get_degraded_reasons() -> dict:
    with _lock:
        return dict(_degraded_reasons)


def get_state() -> dict:
    with _lock:
        return {
            "status": _status,
            "active_startup_tasks": sorted(_active),
            "ready_at": _ready_at,
            "reason": _reason,
            "degraded_reasons": dict(_degraded_reasons),
        }


def is_ready() -> bool:
    with _lock:
        return _status == "ready"
