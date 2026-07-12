"""Persistent chat catalog and transcript storage for the console agent."""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from config import settings

_CHATS_TABLE = "console_agent_chats"
_TURNS_TABLE = "console_agent_chat_turns"
_EVENTS_TABLE = "console_agent_chat_turn_events"
_CHAT_ID_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE_RE = re.compile(r"\s+")
_MARKDOWN_RE = re.compile(r"[`*_#>\[\]()]")
_ENSURED_DB_PATHS: set[str] = set()


class AgentChatError(RuntimeError):
    """Base class for console-agent chat storage errors."""


class AgentChatNotFound(AgentChatError):
    """Raised when a chat does not exist for the current user."""


class AgentChatRunning(AgentChatError):
    """Raised when an operation cannot be applied to a running chat."""


def _db_path() -> Path:
    return Path(settings.console_agent_chats_sqlite_path)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def _utc_now_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def max_chats_per_user() -> int:
    try:
        value = int(settings.console_agent_max_chats_per_user or 100)
    except (TypeError, ValueError):
        value = 100
    return max(1, value)


def _ensure_db(conn: sqlite3.Connection) -> None:
    db_key = str(_db_path())
    if db_key in _ENSURED_DB_PATHS:
        return
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_CHATS_TABLE} (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            login TEXT,
            title TEXT NOT NULL,
            preview TEXT,
            message_count INTEGER NOT NULL DEFAULT 0,
            llm_profile_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_message_at TEXT
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TURNS_TABLE} (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('running', 'done', 'stopped', 'error')),
            user_text TEXT NOT NULL,
            assistant_text TEXT,
            reasoning_text TEXT,
            plan_json TEXT,
            tool_events_json TEXT,
            notices_json TEXT,
            usage_json TEXT,
            llm_profile_id TEXT,
            llm_endpoint_id TEXT,
            llm_model TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            last_event_at TEXT,
            stop_requested_at TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            completed_at TEXT,
            UNIQUE(chat_id, seq),
            FOREIGN KEY(chat_id) REFERENCES {_CHATS_TABLE}(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_EVENTS_TABLE} (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(turn_id, seq),
            FOREIGN KEY(turn_id) REFERENCES {_TURNS_TABLE}(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_console_agent_chats_user_last
            ON {_CHATS_TABLE}(user_id, last_message_at DESC, updated_at DESC)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_console_agent_turns_chat_seq
            ON {_TURNS_TABLE}(chat_id, seq)
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_console_agent_turn_events_turn_seq
            ON {_EVENTS_TABLE}(turn_id, seq)
        """
    )
    chat_columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({_CHATS_TABLE})").fetchall()}
    if "llm_profile_id" not in chat_columns:
        conn.execute(f"ALTER TABLE {_CHATS_TABLE} ADD COLUMN llm_profile_id TEXT")

    turn_columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({_TURNS_TABLE})").fetchall()}
    for column in ("llm_profile_id", "llm_endpoint_id", "llm_model", "started_at", "last_event_at", "stop_requested_at"):
        if column not in turn_columns:
            conn.execute(f"ALTER TABLE {_TURNS_TABLE} ADD COLUMN {column} TEXT")
    if "event_count" not in turn_columns:
        conn.execute(f"ALTER TABLE {_TURNS_TABLE} ADD COLUMN event_count INTEGER NOT NULL DEFAULT 0")
    _ENSURED_DB_PATHS.add(db_key)


def _clean_chat_id(value: str | None = None) -> str:
    cleaned = _CHAT_ID_RE.sub("_", str(value or "").strip())[:120]
    return cleaned or uuid.uuid4().hex


def _clean_text(value: Any) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    return _WHITESPACE_RE.sub(" ", text).strip()


def _make_title(message: str) -> str:
    text = _MARKDOWN_RE.sub("", _clean_text(message))
    if not text:
        return "Новый чат"
    return text[:60]


def _make_preview(text: str) -> str:
    value = _clean_text(text)
    return value[:140]


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _turn_status(row: sqlite3.Row | None) -> str:
    return str(row["status"] or "") if row else ""


def _chat_row(row: sqlite3.Row, running_turn: sqlite3.Row | None = None, last_turn: sqlite3.Row | None = None) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": str(row["title"] or "Новый чат"),
        "preview": str(row["preview"] or ""),
        "message_count": int(row["message_count"] or 0),
        "llm_profile_id": str(row["llm_profile_id"] or ""),
        "running": bool(running_turn),
        "running_turn_id": str(running_turn["id"] or "") if running_turn else "",
        "last_turn_status": _turn_status(last_turn or running_turn),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "last_message_at": str(row["last_message_at"] or "") or None,
    }


def _turn_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "seq": int(row["seq"] or 0),
        "status": str(row["status"] or ""),
        "user_text": str(row["user_text"] or ""),
        "assistant_text": str(row["assistant_text"] or ""),
        "reasoning_text": str(row["reasoning_text"] or ""),
        "plan": _json_loads(row["plan_json"], {}),
        "tool_events": _json_loads(row["tool_events_json"], []),
        "notices": _json_loads(row["notices_json"], []),
        "usage": _json_loads(row["usage_json"], {}),
        "llm_profile_id": str(row["llm_profile_id"] or ""),
        "llm_endpoint_id": str(row["llm_endpoint_id"] or ""),
        "llm_model": str(row["llm_model"] or ""),
        "error_message": str(row["error_message"] or ""),
        "created_at": str(row["created_at"] or ""),
        "started_at": str(row["started_at"] or "") or None,
        "last_event_at": str(row["last_event_at"] or "") or None,
        "stop_requested_at": str(row["stop_requested_at"] or "") or None,
        "event_count": int(row["event_count"] or 0),
        "last_event_seq": int(row["event_count"] or 0),
        "completed_at": str(row["completed_at"] or "") or None,
    }


def _get_chat_row(conn: sqlite3.Connection, user_id: str, chat_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT * FROM {_CHATS_TABLE} WHERE user_id = ? AND id = ?",
        (user_id, chat_id),
    ).fetchone()
    if not row:
        raise AgentChatNotFound(chat_id)
    return row


def _get_turn_row(conn: sqlite3.Connection, user_id: str, chat_id: str, turn_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT * FROM {_TURNS_TABLE} WHERE id = ? AND chat_id = ? AND user_id = ?",
        (turn_id, chat_id, user_id),
    ).fetchone()
    if not row:
        raise AgentChatNotFound(chat_id)
    return row


def _get_running_turn_row(conn: sqlite3.Connection, user_id: str, chat_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT *
          FROM {_TURNS_TABLE}
         WHERE user_id = ? AND chat_id = ? AND status = 'running'
         ORDER BY seq DESC
         LIMIT 1
        """,
        (user_id, chat_id),
    ).fetchone()


def _get_last_turn_row(conn: sqlite3.Connection, user_id: str, chat_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT *
          FROM {_TURNS_TABLE}
         WHERE user_id = ? AND chat_id = ?
         ORDER BY seq DESC
         LIMIT 1
        """,
        (user_id, chat_id),
    ).fetchone()


def _refresh_chat_stats(conn: sqlite3.Connection, chat_id: str, *, preview: str | None = None) -> None:
    message_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {_TURNS_TABLE} WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]
        or 0
    )
    if preview is None:
        row = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(assistant_text, ''), user_text) AS preview
              FROM {_TURNS_TABLE}
             WHERE chat_id = ?
             ORDER BY seq DESC
             LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        preview = _make_preview(row["preview"] if row else "")
    conn.execute(
        f"""
        UPDATE {_CHATS_TABLE}
           SET message_count = ?,
               preview = ?,
               updated_at = {_utc_now_sql()},
               last_message_at = {_utc_now_sql()}
         WHERE id = ?
        """,
        (message_count, _make_preview(preview or ""), chat_id),
    )


def _delete_chats(conn: sqlite3.Connection, chat_ids: list[str]) -> None:
    if not chat_ids:
        return
    placeholders = ",".join("?" for _ in chat_ids)
    conn.execute(f"DELETE FROM {_EVENTS_TABLE} WHERE chat_id IN ({placeholders})", chat_ids)
    conn.execute(f"DELETE FROM {_TURNS_TABLE} WHERE chat_id IN ({placeholders})", chat_ids)
    conn.execute(f"DELETE FROM {_CHATS_TABLE} WHERE id IN ({placeholders})", chat_ids)


def prune_chats_for_user(user_id: str, *, keep_chat_id: str | None = None) -> list[str]:
    limit = max_chats_per_user()
    with _connect() as conn:
        _ensure_db(conn)
        rows = conn.execute(
            f"""
            SELECT id
              FROM {_CHATS_TABLE}
             WHERE user_id = ?
             ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC, created_at DESC
            """,
            (user_id,),
        ).fetchall()
        existing_ids = [str(row["id"]) for row in rows]
        keep_exists = bool(keep_chat_id and keep_chat_id in existing_ids)
        allowed_non_keep = max(0, limit - 1) if keep_exists else limit
        non_keep_ids = [chat_id for chat_id in existing_ids if chat_id != keep_chat_id]
        to_delete = non_keep_ids[allowed_non_keep:]
        _delete_chats(conn, to_delete)
        return to_delete


def list_chats(user_id: str) -> dict[str, Any]:
    with _connect() as conn:
        _ensure_db(conn)
        rows = conn.execute(
            f"""
            SELECT *
              FROM {_CHATS_TABLE}
             WHERE user_id = ?
             ORDER BY COALESCE(last_message_at, updated_at, created_at) DESC, created_at DESC
            """,
            (user_id,),
        ).fetchall()
        chats = []
        for row in rows:
            chat_id = str(row["id"])
            chats.append(
                _chat_row(
                    row,
                    running_turn=_get_running_turn_row(conn, user_id, chat_id),
                    last_turn=_get_last_turn_row(conn, user_id, chat_id),
                )
            )
    return {"max_chats": max_chats_per_user(), "count": len(chats), "chats": chats}


def create_chat(
    user_id: str,
    login: str | None = None,
    llm_profile_id: str | None = None,
) -> dict[str, Any]:
    chat_id = uuid.uuid4().hex
    with _connect() as conn:
        _ensure_db(conn)
        conn.execute(
            f"""
            INSERT INTO {_CHATS_TABLE}
                (
                    id, user_id, login, title, preview, message_count, llm_profile_id,
                    created_at, updated_at, last_message_at
                )
            VALUES (?, ?, ?, 'Новый чат', '', 0, ?, {_utc_now_sql()}, {_utc_now_sql()}, NULL)
            """,
            (chat_id, user_id, login, str(llm_profile_id or "").strip() or None),
        )
    prune_chats_for_user(user_id, keep_chat_id=chat_id)
    return get_chat(user_id, chat_id)


def get_chat(user_id: str, chat_id: str) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        return _chat_row(
            _get_chat_row(conn, user_id, clean_id),
            running_turn=_get_running_turn_row(conn, user_id, clean_id),
            last_turn=_get_last_turn_row(conn, user_id, clean_id),
        )


def get_chat_detail(user_id: str, chat_id: str, *, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        chat = _chat_row(
            _get_chat_row(conn, user_id, clean_id),
            running_turn=_get_running_turn_row(conn, user_id, clean_id),
            last_turn=_get_last_turn_row(conn, user_id, clean_id),
        )
        turns = []
        for row in conn.execute(
            f"""
            SELECT *
              FROM {_TURNS_TABLE}
             WHERE chat_id = ? AND user_id = ?
             ORDER BY seq ASC
            """,
            (clean_id, user_id),
        ).fetchall():
            turn = _turn_row(row)
            if turn["status"] == "running":
                event_rows = conn.execute(
                    f"""
                    SELECT *
                      FROM {_EVENTS_TABLE}
                     WHERE user_id = ? AND chat_id = ? AND turn_id = ?
                     ORDER BY seq ASC
                    """,
                    (user_id, clean_id, turn["id"]),
                ).fetchall()
                turn["events"] = [_event_row(event_row) for event_row in event_rows]
            turns.append(turn)
    return {"chat": chat, "turns": turns, "usage": usage or {}}


def delete_chat(user_id: str, chat_id: str) -> bool:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        if _get_running_turn_row(conn, user_id, clean_id):
            raise AgentChatRunning(clean_id)
        _delete_chats(conn, [clean_id])
    return True


def update_chat_llm_profile(user_id: str, chat_id: str, llm_profile_id: str) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    profile_id = str(llm_profile_id or "").strip()
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        conn.execute(
            f"""
            UPDATE {_CHATS_TABLE}
               SET llm_profile_id = ?,
                   updated_at = {_utc_now_sql()}
             WHERE id = ? AND user_id = ?
            """,
            (profile_id or None, clean_id, user_id),
        )
        return _chat_row(
            _get_chat_row(conn, user_id, clean_id),
            running_turn=_get_running_turn_row(conn, user_id, clean_id),
            last_turn=_get_last_turn_row(conn, user_id, clean_id),
        )


def get_running_turn(user_id: str, chat_id: str) -> dict[str, Any] | None:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        row = _get_running_turn_row(conn, user_id, clean_id)
        return _turn_row(row) if row else None


def chat_has_running_turn(user_id: str, chat_id: str) -> bool:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        return bool(_get_running_turn_row(conn, user_id, clean_id))


def get_turn(user_id: str, chat_id: str, turn_id: str) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        return _turn_row(_get_turn_row(conn, user_id, clean_id, str(turn_id or "")))


def append_turn_event(
    user_id: str,
    chat_id: str,
    turn_id: str,
    event_name: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    name = str(event_name or "message")
    base_payload = dict(payload or {})
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        _get_turn_row(conn, user_id, clean_id, turn_id)
        next_seq = int(
            conn.execute(
                f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {_EVENTS_TABLE} WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()[0]
            or 1
        )
        event_payload = {**base_payload, "chat_id": clean_id, "turn_id": turn_id, "seq": next_seq}
        event_id = uuid.uuid4().hex
        conn.execute(
            f"""
            INSERT INTO {_EVENTS_TABLE}
                (id, turn_id, chat_id, user_id, seq, event_name, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, {_utc_now_sql()})
            """,
            (event_id, turn_id, clean_id, user_id, next_seq, name, _json_dumps(event_payload)),
        )
        conn.execute(
            f"""
            UPDATE {_TURNS_TABLE}
               SET event_count = ?,
                   last_event_at = {_utc_now_sql()}
             WHERE id = ? AND chat_id = ? AND user_id = ?
            """,
            (next_seq, turn_id, clean_id, user_id),
        )
        row = conn.execute(
            f"SELECT * FROM {_EVENTS_TABLE} WHERE id = ?",
            (event_id,),
        ).fetchone()
    return _event_row(row)


def append_turn_events_batch(
    user_id: str,
    chat_id: str,
    turn_id: str,
    events: list[dict[str, Any]],
) -> int:
    clean_id = _clean_chat_id(chat_id)
    prepared: list[tuple[str, str, str, str, int, str, str]] = []
    max_seq = 0
    for event in events or []:
        payload = dict(event.get("payload") or {})
        try:
            seq = int(payload.get("seq") or event.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq <= 0:
            continue
        name = str(event.get("event_name") or event.get("event") or "message")
        event_payload = {**payload, "chat_id": clean_id, "turn_id": turn_id, "seq": seq}
        prepared.append((
            uuid.uuid4().hex,
            turn_id,
            clean_id,
            user_id,
            seq,
            name,
            _json_dumps(event_payload),
        ))
        max_seq = max(max_seq, seq)
    if not prepared:
        return 0

    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        _get_turn_row(conn, user_id, clean_id, turn_id)
        conn.executemany(
            f"""
            INSERT OR IGNORE INTO {_EVENTS_TABLE}
                (id, turn_id, chat_id, user_id, seq, event_name, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, {_utc_now_sql()})
            """,
            prepared,
        )
        conn.execute(
            f"""
            UPDATE {_TURNS_TABLE}
               SET event_count = CASE
                       WHEN COALESCE(event_count, 0) > ? THEN event_count
                       ELSE ?
                   END,
                   last_event_at = {_utc_now_sql()}
             WHERE id = ? AND chat_id = ? AND user_id = ?
            """,
            (max_seq, max_seq, turn_id, clean_id, user_id),
        )
    return len(prepared)


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "turn_id": str(row["turn_id"]),
        "chat_id": str(row["chat_id"]),
        "user_id": str(row["user_id"]),
        "seq": int(row["seq"] or 0),
        "event_name": str(row["event_name"] or "message"),
        "payload": _json_loads(row["payload_json"], {}),
        "created_at": str(row["created_at"] or ""),
    }


def get_turn_events(
    user_id: str,
    chat_id: str,
    turn_id: str,
    *,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    clean_id = _clean_chat_id(chat_id)
    try:
        seq = max(0, int(after_seq or 0))
    except (TypeError, ValueError):
        seq = 0
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        _get_turn_row(conn, user_id, clean_id, turn_id)
        rows = conn.execute(
            f"""
            SELECT *
              FROM {_EVENTS_TABLE}
             WHERE user_id = ? AND chat_id = ? AND turn_id = ? AND seq > ?
             ORDER BY seq ASC
            """,
            (user_id, clean_id, turn_id, seq),
        ).fetchall()
    return [_event_row(row) for row in rows]


def update_running_turn_snapshot(
    user_id: str,
    chat_id: str,
    turn_id: str,
    *,
    assistant_text: str = "",
    reasoning_text: str = "",
    plan: dict[str, Any] | None = None,
    tool_events: list[dict[str, Any]] | None = None,
    notices: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    error_message: str = "",
) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        _get_turn_row(conn, user_id, clean_id, turn_id)
        conn.execute(
            f"""
            UPDATE {_TURNS_TABLE}
               SET assistant_text = ?,
                   reasoning_text = ?,
                   plan_json = ?,
                   tool_events_json = ?,
                   notices_json = ?,
                   usage_json = ?,
                   error_message = ?,
                   last_event_at = {_utc_now_sql()}
             WHERE id = ? AND chat_id = ? AND user_id = ? AND status = 'running'
            """,
            (
                assistant_text or "",
                reasoning_text or "",
                _json_dumps(plan or {}),
                _json_dumps(tool_events or []),
                _json_dumps(notices or []),
                _json_dumps(usage or {}),
                error_message or "",
                turn_id,
                clean_id,
                user_id,
            ),
        )
        row = _get_turn_row(conn, user_id, clean_id, turn_id)
    return _turn_row(row)


def request_stop_turn(user_id: str, chat_id: str, turn_id: str) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        _get_turn_row(conn, user_id, clean_id, turn_id)
        conn.execute(
            f"""
            UPDATE {_TURNS_TABLE}
               SET stop_requested_at = {_utc_now_sql()}
             WHERE id = ? AND chat_id = ? AND user_id = ? AND status = 'running'
            """,
            (turn_id, clean_id, user_id),
        )
        row = _get_turn_row(conn, user_id, clean_id, turn_id)
    return _turn_row(row)


def mark_stale_running_turns() -> int:
    with _connect() as conn:
        _ensure_db(conn)
        rows = conn.execute(
            f"SELECT id, chat_id, user_id FROM {_TURNS_TABLE} WHERE status = 'running'"
        ).fetchall()
        for row in rows:
            conn.execute(
                f"""
                UPDATE {_TURNS_TABLE}
                   SET status = 'error',
                       error_message = 'Выполнение прервано перезапуском сервера',
                       completed_at = {_utc_now_sql()}
                 WHERE id = ? AND chat_id = ? AND user_id = ? AND status = 'running'
                """,
                (row["id"], row["chat_id"], row["user_id"]),
            )
            _refresh_chat_stats(
                conn,
                str(row["chat_id"]),
                preview="Выполнение прервано перезапуском сервера",
            )
    return len(rows)


def start_turn(
    user_id: str,
    chat_id: str,
    message: str,
    *,
    login: str | None = None,
    llm_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_id = _clean_chat_id(chat_id)
    user_text = str(message or "")
    profile_id = str((llm_profile or {}).get("profile_id") or "").strip()
    endpoint_id = str((llm_profile or {}).get("endpoint_id") or "").strip()
    model = str((llm_profile or {}).get("model") or "").strip()
    with _connect() as conn:
        _ensure_db(conn)
        chat = _get_chat_row(conn, user_id, clean_id)
        if _get_running_turn_row(conn, user_id, clean_id):
            raise AgentChatRunning(clean_id)
        next_seq = int(
            conn.execute(
                f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {_TURNS_TABLE} WHERE chat_id = ?",
                (clean_id,),
            ).fetchone()[0]
            or 1
        )
        turn_id = uuid.uuid4().hex
        conn.execute(
            f"""
            INSERT INTO {_TURNS_TABLE}
                (
                    id, chat_id, user_id, seq, status, user_text, assistant_text,
                    reasoning_text, plan_json, tool_events_json, notices_json,
                    usage_json, llm_profile_id, llm_endpoint_id, llm_model,
                    error_message, created_at, started_at, last_event_at,
                    stop_requested_at, event_count, completed_at
                )
            VALUES (
                ?, ?, ?, ?, 'running', ?, '', '', '{{}}', '[]', '[]', '{{}}',
                ?, ?, ?, '', {_utc_now_sql()}, {_utc_now_sql()}, NULL, NULL, 0, NULL
            )
            """,
            (turn_id, clean_id, user_id, next_seq, user_text, profile_id, endpoint_id, model),
        )
        title = str(chat["title"] or "Новый чат")
        if int(chat["message_count"] or 0) == 0 and title == "Новый чат":
            title = _make_title(user_text)
        conn.execute(
            f"""
            UPDATE {_CHATS_TABLE}
               SET title = ?,
                   login = COALESCE(?, login),
                   llm_profile_id = COALESCE(?, llm_profile_id),
                   preview = ?,
                   updated_at = {_utc_now_sql()},
                   last_message_at = {_utc_now_sql()}
             WHERE id = ?
            """,
            (title, login, profile_id or None, _make_preview(user_text), clean_id),
        )
    return {
        "id": turn_id,
        "chat_id": clean_id,
        "seq": next_seq,
        "status": "running",
        "llm_profile_id": profile_id,
        "llm_endpoint_id": endpoint_id,
        "llm_model": model,
        "event_count": 0,
        "last_event_seq": 0,
    }


def finish_turn(
    user_id: str,
    chat_id: str,
    turn_id: str,
    *,
    status: str,
    assistant_text: str = "",
    reasoning_text: str = "",
    plan: dict[str, Any] | None = None,
    tool_events: list[dict[str, Any]] | None = None,
    notices: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
    error_message: str = "",
) -> dict[str, Any]:
    if status not in {"done", "stopped", "error"}:
        raise ValueError("status must be done, stopped, or error")
    clean_id = _clean_chat_id(chat_id)
    with _connect() as conn:
        _ensure_db(conn)
        _get_chat_row(conn, user_id, clean_id)
        row = conn.execute(
            f"SELECT user_text FROM {_TURNS_TABLE} WHERE id = ? AND chat_id = ? AND user_id = ?",
            (turn_id, clean_id, user_id),
        ).fetchone()
        if not row:
            raise AgentChatNotFound(clean_id)
        conn.execute(
            f"""
            UPDATE {_TURNS_TABLE}
               SET status = ?,
                   assistant_text = ?,
                   reasoning_text = ?,
                   plan_json = ?,
                   tool_events_json = ?,
                   notices_json = ?,
                   usage_json = ?,
                   error_message = ?,
                   completed_at = {_utc_now_sql()}
             WHERE id = ? AND chat_id = ? AND user_id = ?
            """,
            (
                status,
                assistant_text or "",
                reasoning_text or "",
                _json_dumps(plan or {}),
                _json_dumps(tool_events or []),
                _json_dumps(notices or []),
                _json_dumps(usage or {}),
                error_message or "",
                turn_id,
                clean_id,
                user_id,
            ),
        )
        preview_source = assistant_text or error_message or str(row["user_text"] or "")
        _refresh_chat_stats(conn, clean_id, preview=preview_source)
        turn = conn.execute(
            f"SELECT * FROM {_TURNS_TABLE} WHERE id = ?",
            (turn_id,),
        ).fetchone()
    prune_chats_for_user(user_id, keep_chat_id=clean_id)
    return _turn_row(turn)


def reset_for_tests() -> None:
    _ENSURED_DB_PATHS.clear()
    path = _db_path()
    if path.exists():
        path.unlink()
