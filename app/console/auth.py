"""Role-based token auth for the web console."""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import settings

_TABLE = "console_users"
_INITIALIZED_PATH: str | None = None
_ROLES = {"admin", "user"}


class ConsoleAuthError(RuntimeError):
    """Raised when console users cannot be loaded or changed."""


class ConsoleUserNotFound(ConsoleAuthError):
    """Raised when a user id does not exist."""


class ConsoleUserConflict(ConsoleAuthError):
    """Raised when a login or token conflicts with existing data."""


class ConsoleLastAdminError(ConsoleAuthError):
    """Raised when an operation would remove the last active admin."""


class ConsoleProtectedAdminError(ConsoleAuthError):
    """Raised when an env-managed admin is modified in a way that breaks env recovery."""


@dataclass(frozen=True)
class ConsoleAuth:
    user_id: str
    login: str
    display_name: str
    role: str
    token_param: str
    token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public_dict(self) -> dict[str, Any]:
        return {
            "userId": self.user_id,
            "login": self.login,
            "displayName": self.display_name,
            "role": self.role,
            "tokenParam": self.token_param,
            "token": self.token,
        }


def _db_path() -> Path:
    return Path(settings.web_console_users_sqlite_path)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_user_token() -> str:
    return secrets.token_urlsafe(32)


def _utc_now_sql() -> str:
    return "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id TEXT PRIMARY KEY,
            login TEXT NOT NULL UNIQUE,
            display_name TEXT,
            role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
            token_hash TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_seen_at TEXT
        )
        """
    )


def _normalise_role(role: Any) -> str:
    value = str(role or "user").strip().lower()
    if value not in _ROLES:
        raise ValueError("role must be admin or user")
    return value


def _normalise_login(login: Any) -> str:
    value = str(login or "").strip()
    if not value:
        raise ValueError("login is required")
    if len(value) > 80:
        raise ValueError("login is too long")
    return value


def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "login": str(row["login"]),
        "display_name": str(row["display_name"] or ""),
        "role": str(row["role"]),
        "enabled": bool(row["enabled"]),
        "source": str(row["source"] or "manual"),
        "created_at": str(row["created_at"] or ""),
        "updated_at": str(row["updated_at"] or ""),
        "last_seen_at": str(row["last_seen_at"] or "") or None,
    }


def _upsert_admin_from_env(conn: sqlite3.Connection) -> None:
    token = str(settings.web_console_admin_token or "").strip()
    if not token:
        return
    token_hash = _hash_token(token)
    existing = conn.execute(
        f"SELECT id FROM {_TABLE} WHERE login = ?",
        ("admin",),
    ).fetchone()
    if existing:
        conn.execute(
            f"""
            UPDATE {_TABLE}
               SET role = 'admin',
                   display_name = COALESCE(NULLIF(display_name, ''), 'Admin'),
                   token_hash = ?,
                   enabled = 1,
                   source = 'env_admin',
                   updated_at = {_utc_now_sql()}
             WHERE login = 'admin'
            """,
            (token_hash,),
        )
        return
    conn.execute(
        f"""
        INSERT INTO {_TABLE}
            (id, login, display_name, role, token_hash, enabled, source, created_at, updated_at)
        VALUES (?, 'admin', 'Admin', 'admin', ?, 1, 'env_admin', {_utc_now_sql()}, {_utc_now_sql()})
        """,
        (uuid.uuid4().hex, token_hash),
    )


def _seed_env_users(conn: sqlite3.Connection) -> None:
    for raw in list(settings.web_console_seed_users or []):
        if not isinstance(raw, dict):
            continue
        try:
            login = _normalise_login(raw.get("login"))
            role = _normalise_role(raw.get("role", "user"))
        except ValueError:
            continue
        token = str(raw.get("token") or "").strip()
        if not token:
            continue
        existing = conn.execute(
            f"SELECT id FROM {_TABLE} WHERE login = ?",
            (login,),
        ).fetchone()
        if existing:
            continue
        display_name = str(raw.get("display_name") or "").strip()
        enabled = 1 if bool(raw.get("enabled", True)) else 0
        conn.execute(
            f"""
            INSERT INTO {_TABLE}
                (id, login, display_name, role, token_hash, enabled, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'env_seed', {_utc_now_sql()}, {_utc_now_sql()})
            """,
            (uuid.uuid4().hex, login, display_name, role, _hash_token(token), enabled),
        )


def ensure_console_users_initialized() -> None:
    global _INITIALIZED_PATH
    path_key = "|".join([
        str(_db_path()),
        _hash_token(str(settings.web_console_admin_token or "")),
        repr(settings.web_console_seed_users or []),
    ])
    if _INITIALIZED_PATH == path_key:
        return
    with _connect() as conn:
        _ensure_table(conn)
        _upsert_admin_from_env(conn)
        _seed_env_users(conn)
    _INITIALIZED_PATH = path_key


def _fetch_user_by_token_hash(token_hash: str, role: str) -> sqlite3.Row | None:
    ensure_console_users_initialized()
    with _connect() as conn:
        return conn.execute(
            f"""
            SELECT * FROM {_TABLE}
             WHERE token_hash = ? AND role = ? AND enabled = 1
            """,
            (token_hash, role),
        ).fetchone()


def authenticate_console_request(request: Any) -> ConsoleAuth | None:
    candidates = [
        ("admin_token", request.query_params.get("admin_token") or request.headers.get("X-Console-Admin-Token", ""), "admin"),
        ("user_token", request.query_params.get("user_token") or request.headers.get("X-Console-User-Token", ""), "user"),
    ]
    for token_param, token, role in candidates:
        token = str(token or "").strip()
        if not token:
            continue
        row = _fetch_user_by_token_hash(_hash_token(token), role)
        if not row:
            continue
        with _connect() as conn:
            conn.execute(
                f"UPDATE {_TABLE} SET last_seen_at = {_utc_now_sql()} WHERE id = ?",
                (row["id"],),
            )
        return ConsoleAuth(
            user_id=str(row["id"]),
            login=str(row["login"]),
            display_name=str(row["display_name"] or ""),
            role=str(row["role"]),
            token_param=token_param,
            token=token,
        )
    return None


def _json_forbidden() -> Any:
    from starlette.responses import JSONResponse

    return JSONResponse({"error": "forbidden"}, status_code=403)


def require_console_auth(request: Any) -> ConsoleAuth | Any:
    auth = authenticate_console_request(request)
    if not auth:
        return _json_forbidden()
    return auth


def require_console_admin(request: Any) -> ConsoleAuth | Any:
    auth = authenticate_console_request(request)
    if not auth or not auth.is_admin:
        return _json_forbidden()
    return auth


def list_console_users() -> dict[str, Any]:
    ensure_console_users_initialized()
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM {_TABLE} ORDER BY role = 'admin' DESC, login COLLATE NOCASE"
        ).fetchall()
    users = [_row_to_user(row) for row in rows]
    return {"count": len(users), "users": users}


def _active_admin_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        f"SELECT COUNT(*) FROM {_TABLE} WHERE role = 'admin' AND enabled = 1"
    ).fetchone()
    return int(row[0] or 0)


def _ensure_not_last_admin(conn: sqlite3.Connection, user_id: str, *, next_role: str, next_enabled: bool) -> None:
    row = conn.execute(f"SELECT role, enabled FROM {_TABLE} WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise ConsoleUserNotFound(user_id)
    currently_active_admin = row["role"] == "admin" and bool(row["enabled"])
    will_be_active_admin = next_role == "admin" and next_enabled
    if currently_active_admin and not will_be_active_admin and _active_admin_count(conn) <= 1:
        raise ConsoleLastAdminError("cannot disable or demote the last active admin")


def create_console_user(payload: dict[str, Any]) -> dict[str, Any]:
    ensure_console_users_initialized()
    login = _normalise_login(payload.get("login"))
    role = _normalise_role(payload.get("role", "user"))
    display_name = str(payload.get("display_name") or "").strip()
    enabled = bool(payload.get("enabled", True))
    token = generate_user_token()
    user_id = uuid.uuid4().hex
    try:
        with _connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {_TABLE}
                    (id, login, display_name, role, token_hash, enabled, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'manual', {_utc_now_sql()}, {_utc_now_sql()})
                """,
                (user_id, login, display_name, role, _hash_token(token), 1 if enabled else 0),
            )
            row = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ConsoleUserConflict(str(exc)) from exc
    return {"user": _row_to_user(row), "token": token}


