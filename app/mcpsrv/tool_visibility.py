"""Runtime visibility controls for locally registered FastMCP tools."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings

SERVER_NAME = "local"

_LOCK = threading.RLock()
_REGISTERED_TOOLS: dict[str, Any] = {}
_ENABLED: dict[str, bool] = {}
_INITIALIZED = False


class ToolVisibilityError(RuntimeError):
    """Raised when MCP tool visibility cannot be loaded or changed."""


class ToolNotRegisteredError(ToolVisibilityError):
    """Raised when a requested tool is not part of the registered snapshot."""


@dataclass(frozen=True)
class RegisteredTool:
    tool: Any
    enabled: bool


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", "") or "").strip()
    return str(getattr(tool, "name", "") or "").strip()


def _db_path() -> Path:
    return Path(settings.mcp_settings_sqlite_path)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_tool_settings (
            server_name TEXT NOT NULL DEFAULT 'local',
            tool_name TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (server_name, tool_name)
        )
        """
    )
    conn.commit()


def _open_connection() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    _ensure_schema(conn)
    return conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_saved_states() -> dict[str, bool]:
    with _open_connection() as conn:
        rows = conn.execute(
            """
            SELECT tool_name, enabled
              FROM mcp_tool_settings
             WHERE server_name = ?
            """,
            (SERVER_NAME,),
        ).fetchall()
    return {str(name): bool(enabled) for name, enabled in rows}


def _save_state(tool_name: str, enabled: bool) -> None:
    with _open_connection() as conn:
        conn.execute(
            """
            INSERT INTO mcp_tool_settings (server_name, tool_name, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(server_name, tool_name) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (SERVER_NAME, tool_name, 1 if enabled else 0, _utc_now()),
        )
        conn.commit()


def _await_sync(value: Any) -> Any:
    if not inspect.isawaitable(value):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise ToolVisibilityError("Cannot synchronously await FastMCP metadata inside a running event loop")


async def _await_async(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _list_tools_sync(mcp: Any) -> list[Any]:
    list_tools = getattr(mcp, "list_tools", None)
    if not callable(list_tools):
        raise ToolVisibilityError("FastMCP list_tools is not available")
    try:
        result = list_tools(run_middleware=False)
    except TypeError:
        result = list_tools()
    return list(_await_sync(result) or [])


def _get_tool_sync(mcp: Any, name: str) -> Any | None:
    get_tool = getattr(mcp, "get_tool", None)
    if not callable(get_tool):
        return None
    try:
        result = get_tool(name, version=None)
    except TypeError:
        result = get_tool(name)
    return _await_sync(result)


async def _get_tool_async(mcp: Any, name: str) -> Any | None:
    get_tool = getattr(mcp, "get_tool", None)
    if not callable(get_tool):
        return None
    try:
        result = get_tool(name, version=None)
    except TypeError:
        result = get_tool(name)
    return await _await_async(result)


def _remove_tool(mcp: Any, name: str) -> None:
    remove_tool = getattr(mcp, "remove_tool", None)
    if not callable(remove_tool):
        raise ToolVisibilityError("FastMCP remove_tool is not available")
    try:
        result = remove_tool(name, version=None)
    except TypeError:
        result = remove_tool(name)
    _await_sync(result)


async def _remove_tool_async(mcp: Any, name: str) -> None:
    remove_tool = getattr(mcp, "remove_tool", None)
    if not callable(remove_tool):
        raise ToolVisibilityError("FastMCP remove_tool is not available")
    try:
        result = remove_tool(name, version=None)
    except TypeError:
        result = remove_tool(name)
    await _await_async(result)


def _add_tool(mcp: Any, tool: Any) -> None:
    add_tool = getattr(mcp, "add_tool", None)
    if not callable(add_tool):
        raise ToolVisibilityError("FastMCP add_tool is not available")
    _await_sync(add_tool(tool))


async def _add_tool_async(mcp: Any, tool: Any) -> None:
    add_tool = getattr(mcp, "add_tool", None)
    if not callable(add_tool):
        raise ToolVisibilityError("FastMCP add_tool is not available")
    await _await_async(add_tool(tool))


def initialize_tool_visibility(mcp: Any) -> None:
    """Capture registered tools and remove tools disabled in persisted settings."""
    global _INITIALIZED, _REGISTERED_TOOLS, _ENABLED
    with _LOCK:
        tools = {
            name: tool
            for tool in _list_tools_sync(mcp)
            if (name := _tool_name(tool))
        }
        saved_states = _load_saved_states()
        enabled = {name: saved_states.get(name, True) for name in tools}

        for name, is_enabled in enabled.items():
            if not is_enabled:
                present = _get_tool_sync(mcp, name)
                if present is not None:
                    _remove_tool(mcp, name)

        _REGISTERED_TOOLS = tools
        _ENABLED = enabled
        _INITIALIZED = True


def is_initialized() -> bool:
    with _LOCK:
        return _INITIALIZED


def list_registered_tools() -> list[RegisteredTool]:
    with _LOCK:
        return [
            RegisteredTool(tool=tool, enabled=_ENABLED.get(name, True))
            for name, tool in _REGISTERED_TOOLS.items()
        ]


def get_tool_enabled(tool_name: str) -> bool:
    with _LOCK:
        if tool_name in _REGISTERED_TOOLS:
            return _ENABLED.get(tool_name, True)
    return True


def set_tool_enabled(mcp: Any, tool_name: str, enabled: bool) -> RegisteredTool:
    with _LOCK:
        name = str(tool_name or "").strip()
        if name not in _REGISTERED_TOOLS:
            raise ToolNotRegisteredError(name)

        tool = _REGISTERED_TOOLS[name]
        current = _ENABLED.get(name, True)
        desired = bool(enabled)
        if desired != current:
            if desired:
                if _get_tool_sync(mcp, name) is None:
                    _add_tool(mcp, tool)
            else:
                if _get_tool_sync(mcp, name) is not None:
                    _remove_tool(mcp, name)
            _ENABLED[name] = desired

        _save_state(name, desired)
        _ENABLED[name] = desired
        return RegisteredTool(tool=tool, enabled=desired)


async def set_tool_enabled_async(mcp: Any, tool_name: str, enabled: bool) -> RegisteredTool:
    name = str(tool_name or "").strip()
    with _LOCK:
        if name not in _REGISTERED_TOOLS:
            raise ToolNotRegisteredError(name)
        tool = _REGISTERED_TOOLS[name]
        current = _ENABLED.get(name, True)

    desired = bool(enabled)
    if desired != current:
        if desired:
            if await _get_tool_async(mcp, name) is None:
                await _add_tool_async(mcp, tool)
        else:
            if await _get_tool_async(mcp, name) is not None:
                await _remove_tool_async(mcp, name)

    _save_state(name, desired)
    with _LOCK:
        _ENABLED[name] = desired
    return RegisteredTool(tool=tool, enabled=desired)


def _reset_for_tests() -> None:
    global _INITIALIZED, _REGISTERED_TOOLS, _ENABLED
    with _LOCK:
        _REGISTERED_TOOLS = {}
        _ENABLED = {}
        _INITIALIZED = False


__all__ = [
    "RegisteredTool",
    "ToolNotRegisteredError",
    "ToolVisibilityError",
    "get_tool_enabled",
    "initialize_tool_visibility",
    "is_initialized",
    "list_registered_tools",
    "set_tool_enabled",
    "set_tool_enabled_async",
]
