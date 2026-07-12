"""Per-process aggregated usage/cost in a local SQLite file.

Aggregates only, no raw events. One row per
`(run_id, project_name, event_type, provider, model, cost_source, cost_unit, actor_id)`.

`cost_unit` is stored as a non-NULL TEXT (empty string when the provider did
not report a unit). NULLs cannot participate in SQLite UNIQUE constraints,
so collapsing them into `''` is what keeps multiple flushes from spawning a
new row each time.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from config import settings
from runtime_context import get_run_id

logger = logging.getLogger(__name__)


@dataclass
class UsageDelta:
    calls: int = 0
    successes: int = 0
    failures: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_amount: Optional[float] = None
    duration_ms_total: int = 0


_LOCK = threading.Lock()
_INITIALISED_PATH: Optional[Path] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_path() -> Path:
    return Path(settings.runtime_metrics_sqlite_path)


def _table_exists(conn: sqlite3.Connection, table_name: str = "runtime_usage_totals") -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _usage_columns(conn: sqlite3.Connection, table_name: str = "runtime_usage_totals") -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _create_usage_table(conn: sqlite3.Connection, table_name: str = "runtime_usage_totals") -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            project_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            actor_id TEXT NOT NULL DEFAULT '',
            actor_login TEXT NOT NULL DEFAULT '',
            cost_source TEXT NOT NULL,
            cost_unit TEXT NOT NULL DEFAULT '',
            calls INTEGER NOT NULL DEFAULT 0,
            successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cost_amount REAL,
            duration_ms_total INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE(run_id, project_name, event_type, provider, model, cost_source, cost_unit, actor_id)
        )
        """
    )


def _migrate_usage_table_for_actor(conn: sqlite3.Connection) -> None:
    columns = _usage_columns(conn)
    if {"actor_id", "actor_login"}.issubset(columns):
        return

    new_table = "runtime_usage_totals_new"
    conn.execute(f"DROP TABLE IF EXISTS {new_table}")
    _create_usage_table(conn, new_table)
    conn.execute(
        f"""
        INSERT INTO {new_table} (
            id, run_id, project_name, event_type, provider, model,
            actor_id, actor_login, cost_source, cost_unit,
            calls, successes, failures,
            input_tokens, output_tokens, total_tokens, cost_amount,
            duration_ms_total, first_seen_at, last_seen_at
        )
        SELECT
            id, run_id, project_name, event_type, provider, model,
            '', '', cost_source, cost_unit,
            calls, successes, failures,
            input_tokens, output_tokens, total_tokens, cost_amount,
            duration_ms_total, first_seen_at, last_seen_at
          FROM runtime_usage_totals
        """
    )
    conn.execute("DROP TABLE runtime_usage_totals")
    conn.execute(f"ALTER TABLE {new_table} RENAME TO runtime_usage_totals")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn):
        _create_usage_table(conn)
    else:
        _migrate_usage_table_for_actor(conn)
    conn.commit()


def _open_connection() -> sqlite3.Connection:
    global _INITIALISED_PATH
    path = _resolve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    if _INITIALISED_PATH != path:
        _ensure_schema(conn)
        _INITIALISED_PATH = path
    return conn