def update_console_user(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_console_users_initialized()
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ConsoleUserNotFound(user_id)
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ConsoleUserNotFound(user_id)
        role = _normalise_role(payload.get("role", row["role"]))
        enabled = bool(payload.get("enabled", bool(row["enabled"])))
        display_name = str(payload.get("display_name", row["display_name"] or "") or "").strip()
        _ensure_not_last_admin(conn, user_id, next_role=role, next_enabled=enabled)
        conn.execute(
            f"""
            UPDATE {_TABLE}
               SET display_name = ?, role = ?, enabled = ?, updated_at = {_utc_now_sql()}
             WHERE id = ?
            """,
            (display_name, role, 1 if enabled else 0, user_id),
        )
        updated = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (user_id,)).fetchone()
    return {"user": _row_to_user(updated)}


def rotate_console_user_token(user_id: str) -> dict[str, Any]:
    ensure_console_users_initialized()
    token = generate_user_token()
    with _connect() as conn:
        row = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise ConsoleUserNotFound(user_id)
        if row["role"] == "admin" and row["source"] == "env_admin":
            raise ConsoleProtectedAdminError(
                "env admin token is managed by WEB_CONSOLE_ADMIN_TOKEN"
            )
        conn.execute(
            f"UPDATE {_TABLE} SET token_hash = ?, updated_at = {_utc_now_sql()} WHERE id = ?",
            (_hash_token(token), user_id),
        )
        updated = conn.execute(f"SELECT * FROM {_TABLE} WHERE id = ?", (user_id,)).fetchone()
    return {"user": _row_to_user(updated), "token": token}


def _reset_for_tests() -> None:
    global _INITIALIZED_PATH
    _INITIALIZED_PATH = None
