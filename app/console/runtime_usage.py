"""Console-facing runtime usage aggregation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from config import settings
from runtime_context import get_run_id
import runtime_metrics


EVENT_LABELS = {
    "object_summary.llm": "Генерация сводок объектов метаданных",
    "console_agent.llm": "Агент Метакод",
    "object_summary.embedding": "Индексация сводок объектов метаданных",
    "metadata_description.embedding.index": "Индексация описаний метаданных",
    "routine_description.embedding.index": "Индексация описаний процедур",
    "bsl_code.embedding.index": "Индексация BSL-кода",
    "metadata_description.embedding.query": "Поиск по описаниям метаданных",
    "routine_description.embedding.query": "Поиск по описаниям процедур",
    "object_summary.embedding.query": "Поиск по сводкам объектов метаданных",
    "bsl_code.embedding.query": "Поиск по BSL-коду",
    "bsl_code.rerank": "Реранк BSL-кода",
    "metadata_description.rerank": "Реранк описаний метаданных",
    "object_summary.rerank": "Реранк сводок объектов метаданных",
    "routine_description.rerank": "Реранк описаний процедур",
}

SECTION_TITLES = {
    "llm": "LLM",
    "embeddings": "Embeddings",
    "rerank": "Rerank",
    "mcp_tools": "MCP tools",
    "other": "Прочее",
}
SECTION_ORDER = ("llm", "embeddings", "rerank", "mcp_tools", "other")


class RuntimeUsageUnavailable(RuntimeError):
    pass


def _runtime_db_path() -> Path:
    return Path(settings.runtime_metrics_sqlite_path)


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runtime_usage_totals'"
    ).fetchone()
    return bool(row)


def _section_for_event(event_type: str) -> str:
    if event_type.startswith("mcp.tool."):
        return "mcp_tools"
    if event_type == "rerank" or event_type.endswith(".rerank"):
        return "rerank"
    if ".embedding" in event_type or event_type.endswith(".embedding"):
        return "embeddings"
    if ".llm" in event_type or event_type.endswith(".llm"):
        return "llm"
    return "other"


def _label_for_event(event_type: str) -> str:
    if event_type.startswith("mcp.tool."):
        return event_type.removeprefix("mcp.tool.")
    return EVENT_LABELS.get(event_type, event_type)


def _add_optional(a: int | None, b: Any) -> int | None:
    if b is None:
        return a
    try:
        value = int(b)
    except (TypeError, ValueError):
        return a
    if a is None:
        return value
    return a + value


def _provider_for_row(row: sqlite3.Row) -> str:
    provider = str(row["provider"] or "unknown")
    if provider != "unknown":
        return provider
    event_type = str(row["event_type"] or "")
    if event_type.startswith("object_summary.embedding"):
        return runtime_metrics.detect_provider_from_api_base(
            settings.embedding_api_base,
            fallback=provider,
        )
    return provider


def _empty_bucket(event_type: str | None = None) -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "calls": 0,
        "successes": 0,
        "failures": 0,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "duration_ms_total": 0,
        "_costs": {},
        "_models": {},
        "first_seen_at": None,
        "last_seen_at": None,
    }
    if event_type is not None:
        bucket["event_type"] = event_type
        bucket["label"] = _label_for_event(event_type)
    return bucket


def _merge_row(bucket: dict[str, Any], row: sqlite3.Row) -> None:
    calls = int(row["calls"] or 0)
    bucket["calls"] += calls
    bucket["successes"] += int(row["successes"] or 0)
    bucket["failures"] += int(row["failures"] or 0)
    bucket["input_tokens"] = _add_optional(bucket["input_tokens"], row["input_tokens"])
    bucket["output_tokens"] = _add_optional(bucket["output_tokens"], row["output_tokens"])
    bucket["total_tokens"] = _add_optional(bucket["total_tokens"], row["total_tokens"])
    bucket["duration_ms_total"] += int(row["duration_ms_total"] or 0)

    cost = row["cost_amount"]
    if cost is not None:
        cost_key = (str(row["cost_source"] or "unknown"), str(row["cost_unit"] or ""))
        bucket["_costs"][cost_key] = bucket["_costs"].get(cost_key, 0.0) + float(cost)

    model_key = (_provider_for_row(row), str(row["model"] or "unknown"))
    model_entry = bucket["_models"].setdefault(
        model_key,
        {"provider": model_key[0], "model": model_key[1], "calls": 0},
    )
    model_entry["calls"] += calls

    first_seen = row["first_seen_at"]
    last_seen = row["last_seen_at"]
    if first_seen and (bucket["first_seen_at"] is None or first_seen < bucket["first_seen_at"]):
        bucket["first_seen_at"] = first_seen
    if last_seen and (bucket["last_seen_at"] is None or last_seen > bucket["last_seen_at"]):
        bucket["last_seen_at"] = last_seen


def _merge_model_child(bucket: dict[str, Any], row: sqlite3.Row) -> None:
    model_key = (_provider_for_row(row), str(row["model"] or "unknown"))
    children = bucket.setdefault("_children", {})
    child = children.get(model_key)
    if child is None:
        child = _empty_bucket(str(row["event_type"] or "other"))
        child["provider"] = model_key[0]
        child["model"] = model_key[1]
        child["label"] = f"{model_key[0]} · {model_key[1]}"
        children[model_key] = child
    _merge_row(child, row)


def _finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    calls = int(bucket["calls"] or 0)
    costs = [
        {"source": source, "unit": unit or None, "amount": amount}
        for (source, unit), amount in bucket.pop("_costs").items()
    ]
    costs.sort(key=lambda item: (item["unit"] or "", item["source"]))
    models = list(bucket.pop("_models").values())
    models.sort(key=lambda item: (-int(item["calls"] or 0), item["provider"], item["model"]))
    bucket["costs"] = costs
    bucket["models"] = models
    children = [
        _finalize_bucket(child)
        for child in bucket.pop("_children", {}).values()
    ]
    children.sort(key=lambda item: (-int(item["calls"] or 0), item.get("provider") or "", item.get("model") or ""))
    bucket["children"] = children
    bucket["avg_duration_ms"] = (bucket["duration_ms_total"] / calls) if calls else 0
    return bucket


def _empty_response(scope: str, reason: str | None = None) -> dict[str, Any]:
    sections = [
        {
            "key": key,
            "title": SECTION_TITLES[key],
            "totals": _finalize_bucket(_empty_bucket()),
            "items": [],
        }
        for key in SECTION_ORDER
        if key != "other"
    ]
    return {
        "available": reason is None,
        "reason": reason,
        "project_name": settings.project_name,
        "scope": scope,
        "current_run_id": get_run_id(),
        "sections": sections,
        "last_seen_at": None,
    }


def get_runtime_usage(scope: str = "all") -> dict[str, Any]:
    scope = (scope or "all").strip().lower()
    if scope not in {"all", "current"}:
        raise ValueError("scope must be all or current")

    db_path = _runtime_db_path()
    if not db_path.exists():
        return _empty_response(scope, "not_found")

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not _table_exists(conn):
                return _empty_response(scope, "table_missing")
            params: list[Any] = [settings.project_name]
            where = "project_name = ?"
            current_run_id = get_run_id()
            if scope == "current":
                where += " AND run_id = ?"
                params.append(current_run_id)
            rows = conn.execute(
                f"""
                SELECT run_id, project_name, event_type, provider, model, cost_source, cost_unit,
                       calls, successes, failures,
                       input_tokens, output_tokens, total_tokens, cost_amount,
                       duration_ms_total, first_seen_at, last_seen_at
                  FROM runtime_usage_totals
                 WHERE {where}
                 ORDER BY last_seen_at DESC
                """,
                params,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        raise RuntimeUsageUnavailable(str(exc) or "runtime usage unavailable") from exc

    section_totals = {key: _empty_bucket() for key in SECTION_ORDER}
    items_by_event: dict[str, dict[str, Any]] = {}
    last_seen_at = None

    for row in rows:
        event_type = str(row["event_type"] or "other")
        section_key = _section_for_event(event_type)
        bucket = items_by_event.setdefault(event_type, _empty_bucket(event_type))
        _merge_row(bucket, row)
        if section_key == "llm":
            _merge_model_child(bucket, row)
        _merge_row(section_totals[section_key], row)
        row_last_seen = row["last_seen_at"]
        if row_last_seen and (last_seen_at is None or row_last_seen > last_seen_at):
            last_seen_at = row_last_seen

    sections = []
    for key in SECTION_ORDER:
        section_items = [
            _finalize_bucket(item)
            for event_type, item in items_by_event.items()
            if _section_for_event(event_type) == key
        ]
        section_items.sort(key=lambda item: (-int(item["calls"] or 0), item["label"]))
        if key == "other" and not section_items:
            continue
        sections.append({
            "key": key,
            "title": SECTION_TITLES[key],
            "totals": _finalize_bucket(section_totals[key]),
            "items": section_items,
        })

    return {
        "available": True,
        "reason": None,
        "project_name": settings.project_name,
        "scope": scope,
        "current_run_id": get_run_id(),
        "sections": sections,
        "last_seen_at": last_seen_at,
    }
