"""Shared Phase B usage/progress metrics for BSL code search.

Single source of truth for the Phase B stats model, token/cost formatting and
the item/time/final progress trigger, reused by both the ordinary final Phase B
(`bsl_code_indexer._PhaseBProgress`) and the startup overlap Phase B
(`bsl_code_phase_b_overlap`). Keeping it here avoids two divergent copies of the
cost/token rules and lets the overlap controller depend on the stats model
without importing the heavy `bsl_code_indexer` module.

`bsl_code_indexer` re-imports these names at module scope so existing imports
(`from graphdb.bsl_code_indexer import _PhaseBStats`, monkeypatch of
`_ProgressLogger` by name) keep working.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


BSL_PHASE_B_PROGRESS_UNITS = 100
BSL_PROGRESS_SECONDS = 30.0


def _format_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {sec}s"


def _format_usage_tokens(value: Optional[int]) -> str:
    """Token count for logs; missing usage surfaces as 'unknown' (mirrors the
    ObjectSummary S2 log model in object_summary_pipeline._format_usage_tokens)."""
    return str(int(value)) if isinstance(value, int) and value >= 0 else "unknown"


def _format_cost(amount: Optional[float], unit: Optional[str], source: str) -> str:
    """Provider-reported cost for logs; anything else is 'unknown' (mirrors
    object_summary_pipeline._format_cost)."""
    if source != "provider_reported" or amount is None:
        return "unknown"
    unit_label = (unit or "").strip() or "units"
    return f"{amount:.4f} {unit_label}"


def _add_optional_tokens(a: Optional[int], b: Optional[int]) -> Optional[int]:
    """Sum two optional token counts. None + None stays None (usage never
    reported), so 'unknown' does not collapse into 0."""
    if a is None and b is None:
        return None
    return int(a or 0) + int(b or 0)


class _ProgressLogger:
    def __init__(
        self,
        label: str,
        total: int,
        item_interval: int,
        seconds_interval: float = BSL_PROGRESS_SECONDS,
        item_name: str = "routines",
        log: Optional[logging.Logger] = None,
    ) -> None:
        self.label = label
        self.total = max(0, int(total))
        self.item_interval = max(1, int(item_interval))
        self.seconds_interval = max(1.0, float(seconds_interval))
        self.item_name = item_name
        # Callers inject their own module logger so progress records keep the
        # namespace their tests/log filters expect (e.g. Phase B under
        # graphdb.bsl_code_indexer, overlap under graphdb.bsl_code_phase_b_overlap).
        self._log = log or logger
        self.started_at = time.monotonic()
        self.last_logged_at = self.started_at
        self.last_logged_items = 0

    def maybe_log(self, processed: int, *, final: bool = False, **stats: Any) -> None:
        processed = max(0, int(processed))
        now = time.monotonic()
        item_due = processed - self.last_logged_items >= self.item_interval
        time_due = now - self.last_logged_at >= self.seconds_interval
        if not final and not item_due and not time_due:
            return

        pct = (processed / self.total * 100.0) if self.total > 0 else 0.0
        stats_text = ", ".join(
            f"{key}={value}" for key, value in stats.items() if value is not None
        )
        suffix = f", {stats_text}" if stats_text else ""
        self._log.info(
            "%s: processed %d/%d %s (%.1f%%)%s, elapsed=%s",
            self.label,
            processed,
            self.total,
            self.item_name,
            pct,
            suffix,
            _format_elapsed(now - self.started_at),
        )
        self.last_logged_items = processed
        self.last_logged_at = now


@dataclasses.dataclass
class _PhaseBStats:
    units_requested: int = 0
    units_prepared: int = 0
    units_written: int = 0
    skipped_missing_body: int = 0
    skipped_hash_mismatch: int = 0
    skipped_empty_text: int = 0
    batches: int = 0
    # Embedding usage accumulated from EmbeddingBatchResult (only for batches
    # that actually reached the embedding API). input/total tokens stay None
    # until a provider reports them, so 'unknown' does not collapse into 0.
    embedding_api_calls: int = 0
    input_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    # Provider-reported cost keyed by (cost_source, cost_unit); mirrors
    # object_summary_pipeline._PhaseTotals.cost_by_unit so mixed units don't
    # silently sum into one wrong number.
    cost_by_unit: Dict[Tuple[str, str], float] = dataclasses.field(
        default_factory=dict
    )

    def add(self, other: "_PhaseBStats") -> None:
        self.units_requested += int(other.units_requested)
        self.units_prepared += int(other.units_prepared)
        self.units_written += int(other.units_written)
        self.skipped_missing_body += int(other.skipped_missing_body)
        self.skipped_hash_mismatch += int(other.skipped_hash_mismatch)
        self.skipped_empty_text += int(other.skipped_empty_text)
        self.batches += int(other.batches)
        self.embedding_api_calls += int(other.embedding_api_calls)
        self.input_tokens = _add_optional_tokens(
            self.input_tokens, other.input_tokens
        )
        self.total_tokens = _add_optional_tokens(
            self.total_tokens, other.total_tokens
        )
        for key, amount in other.cost_by_unit.items():
            self.cost_by_unit[key] = self.cost_by_unit.get(key, 0.0) + float(amount)

    def add_cost(
        self, amount: Optional[float], unit: Optional[str], source: str,
    ) -> None:
        """Accumulate provider-reported cost only; unknown/None sources are
        ignored so they never masquerade as 0.0000 credits."""
        if amount is None or source != "provider_reported":
            return
        key = (source, (unit or "").strip())
        self.cost_by_unit[key] = self.cost_by_unit.get(key, 0.0) + float(amount)

    def primary_cost(self) -> Tuple[Optional[float], Optional[str], str]:
        """(amount, unit, source) for the single cost line shown in logs. Sum
        per unit; on mixed units surface the largest. Empty -> unknown."""
        if not self.cost_by_unit:
            return None, None, "unknown"
        unit, amount = max(self.cost_by_unit.items(), key=lambda kv: kv[1])
        return amount, unit[1] or None, unit[0]

    def copy(self) -> "_PhaseBStats":
        # Deep-copy cost_by_unit: final() hands the snapshot to a caller while
        # the live accumulator keeps mutating; a shared dict would leak writes.
        return dataclasses.replace(self, cost_by_unit=dict(self.cost_by_unit))