def flush_delta(
    *,
    event_type: str,
    provider: str,
    model: str,
    cost_source: str,
    cost_unit: Optional[str],
    delta: UsageDelta,
    actor_id: Optional[str] = None,
    actor_login: Optional[str] = None,
) -> None:
    if delta.calls <= 0 and delta.successes <= 0 and delta.failures <= 0:
        # nothing to record
        return
    unit_key = (cost_unit or "").strip()
    actor_id_key = (actor_id or "").strip()
    actor_login_value = (actor_login or "").strip()
    now = _now_iso()
    project_name = settings.project_name
    run_id = get_run_id()
    try:
        with _LOCK:
            conn = _open_connection()
            try:
                conn.execute(
                    """
                    INSERT INTO runtime_usage_totals (
                        run_id, project_name, event_type, provider, model,
                        actor_id, actor_login, cost_source, cost_unit,
                        calls, successes, failures,
                        input_tokens, output_tokens, total_tokens, cost_amount,
                        duration_ms_total, first_seen_at, last_seen_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?
                    )
                    ON CONFLICT(run_id, project_name, event_type, provider, model, cost_source, cost_unit, actor_id)
                    DO UPDATE SET
                        actor_login = excluded.actor_login,
                        calls = calls + excluded.calls,
                        successes = successes + excluded.successes,
                        failures = failures + excluded.failures,
                        input_tokens = CASE
                            WHEN input_tokens IS NULL AND excluded.input_tokens IS NULL THEN NULL
                            ELSE COALESCE(input_tokens, 0) + COALESCE(excluded.input_tokens, 0)
                        END,
                        output_tokens = CASE
                            WHEN output_tokens IS NULL AND excluded.output_tokens IS NULL THEN NULL
                            ELSE COALESCE(output_tokens, 0) + COALESCE(excluded.output_tokens, 0)
                        END,
                        total_tokens = CASE
                            WHEN total_tokens IS NULL AND excluded.total_tokens IS NULL THEN NULL
                            ELSE COALESCE(total_tokens, 0) + COALESCE(excluded.total_tokens, 0)
                        END,
                        cost_amount = CASE
                            WHEN cost_amount IS NULL AND excluded.cost_amount IS NULL THEN NULL
                            ELSE COALESCE(cost_amount, 0) + COALESCE(excluded.cost_amount, 0)
                        END,
                        duration_ms_total = duration_ms_total + excluded.duration_ms_total,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        run_id, project_name, event_type, provider, model,
                        actor_id_key, actor_login_value, cost_source, unit_key,
                        delta.calls, delta.successes, delta.failures,
                        delta.input_tokens, delta.output_tokens, delta.total_tokens, delta.cost_amount,
                        delta.duration_ms_total, now, now,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        logger.warning("runtime_metrics flush failed: %s", exc)


def _clean_text(value: Optional[str], fallback: str) -> str:
    text = str(value or "").strip()
    return text or fallback


def _positive_int(value: Optional[int], default: int = 0) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def detect_provider_from_api_base(api_base: Optional[str], *, fallback: str = "unknown") -> str:
    host = (urlparse(str(api_base or "")).hostname or "").lower()
    if not host:
        return fallback
    if "openrouter.ai" in host:
        return "openrouter"
    if "openai.com" in host:
        return "openai"
    if "googleapis.com" in host or "generativelanguage" in host:
        return "google"
    return host


def record_llm_usage(
    *,
    event_type: str,
    provider: str,
    model: str,
    calls: int = 1,
    success: bool = True,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_amount: Optional[float] = None,
    cost_unit: Optional[str] = None,
    cost_source: str = "unknown",
    duration_ms: int = 0,
    actor_id: Optional[str] = None,
    actor_login: Optional[str] = None,
) -> None:
    n_calls = _positive_int(calls, 1)
    flush_delta(
        event_type=_clean_text(event_type, "llm"),
        provider=_clean_text(provider, "unknown"),
        model=_clean_text(model, "unknown"),
        cost_source=_clean_text(cost_source, "unknown"),
        cost_unit=cost_unit,
        actor_id=actor_id,
        actor_login=actor_login,
        delta=UsageDelta(
            calls=n_calls,
            successes=n_calls if success else 0,
            failures=0 if success else n_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_amount=cost_amount,
            duration_ms_total=max(0, int(duration_ms or 0)),
        ),
    )


def record_embedding_usage(
    *,
    event_type: str,
    provider: str,
    model: str,
    calls: int = 1,
    success: bool = True,
    input_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_amount: Optional[float] = None,
    cost_unit: Optional[str] = None,
    cost_source: str = "unknown",
    duration_ms: int = 0,
    actor_id: Optional[str] = None,
    actor_login: Optional[str] = None,
) -> None:
    n_calls = _positive_int(calls, 1)
    flush_delta(
        event_type=_clean_text(event_type, "embedding"),
        provider=_clean_text(provider, "unknown"),
        model=_clean_text(model, "unknown"),
        cost_source=_clean_text(cost_source, "unknown"),
        cost_unit=cost_unit,
        actor_id=actor_id,
        actor_login=actor_login,
        delta=UsageDelta(
            calls=n_calls,
            successes=n_calls if success else 0,
            failures=0 if success else n_calls,
            input_tokens=input_tokens,
            output_tokens=None,
            total_tokens=total_tokens,
            cost_amount=cost_amount,
            duration_ms_total=max(0, int(duration_ms or 0)),
        ),
    )


def record_rerank_usage(
    *,
    event_type: str,
    provider: str,
    model: str,
    calls: int = 1,
    success: bool = True,
    input_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    cost_amount: Optional[float] = None,
    cost_unit: Optional[str] = None,
    cost_source: str = "unknown",
    duration_ms: int = 0,
    actor_id: Optional[str] = None,
    actor_login: Optional[str] = None,
) -> None:
    n_calls = _positive_int(calls, 1)
    flush_delta(
        event_type=_clean_text(event_type, "rerank"),
        provider=_clean_text(provider, "unknown"),
        model=_clean_text(model, "unknown"),
        cost_source=_clean_text(cost_source, "unknown"),
        cost_unit=cost_unit,
        actor_id=actor_id,
        actor_login=actor_login,
        delta=UsageDelta(
            calls=n_calls,
            successes=n_calls if success else 0,
            failures=0 if success else n_calls,
            input_tokens=input_tokens,
            output_tokens=None,
            total_tokens=total_tokens,
            cost_amount=cost_amount,
            duration_ms_total=max(0, int(duration_ms or 0)),
        ),
    )


def record_mcp_tool_call(*, tool_name: str, success: bool, duration_ms: int = 0) -> None:
    clean_tool_name = _clean_text(tool_name, "unknown")
    flush_delta(
        event_type=f"mcp.tool.{clean_tool_name}",
        provider="local",
        model="fastmcp",
        cost_source="none",
        cost_unit=None,
        delta=UsageDelta(
            calls=1,
            successes=1 if success else 0,
            failures=0 if success else 1,
            duration_ms_total=max(0, int(duration_ms or 0)),
        ),
    )
