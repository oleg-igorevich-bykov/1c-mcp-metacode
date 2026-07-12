"""Best-effort runtime metrics for embedding operations."""

from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

import runtime_metrics


def format_usage_tokens(value: Optional[int]) -> str:
    """Token count for progress logs; missing usage surfaces as 'unknown'
    (mirrors the bsl_code_indexer / object_summary_pipeline usage log model)."""
    return str(int(value)) if isinstance(value, int) and value >= 0 else "unknown"


def format_cost(amount: Optional[float], unit: Optional[str], source: str) -> str:
    """Provider-reported cost for progress logs; anything else is 'unknown'."""
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


@dataclasses.dataclass
class EmbeddingUsageStats:
    """Live usage/cost accumulator for embedding progress logs.

    Cost is kept per (cost_source, cost_unit) so mixed units are never summed
    into one number; primary_cost() surfaces the dominant one, matching the
    Phase B (_PhaseBStats) and Object Summary (_PhaseTotals) log model. This is
    a live-log counter only and does not write to runtime_metrics — that stays
    owned by record_result(...)."""

    embedding_api_calls: int = 0
    input_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_by_unit: Dict[Tuple[str, str], float] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_result(cls, result: Any) -> "EmbeddingUsageStats":
        """Snap usage off an EmbeddingBatchResult (or the SimpleNamespace
        fallback). api_calls is taken raw (0 if unreported), matching how
        Phase B records batch_result.api_calls."""
        stats = cls(
            embedding_api_calls=int(getattr(result, "api_calls", 0) or 0),
            input_tokens=getattr(result, "input_tokens", None),
            total_tokens=getattr(result, "total_tokens", None),
        )
        stats.add_cost(
            getattr(result, "cost_amount", None),
            getattr(result, "cost_unit", None),
            str(getattr(result, "cost_source", "") or "unknown"),
        )
        return stats

    def add(self, other: "EmbeddingUsageStats") -> None:
        self.embedding_api_calls += other.embedding_api_calls
        self.input_tokens = _add_optional_tokens(self.input_tokens, other.input_tokens)
        self.total_tokens = _add_optional_tokens(self.total_tokens, other.total_tokens)
        for key, amount in other.cost_by_unit.items():
            self.cost_by_unit[key] = self.cost_by_unit.get(key, 0.0) + float(amount)

    def add_cost(self, amount: Optional[float], unit: Optional[str], source: str) -> None:
        if amount is None or source != "provider_reported":
            return
        key = (source, (unit or "").strip())
        self.cost_by_unit[key] = self.cost_by_unit.get(key, 0.0) + float(amount)

    def primary_cost(self) -> Tuple[Optional[float], Optional[str], str]:
        if not self.cost_by_unit:
            return None, None, "unknown"
        key, amount = max(self.cost_by_unit.items(), key=lambda kv: kv[1])
        return amount, key[1] or None, key[0]

    def copy(self) -> "EmbeddingUsageStats":
        return dataclasses.replace(self, cost_by_unit=dict(self.cost_by_unit))


def started() -> float:
    return time.perf_counter()


def elapsed_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


def provider_for_service(embedding_service: Any) -> str:
    return runtime_metrics.detect_provider_from_api_base(
        getattr(embedding_service, "api_base", None),
        fallback="unknown",
    )


def model_for_service(embedding_service: Any) -> str:
    return str(getattr(embedding_service, "model", "") or "unknown")


def _result_from_embeddings(embeddings: list[Any]) -> Any:
    return SimpleNamespace(
        embeddings=embeddings,
        input_tokens=None,
        total_tokens=None,
        cost_amount=None,
        cost_unit=None,
        cost_source="unknown",
        api_calls=1 if embeddings else 0,
    )


def call_batched_with_usage(
    embedding_service: Any,
    texts: list[str],
    *,
    format_spec: Any,
) -> Any:
    method = getattr(embedding_service, "get_embeddings_batched_with_usage", None)
    if callable(method):
        return method(texts, format_spec=format_spec)
    embeddings = embedding_service.get_embeddings_batched(texts, format_spec=format_spec)
    return _result_from_embeddings(embeddings)


def call_single_with_usage(
    embedding_service: Any,
    text: str,
    *,
    format_spec: Any,
) -> Any:
    method = getattr(embedding_service, "get_embeddings_with_usage", None)
    if callable(method):
        return method([text], format_spec=format_spec)
    embeddings = embedding_service.get_embeddings([text], format_spec=format_spec)
    return _result_from_embeddings(embeddings)


def record_result(
    *,
    event_type: str,
    embedding_service: Any,
    result: Any,
    duration_ms: int,
    success: bool = True,
) -> None:
    runtime_metrics.record_embedding_usage(
        event_type=event_type,
        provider=provider_for_service(embedding_service),
        model=model_for_service(embedding_service),
        calls=max(1, int(getattr(result, "api_calls", 0) or 1)),
        success=success,
        input_tokens=getattr(result, "input_tokens", None),
        total_tokens=getattr(result, "total_tokens", None),
        cost_amount=getattr(result, "cost_amount", None),
        cost_unit=getattr(result, "cost_unit", None),
        cost_source=str(getattr(result, "cost_source", "") or "unknown"),
        duration_ms=duration_ms,
    )


def record_failure(
    *,
    event_type: str,
    embedding_service: Any,
    duration_ms: int,
) -> None:
    runtime_metrics.record_embedding_usage(
        event_type=event_type,
        provider=provider_for_service(embedding_service),
        model=model_for_service(embedding_service),
        calls=1,
        success=False,
        duration_ms=duration_ms,
    )


def first_embedding(result: Any) -> Optional[list[float]]:
    embeddings = getattr(result, "embeddings", None)
    if not embeddings:
        return None
    first = embeddings[0]
    return list(first) if first is not None else None
