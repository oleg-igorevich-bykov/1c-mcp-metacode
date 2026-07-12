"""Streaming web-console agent backed by OpenAI Agents SDK and MCP tools."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sqlite3
import time
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterable, Literal

import runtime_metrics
from config import settings
from console import agent_llm

try:
    import httpx
except ImportError:  # pragma: no cover - production image includes httpx
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SESSION_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_PREVIEW_LIMIT = 20000
_PLAN_TOOL_NAMES = {"set_plan", "update_plan_step", "complete_plan"}
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")
_USAGE_TABLE = "console_agent_usage"
_MODEL_PRICING_CACHE: dict[str, tuple[float, float]] = {}
_CONSOLE_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\((metacode://[^)\s]+)\)")
_CONSOLE_BARE_LINK_RE = re.compile(r"metacode://[^\s)]+")


class ConsoleAgentUnavailable(RuntimeError):
    """Raised when the console agent is disabled or misconfigured."""


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            text = str(value)
    text = text.replace("\r\n", "\n")
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def _typed_text_preview_value(value: Any) -> Any:
    """Unwrap MCP typed text content for human-readable UI previews."""
    if isinstance(value, str):
        text = value.strip()
        if not text or text[0] not in "[{":
            return value
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return value
        return _typed_text_preview_value(parsed)

    if isinstance(value, dict):
        item_type = str(value.get("type") or "")
        if item_type in {"text", "input_text"} and isinstance(value.get("text"), str):
            return value["text"]
        return value

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            unwrapped = _typed_text_preview_value(item)
            if isinstance(unwrapped, str):
                parts.append(unwrapped)
            else:
                return value
        return "\n\n".join(part.strip("\n") for part in parts if part)

    return value


def _tool_output_preview(value: Any, limit: int = _PREVIEW_LIMIT) -> str:
    return _preview(_typed_text_preview_value(value), limit=limit)


def _token_estimate(value: Any, model_name: str | None = None) -> int:
    if value is None:
        return 0
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            value = str(value)
    text = value or ""
    try:
        import tiktoken  # type: ignore

        model = (model_name or settings.console_agent_model or "").strip()
        try:
            encoding = tiktoken.encoding_for_model(model)
        except Exception:
            encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return max(1, int((len(text) + 2) / 3)) if text else 0


def _usage_int(value: Any, attr: str) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        raw = value.get(attr)
    else:
        raw = getattr(value, attr, None)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _usage_float(value: Any, attr: str) -> float | None:
    if value is None:
        return None
    raw = value.get(attr) if isinstance(value, dict) else getattr(value, attr, None)
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _sdk_usage_dict(usage: Any) -> dict[str, Any]:
    entries = list(getattr(usage, "request_usage_entries", []) or [])
    last_entry = entries[-1] if entries else None
    return {
        "requests": _usage_int(usage, "requests") or len(entries),
        "input_tokens": _usage_int(usage, "input_tokens"),
        "output_tokens": _usage_int(usage, "output_tokens"),
        "total_tokens": _usage_int(usage, "total_tokens"),
        "current_context_tokens": _usage_int(last_entry, "input_tokens"),
        "cost_amount": None,
        "cost_unit": None,
        "cost_source": "unknown",
        "source": "api_usage" if usage is not None else "unavailable",
    }


def _ensure_usage_table(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_USAGE_TABLE} (
                session_id TEXT PRIMARY KEY,
                requests INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_amount REAL,
                cost_unit TEXT,
                user_id TEXT,
                login TEXT,
                llm_profile_id TEXT,
                llm_endpoint_id TEXT,
                llm_model TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({_USAGE_TABLE})").fetchall()
        }
        if "cost_amount" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN cost_amount REAL")
        if "cost_unit" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN cost_unit TEXT")
        if "user_id" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN user_id TEXT")
        if "login" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN login TEXT")
        if "llm_profile_id" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN llm_profile_id TEXT")
        if "llm_endpoint_id" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN llm_endpoint_id TEXT")
        if "llm_model" not in columns:
            conn.execute(f"ALTER TABLE {_USAGE_TABLE} ADD COLUMN llm_model TEXT")


def _get_session_usage(db_path: Path, session_id: str) -> dict[str, Any]:
    _ensure_usage_table(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            f"""
            SELECT requests, input_tokens, output_tokens, total_tokens, cost_amount, cost_unit,
                   llm_profile_id, llm_endpoint_id, llm_model
              FROM {_USAGE_TABLE}
             WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    if not row:
        return {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_amount": None,
            "cost_unit": None,
            "llm_profile_id": "",
            "llm_endpoint_id": "",
            "llm_model": "",
        }
    return {
        "requests": int(row[0] or 0),
        "input_tokens": int(row[1] or 0),
        "output_tokens": int(row[2] or 0),
        "total_tokens": int(row[3] or 0),
        "cost_amount": float(row[4]) if row[4] is not None else None,
        "cost_unit": str(row[5] or "") or None,
        "llm_profile_id": str(row[6] or ""),
        "llm_endpoint_id": str(row[7] or ""),
        "llm_model": str(row[8] or ""),
    }


def _add_session_usage(
    db_path: Path,
    session_id: str,
    turn_usage: dict[str, Any],
    *,
    user_id: str | None = None,
    login: str | None = None,
    llm_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_usage_table(db_path)
    requests = int(turn_usage.get("requests") or 0)
    input_tokens = int(turn_usage.get("input_tokens") or 0)
    output_tokens = int(turn_usage.get("output_tokens") or 0)
    total_tokens = int(turn_usage.get("total_tokens") or 0)
    cost_amount = _usage_float(turn_usage, "cost_amount")
    cost_unit = str(turn_usage.get("cost_unit") or "").strip() or None
    llm_profile_id = str((llm_profile or {}).get("profile_id") or "").strip() or None
    llm_endpoint_id = str((llm_profile or {}).get("endpoint_id") or "").strip() or None
    llm_model = str((llm_profile or {}).get("model") or "").strip() or None
    if (
        requests <= 0
        and input_tokens <= 0
        and output_tokens <= 0
        and total_tokens <= 0
        and cost_amount is None
    ):
        return _get_session_usage(db_path, session_id)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            f"""
            INSERT INTO {_USAGE_TABLE}
                (
                    session_id, requests, input_tokens, output_tokens, total_tokens,
                    cost_amount, cost_unit, user_id, login,
                    llm_profile_id, llm_endpoint_id, llm_model, updated_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(session_id) DO UPDATE SET
                requests = requests + excluded.requests,
                input_tokens = input_tokens + excluded.input_tokens,
                output_tokens = output_tokens + excluded.output_tokens,
                total_tokens = total_tokens + excluded.total_tokens,
                cost_amount = CASE
                    WHEN cost_amount IS NULL AND excluded.cost_amount IS NULL THEN NULL
                    ELSE COALESCE(cost_amount, 0) + COALESCE(excluded.cost_amount, 0)
                END,
                cost_unit = COALESCE(excluded.cost_unit, cost_unit),
                user_id = COALESCE(excluded.user_id, user_id),
                login = COALESCE(excluded.login, login),
                llm_profile_id = COALESCE(excluded.llm_profile_id, llm_profile_id),
                llm_endpoint_id = COALESCE(excluded.llm_endpoint_id, llm_endpoint_id),
                llm_model = COALESCE(excluded.llm_model, llm_model),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                session_id,
                requests,
                input_tokens,
                output_tokens,
                total_tokens,
                cost_amount,
                cost_unit,
                user_id,
                login,
                llm_profile_id,
                llm_endpoint_id,
                llm_model,
            ),
        )
    return _get_session_usage(db_path, session_id)


def _clean_session_id(session_id: str) -> str:
    cleaned = _SESSION_RE.sub("_", (session_id or "").strip())[:120]
    return cleaned or "default"


def _user_scoped_session_id(user_id: str | None, session_id: str) -> str:
    public_sid = _clean_session_id(session_id)
    clean_user_id = _clean_session_id(user_id or "anonymous")
    return f"user:{clean_user_id}:session:{public_sid}"


def public_session_id(session_id: str) -> str:
    """Return the frontend-safe session id used by SSE payloads."""
    return _clean_session_id(session_id)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def user_scoped_session_id(user_id: str | None, session_id: str) -> str:
    """Return the backend session id used by Agents SDK and usage rows."""
    return _user_scoped_session_id(user_id, session_id)


def _strip_console_links_from_text(text: str) -> tuple[str, bool]:
    """Remove UI-only console links before passing history back to the model."""
    if "metacode://" not in text:
        return text, False

    changed = False

    def replace_markdown_link(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        return match.group(1)

    cleaned = _CONSOLE_MARKDOWN_LINK_RE.sub(replace_markdown_link, text)
    without_bare_links = _CONSOLE_BARE_LINK_RE.sub("", cleaned)
    if without_bare_links != cleaned:
        changed = True
        cleaned = without_bare_links
    return cleaned, changed


def _sanitize_console_links_in_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _strip_console_links_from_text(value)
    if isinstance(value, list):
        changed = False
        cleaned_items = []
        for item in value:
            cleaned_item, item_changed = _sanitize_console_links_in_value(item)
            changed = changed or item_changed
            cleaned_items.append(cleaned_item)
        return cleaned_items, changed
    if isinstance(value, tuple):
        changed = False
        cleaned_items = []
        for item in value:
            cleaned_item, item_changed = _sanitize_console_links_in_value(item)
            changed = changed or item_changed
            cleaned_items.append(cleaned_item)
        return tuple(cleaned_items), changed
    if isinstance(value, dict):
        changed = False
        cleaned_dict: dict[Any, Any] = {}
        for key, item in value.items():
            cleaned_item, item_changed = _sanitize_console_links_in_value(item)
            changed = changed or item_changed
            cleaned_dict[key] = cleaned_item
        return cleaned_dict, changed
    return value, False


async def _sanitize_session_console_links(session: Any) -> bool:
    """Best-effort cleanup of UI-only metacode links in SDK session history."""
    get_items = getattr(session, "get_items", None)
    clear_session = getattr(session, "clear_session", None)
    add_items = getattr(session, "add_items", None)
    if not callable(get_items) or not callable(clear_session) or not callable(add_items):
        return False

    try:
        items = await _maybe_await(get_items())
        cleaned_items, changed = _sanitize_console_links_in_value(items)
        if not changed:
            return False
    except Exception:
        logger.debug("Failed to inspect console agent SDK session history", exc_info=True)
        return False

    cleared = False
    try:
        await _maybe_await(clear_session())
        cleared = True
        await _maybe_await(add_items(cleaned_items))
        return True
    except Exception:
        logger.warning("Failed to sanitize console agent SDK session history", exc_info=True)
        if cleared:
            try:
                await _maybe_await(clear_session())
                await _maybe_await(add_items(items))
            except Exception:
                logger.warning("Failed to restore console agent SDK session history after sanitizer error", exc_info=True)
        return False


def get_session_usage_for_user(user_id: str | None, session_id: str) -> dict[str, Any]:
    sid = _user_scoped_session_id(user_id, session_id)
    return _get_session_usage(Path(settings.console_agent_session_sqlite_path), sid)


def delete_session_usage_for_user(user_id: str | None, session_id: str) -> None:
    sid = _user_scoped_session_id(user_id, session_id)
    session_path = Path(settings.console_agent_session_sqlite_path)
    _ensure_usage_table(session_path)
    with sqlite3.connect(str(session_path)) as conn:
        conn.execute(f"DELETE FROM {_USAGE_TABLE} WHERE session_id = ?", (sid,))


async def try_clear_sdk_session_for_user(user_id: str | None, session_id: str) -> bool:
    """Best-effort cleanup for old Agents SDK session rows.

    The SDK owns the SQLiteSession schema, so we only use public methods when
    the installed version exposes one. Chat metadata and usage cleanup do not
    depend on this.
    """
    try:
        sdk = _load_agents_sdk()
    except Exception:
        return False
    try:
        session_path = Path(settings.console_agent_session_sqlite_path)
        session = sdk["SQLiteSession"](
            _user_scoped_session_id(user_id, session_id),
            str(session_path),
        )
    except Exception:
        return False

    for method_name in ("clear_session", "clear"):
        method = getattr(session, method_name, None)
        if not callable(method):
            continue
        try:
            await _maybe_await(method())
            return True
        except Exception:
            logger.debug("Failed to clear console agent SDK session via %s", method_name, exc_info=True)
            return False
    return False


def _default_local_mcp_server() -> dict[str, Any]:
    return {
        "name": "metacode",
        "url": f"http://127.0.0.1:{settings.mcp_port}{settings.mcp_path}",
        "transport": "streamable_http",
        "enabled": True,
        "required": True,
        "timeout": float(settings.console_agent_mcp_timeout or 180.0),
        "excluded_tools": [
            str(tool).strip()
            for tool in (settings.console_agent_local_mcp_excluded_tools or [])
            if str(tool).strip()
        ],
    }


def get_console_agent_mcp_servers() -> list[dict[str, Any]]:
    """Return normalized MCP server definitions for the console agent."""
    raw_servers = [_default_local_mcp_server(), *(settings.console_agent_external_mcp_servers or [])]

    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_servers):
        if not isinstance(raw, dict):
            raise ConsoleAgentUnavailable("CONSOLE_AGENT_EXTERNAL_MCP_SERVERS must contain objects")

        name = str(raw.get("name") or "").strip()
        if not name:
            raise ConsoleAgentUnavailable("Each console agent MCP server requires a name")
        if index > 0 and name == "metacode":
            raise ConsoleAgentUnavailable(
                "External console agent MCP server name 'metacode' is reserved for the local MCP server"
            )

        enabled = bool(raw.get("enabled", True))
        transport = str(raw.get("transport") or "streamable_http").strip().lower()
        if transport != "streamable_http":
            raise ConsoleAgentUnavailable(
                f"Unsupported console agent MCP transport for {name!r}: {transport!r}"
            )

        url = str(raw.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ConsoleAgentUnavailable(f"Console agent MCP server {name!r} requires http(s) url")

        headers = raw.get("headers") or {}
        if not isinstance(headers, dict):
            raise ConsoleAgentUnavailable(f"Console agent MCP server {name!r} headers must be an object")

        excluded_tools = raw.get("excluded_tools") or []
        if not isinstance(excluded_tools, list):
            raise ConsoleAgentUnavailable(
                f"Console agent MCP server {name!r} excluded_tools must be a list"
            )

        required = bool(raw.get("required", index == 0))
        timeout = float(raw.get("timeout") or settings.console_agent_mcp_timeout or 180.0)

        normalized.append({
            "name": name,
            "url": url,
            "transport": transport,
            "enabled": enabled,
            "required": required,
            "headers": {str(k): str(v) for k, v in headers.items()},
            "timeout": timeout,
            "excluded_tools": [str(t).strip() for t in excluded_tools if str(t).strip()],
        })

    return normalized


def validate_agent_available() -> None:
    if not settings.console_agent_enabled:
        raise ConsoleAgentUnavailable("feature_disabled")
    try:
        agent_llm.resolve_agent_llm_profile()
        get_console_agent_mcp_servers()
    except agent_llm.AgentLlmConfigError as exc:
        raise ConsoleAgentUnavailable(str(exc)) from exc


_LOCAL_MCP_SERVER_NAME = "metacode"


def _sdk_safe_tool_name_part(value: str, fallback: str) -> str:
    safe = "".join(
        char if char.isascii() and (char.isalnum() or char in {"_", "-"}) else "_"
        for char in value
    )
    safe = safe.strip("_-")
    return safe or fallback


def _sdk_prefixed_mcp_tool_name(server_name: str, tool_name: str) -> str:
    server_part = _sdk_safe_tool_name_part(server_name, "server")
    tool_part = _sdk_safe_tool_name_part(tool_name, "tool")
    return f"mcp_{server_part}__{tool_part}"


def _tool_visibility_registered_tools() -> list[Any]:
    from mcpsrv import tool_visibility

    return list(tool_visibility.list_registered_tools())


def _local_mcp_tool_name_map() -> dict[str, str]:
    """Build local MCP tool names from the in-process registry snapshot.

    This intentionally does not call MCP list_tools over HTTP. The agent only
    needs names for prompt hints, and tool_visibility already knows the local
    FastMCP registry after startup.
    """
    try:
        registered_tools = _tool_visibility_registered_tools()
    except Exception:
        logger.debug("Failed to read local MCP tool registry for console agent prompt", exc_info=True)
        return {}

    excluded = {
        str(tool).strip()
        for tool in (settings.console_agent_local_mcp_excluded_tools or [])
        if str(tool).strip()
    }
    mapping: dict[str, str] = {}
    for item in registered_tools:
        if not bool(getattr(item, "enabled", True)):
            continue
        tool = getattr(item, "tool", item)
        name = str(getattr(tool, "name", "") or "").strip()
        if not name or name in excluded:
            continue
        mapping[name] = _sdk_prefixed_mcp_tool_name(_LOCAL_MCP_SERVER_NAME, name)
    return mapping


def _tool_name(tool_names: dict[str, str] | None, base_name: str) -> str | None:
    if tool_names is None:
        return base_name
    return tool_names.get(base_name)


def _join_tool_names(tool_names: dict[str, str] | None, base_names: Iterable[str]) -> str:
    names = [
        name
        for base_name in base_names
        if (name := _tool_name(tool_names, base_name))
    ]
    return ", ".join(names)


def _build_instructions(tool_names: dict[str, str] | None = None) -> str:
    get_metadata = _tool_name(tool_names, "get_metadata")
    overview_tools = _join_tool_names(
        tool_names,
        ["inspect_metadata_object", "get_metadata_object_structure", "get_metadata_details"],
    )
    search_tools = _join_tool_names(
        tool_names,
        ["find_metadata_objects", "find_metadata_elements", "find_objects_by_summary", "get_metadata"],
    )
    usage_tools = _join_tool_names(tool_names, ["find_metadata_usages", "find_dependency_paths"])
    structure_tool = _tool_name(tool_names, "get_metadata_object_structure")
    form_tools = _join_tool_names(tool_names, ["get_form_structure", "find_form_links"])
    access_tools = _join_tool_names(tool_names, ["get_access_rights"])
    subscription_tools = _join_tool_names(tool_names, ["get_event_subscriptions"])
    bsl_tools = _join_tool_names(
        tool_names,
        [
            "get_bsl_modules",
            "search_bsl_routines",
            "search_bsl_code",
            "get_bsl_routine_body",
            "get_bsl_call_graph",
        ],
    )
    extension_tools = _join_tool_names(tool_names, ["get_extension_object_diff"])
    get_bsl_modules = _tool_name(tool_names, "get_bsl_modules")

    tool_lines: list[str] = []
    if get_metadata:
        tool_lines.append(
            f"- обзор проекта, конфигураций или категорий: {get_metadata} mode='summary', "
            "mode='configurations' или mode='categories';"
        )
    if overview_tools:
        tool_lines.append(f"- обзор объекта: {overview_tools};")
    if search_tools:
        tool_lines.append(f"- поиск объекта по смыслу или элементам: {search_tools};")
    if usage_tools:
        tool_lines.append(f"- использования и зависимости: {usage_tools};")
    if structure_tool and form_tools:
        tool_lines.append(
            f"- формы: сначала {structure_tool} для полного списка форм объекта, затем "
            f"{form_tools} для нужных форм;"
        )
    if access_tools:
        tool_lines.append(f"- права: {access_tools};")
    if subscription_tools:
        tool_lines.append(f"- подписки: {subscription_tools};")
    if bsl_tools:
        tool_lines.append(f"- BSL модули и процедуры: {bsl_tools};")
    if extension_tools:
        tool_lines.append(f"- расширения: {extension_tools}.")

    tool_routing = "\n".join(tool_lines)
    get_metadata_strategy = get_metadata or (
        "get_metadata" if tool_names is None else "доступный tool обзора метаданных"
    )
    get_bsl_modules_strategy = get_bsl_modules or (
        "get_bsl_modules" if tool_names is None else "доступный BSL tool для списка модулей"
    )

    return (
        "Тебя зовут Метакод. Ты эксперт по анализу конфигураций 1С:Предприятие.\n"
        "\n"
        "Твоя задача — помогать разработчику разбираться в метаданных, модулях, формах, "
        "правах, связях объектов, использовании процедур и причинах поведения системы.\n"
        "\n"
        "Отвечай на языке пользователя. Если язык запроса не очевиден, используй русский. "
        "Отвечай по делу: сначала вывод, затем проверенные факты, найденные объекты/модули/"
        "процедуры и, если нужно, следующие шаги проверки.\n"
        "Не добавляй в конец ответа предложения в стиле 'если хочешь, могу ...', если "
        "пользователь прямо не просил продолжение или варианты следующего действия.\n"
        "\n"
        "Не утверждай наличие объектов, реквизитов, табличных частей, форм, команд, ролей, "
        "прав, процедур, вызовов или связей, пока это не подтверждено tools или переданным "
        "контекстом.\n"
        "\n"
        "Если в сообщении есть контекст текущего выбора: объект, раздел, модуль, вкладка "
        "или поисковая строка — используй его для ссылок вроде 'этот объект', 'текущая форма', "
        "'этот модуль', 'здесь'. Если контекста мало, найди нужные данные через tools или "
        "попроси уточнить объект/модуль.\n"
        "Если в контексте есть точный selected_ref, qualified_name или module_id, используй "
        "это значение напрямую. Не ищи этот же объект через semantic search, пока пользователь "
        "не просит найти объект по смыслу или пока точная ссылка не оказалась невалидной.\n"
        "Не считай page, active_tab, selected_section или search_query именем объекта. Если "
        "пользователь спрашивает про 'текущий объект', а selected_ref/qualified_name в контексте "
        "нет, не вызывай object tools по вкладке или странице — попроси выбрать или назвать объект.\n"
        "\n"
        "Выбирай tools по задаче:\n"
        f"{tool_routing}\n"
        "Имена tools могут быть дополнены префиксом MCP-сервера; ориентируйся на смысл имени.\n"
        "\n"
        "Не подменяй задачу смежной темой. Если пользователь просит обзор проекта, список "
        "конфигураций или категории объектов, не переходи к правам, пользователям, формам или "
        "конкретному объекту без отдельного запроса. Не используй semantic search по объектам "
        "для таких вопросов: достаточно get_metadata.\n"
        "\n"
        "Для сложных исследовательских задач используй structured planning tools. Сложная задача — это "
        "диагностика причины поведения, поиск точки изменения, оценка риска доработки, разбор "
        "цепочки вызовов, анализ влияния изменения, поиск места ошибки или вопрос 'как доработать'. "
        "Planning tools — это set_plan, update_plan_step и complete_plan; не добавляй к ним MCP-префикс. "
        "Для таких задач сначала вызови set_plan с 3-6 шагами проверки. Затем перед группой "
        "проверок отмечай текущий шаг через update_plan_step(status='in_progress'), после "
        "проверки отмечай status='done'. В конце вызови complete_plan с коротким резюме. "
        "update_plan_step используй для крупных фаз проверки, но не после каждого отдельного "
        "MCP-вызова. Не раздувай количество planning updates: лучше 2-5 осмысленных обновлений, "
        "чем обновление после каждого tool result. Перед complete_plan незавершенные шаги должны "
        "быть понятны по итоговому резюме.\n"
        "Не печатай план обычным текстом, если planning tools доступны: план будет показан UI "
        "отдельно. После tools дай итог по найденным фактам, точкам изменения, рискам и тому, "
        "что не удалось подтвердить.\n"
        "\n"
        "Не используй planning для простых точечных вопросов: что это за объект, где используется, "
        "какие формы, какие права, какие модули, какие процедуры. Для них сразу вызывай нужные "
        "tools и отвечай кратко.\n"
        "\n"
        "Типовые стратегии:\n"
        "- 'обзор проекта' / 'какие конфигурации' / 'какие категории': получи summary или "
        f"categories через {get_metadata_strategy} и ответь именно по составу проекта;\n"
        "- 'что это' / 'объясни объект': если есть точная ссылка на объект, используй ее; "
        "сначала получи обзор, структуру, формы, BSL и связи, потом дай краткое назначение "
        "и важные места;\n"
        "- 'где используется': проверь usages/dependency paths, для процедур дополнительно "
        "call graph;\n"
        "- 'разбери форму': получи структуру формы, события, обработчики и привязки элементов;\n"
        "- 'разбери формы' / 'какие формы': сначала перечисли все формы из структуры объекта. "
        "Если детально проверена только часть форм, явно отдели 'проверенные формы' от "
        "'остальных найденных форм' и не называй частичный список полным;\n"
        "- 'разбери код' / 'где логика': найди модуль/процедуры, при необходимости получи тело "
        "конкретной процедуры;\n"
        f"- 'какие модули' / 'модули объекта': используй {get_bsl_modules_strategy}. Не заменяй этот запрос "
        "структурой объекта: формы и реквизиты не являются списком BSL-модулей;\n"
        "- 'права': проверь роли и права к объекту, отделяй чтение/изменение/проведение/"
        "интерактивные права;\n"
        "- 'как доработать' / 'что менять': сначала дай план проверки, затем найди точки "
        "изменения, формы, модули, процедуры и зависимости. В итоговом ответе отдели "
        "'что менять', 'что проверить' и 'риски'.\n"
        "\n"
        "Перед финальным ответом сделай внутреннюю проверку качества, но не печатай чеклист: "
        "ответил ли ровно на вопрос; все ли важные утверждения подтверждены tools или контекстом; "
        "нет ли подмены темы; не пропущены ли формы/права/BSL/связи, если они нужны по задаче; "
        "не выведены ли лишние сырой JSON, полный код или нерелевантные детали; явно ли сказано, "
        "какие данные не удалось проверить.\n"
        "\n"
        "Если найденные данные неполные или противоречивые, прямо укажи, что удалось проверить, "
        "а что нет.\n"
        "\n"
        "Если в context или tool results уже есть точный qualified_name/ref/qn/module_id, оформляй "
        "каждое упоминание соответствующей сущности как markdown-ссылку для web console: объекты, "
        "реквизиты, стандартные реквизиты, табличные части, реквизиты табличных частей, формы, "
        "элементы формы, реквизиты формы, команды, подписки, предопределенные значения, модули "
        "и процедуры/функции, если для них есть полный ref или module_id. Не ограничивайся только "
        "объектами метаданных и не пропускай реквизиты, если их qualified_name есть в результате. "
        "Не вызывай дополнительные tools только ради ссылок. Если tool result использует compact "
        "aliases вида @qn:1, @p:1 или @config:1, сначала раскрой их по секциям qn, prefixes и "
        "configs из этого же результата и в ссылку подставляй полный qualified_name/ref, а не alias.\n"
        "Форматы внутренних ссылок:\n"
        "- объект: [Имя](metacode://open?kind=object&ref=<qualified_name>&tab=summary);\n"
        "- объект на вкладке: [Права](metacode://open?kind=object&ref=<qualified_name>&tab=access), "
        "tab может быть summary, properties, relationships или access;\n"
        "- элемент/форма/реквизит/табличная часть: [Имя](metacode://open?kind=node&ref=<qualified_name>);\n"
        "- раздел объекта: [Раздел](metacode://open?kind=section&ref=<qualified_name>&section=<section>);\n"
        "- модуль: [Модуль](metacode://open?kind=module&module_id=<module_id>&owner_ref=<owner_qn>&module_type=<module_type>).\n"
        "\n"
        "Не пересказывай сырые JSON/BSL целиком. Цитируй только нужные фрагменты, имена методов, "
        "объектов и ключевые условия. Большие результаты tools сжимай до сути."
    )


def _build_user_input(message: str, context: dict[str, Any]) -> str:
    context_json = json.dumps(context or {}, ensure_ascii=False, sort_keys=True)
    return (
        "Контекст текущего запроса JSON:\n"
        f"{context_json}\n\n"
        "Вопрос пользователя:\n"
        f"{message.strip()}"
    )


def _make_tool_filter(excluded_tools: Iterable[str]):
    excluded = {str(t) for t in excluded_tools}

    def tool_filter(_context: Any, tool: Any) -> bool:
        return str(getattr(tool, "name", "")) not in excluded

    return tool_filter


def _server_from_tool_name(tool_name: str, server_names: Iterable[str]) -> str:
    name = str(tool_name or "")
    for server in server_names:
        if (
            name == server
            or name.startswith(f"{server}_")
            or name.startswith(f"{server}__")
            or name.startswith(f"mcp_{server}__")
        ):
            return server
    return ""


def _display_tool_name(tool_name: str, server_names: Iterable[str]) -> str:
    name = str(tool_name or "")
    for server in server_names:
        prefixes = (f"mcp_{server}__", f"{server}__", f"{server}_")
        for prefix in prefixes:
            if name.startswith(prefix):
                return name[len(prefix):] or name
    return name


def _extract_tool_name(item: Any) -> str:
    for obj in (item, getattr(item, "raw_item", None), getattr(item, "tool_call", None)):
        if obj is None:
            continue
        for attr in ("name", "tool_name"):
            value = getattr(obj, attr, None)
            if value:
                return str(value)
        function = getattr(obj, "function", None)
        value = getattr(function, "name", None) if function is not None else None
        if value:
            return str(value)
    return ""


def _extract_tool_arguments(item: Any) -> str:
    for obj in (item, getattr(item, "raw_item", None), getattr(item, "tool_call", None)):
        if obj is None:
            continue
        value = getattr(obj, "arguments", None)
        if value:
            return _preview(value, limit=600)
        function = getattr(obj, "function", None)
        value = getattr(function, "arguments", None) if function is not None else None
        if value:
            return _preview(value, limit=600)
    return ""


def _extract_text_delta(event: Any) -> str:
    data = getattr(event, "data", None)
    if data is None:
        return ""
    data_type = str(getattr(data, "type", "") or "")
    if data_type not in {"response.output_text.delta", "response.refusal.delta"}:
        return ""
    delta = getattr(data, "delta", None)
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        text = delta.get("text") or delta.get("content")
        return str(text or "")
    return ""


def _extract_reasoning_delta(event: Any) -> str:
    data = getattr(event, "data", None)
    if data is None:
        return ""
    data_type = str(getattr(data, "type", "") or "")
    if "reasoning" not in data_type or not data_type.endswith(".delta"):
        return ""
    delta = getattr(data, "delta", None)
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        text = delta.get("text") or delta.get("content")
        return str(text or "")
    return ""


def _extract_item_output(item: Any) -> str:
    for attr in ("output", "content"):
        value = getattr(item, attr, None)
        if value:
            return _tool_output_preview(value)
    raw = getattr(item, "raw_item", None)
    for attr in ("output", "content"):
        value = getattr(raw, attr, None) if raw is not None else None
        if value:
            return _tool_output_preview(value)
    return _tool_output_preview(item)


def _finalize_plan_state(plan_state: dict[str, Any], summary: str = "") -> dict[str, Any]:
    for step in plan_state.get("steps", []):
        if step.get("status") in {"pending", "in_progress"}:
            step["status"] = "done"
    return {
        "summary": str(summary or "").strip()[:800],
        "steps": plan_state.get("steps", []),
    }


def _planning_tool_alias_prefixes(server_names: Iterable[str] | None) -> list[str]:
    prefixes: list[str] = []
    for server_name in server_names or []:
        server = str(server_name or "").strip()
        if not server or not _TOOL_NAME_RE.match(server):
            continue
        prefixes.append(f"mcp_{server}__")
    return prefixes


def _build_planning_tools(
    sdk: dict[str, Any],
    server_names: Iterable[str] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    plan_state: dict[str, Any] = {"steps": []}
    function_tool = sdk.get("function_tool")
    if function_tool is None:
        return [], plan_state

    def set_plan_impl(steps: list[str]) -> str:
        normalized = [
            {
                "id": str(index + 1),
                "title": str(step).strip()[:240],
                "status": "pending",
                "note": "",
            }
            for index, step in enumerate(steps[:6])
            if str(step).strip()
        ]
        plan_state["steps"] = normalized
        return json.dumps({"steps": normalized}, ensure_ascii=False)

    def update_plan_step_impl(
        step_id: str,
        status: Literal["pending", "in_progress", "done", "blocked"],
        note: str = "",
    ) -> str:
        step_key = str(step_id).strip()
        found = None
        for step in plan_state.get("steps", []):
            if str(step.get("id")) == step_key:
                found = step
                break
        if found is None:
            found = {
                "id": step_key or str(len(plan_state.get("steps", [])) + 1),
                "title": "",
                "status": "pending",
                "note": "",
            }
            plan_state.setdefault("steps", []).append(found)
        found["status"] = status
        found["note"] = str(note or "").strip()[:500]
        return json.dumps(found, ensure_ascii=False)

    def complete_plan_impl(summary: str = "") -> str:
        return json.dumps(_finalize_plan_state(plan_state, summary), ensure_ascii=False)

    @function_tool(
        name_override="set_plan",
        description_override=(
            "Create a structured checklist for complex analysis tasks. "
            "Use only for diagnostics, change planning, impact analysis, or call-chain analysis."
        ),
    )
    def set_plan(steps: list[str]) -> str:
        return set_plan_impl(steps)

    @function_tool(
        name_override="update_plan_step",
        description_override="Update one step in the structured analysis checklist.",
    )
    def update_plan_step(
        step_id: str,
        status: Literal["pending", "in_progress", "done", "blocked"],
        note: str = "",
    ) -> str:
        return update_plan_step_impl(step_id, status, note)

    @function_tool(
        name_override="complete_plan",
        description_override="Mark the structured analysis checklist as complete with a short summary.",
    )
    def complete_plan(summary: str = "") -> str:
        return complete_plan_impl(summary)

    tools = [set_plan, update_plan_step, complete_plan]

    def make_set_plan_alias(name: str) -> Any:
        @function_tool(
            name_override=name,
            description_override="Alias for set_plan when a model mistakenly adds the MCP server prefix.",
        )
        def set_plan_alias(steps: list[str]) -> str:
            return set_plan_impl(steps)

        return set_plan_alias

    def make_update_plan_step_alias(name: str) -> Any:
        @function_tool(
            name_override=name,
            description_override="Alias for update_plan_step when a model mistakenly adds the MCP server prefix.",
        )
        def update_plan_step_alias(
            step_id: str,
            status: Literal["pending", "in_progress", "done", "blocked"],
            note: str = "",
        ) -> str:
            return update_plan_step_impl(step_id, status, note)

        return update_plan_step_alias

    def make_complete_plan_alias(name: str) -> Any:
        @function_tool(
            name_override=name,
            description_override="Alias for complete_plan when a model mistakenly adds the MCP server prefix.",
        )
        def complete_plan_alias(summary: str = "") -> str:
            return complete_plan_impl(summary)

        return complete_plan_alias

    for prefix in _planning_tool_alias_prefixes(server_names):
        tools.extend([
            make_set_plan_alias(f"{prefix}set_plan"),
            make_update_plan_step_alias(f"{prefix}update_plan_step"),
            make_complete_plan_alias(f"{prefix}complete_plan"),
        ])

    return tools, plan_state


def _planning_sse_event(tool_name: str, output: str) -> tuple[str, dict[str, Any]] | None:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = {"message": output}
    if not isinstance(payload, dict):
        payload = {"message": str(payload)}

    if tool_name == "set_plan":
        steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
        return "plan", {"steps": steps}
    if tool_name == "update_plan_step":
        return "plan_step", payload
    if tool_name == "complete_plan":
        return "plan_done", payload
    return None


async def _build_mcp_servers(stack: AsyncExitStack, sdk: dict[str, Any]) -> list[Any]:
    servers = []
    for server_def in get_console_agent_mcp_servers():
        if not server_def["enabled"]:
            continue

        params: dict[str, Any] = {
            "url": server_def["url"],
            "timeout": server_def["timeout"],
        }
        if server_def["headers"]:
            params["headers"] = server_def["headers"]

        try:
            server = sdk["MCPServerStreamableHttp"](
                name=server_def["name"],
                params=params,
                client_session_timeout_seconds=server_def["timeout"],
                cache_tools_list=True,
                max_retry_attempts=2,
                tool_filter=_make_tool_filter(server_def["excluded_tools"]),
                require_approval="never",
            )
            servers.append(await stack.enter_async_context(server))
        except Exception:
            if server_def["required"]:
                raise
            logger.warning("Optional MCP server %s is unavailable", server_def["name"], exc_info=True)
    if not servers:
        raise ConsoleAgentUnavailable("No console agent MCP servers are available")
    return servers


def _load_agents_sdk() -> dict[str, Any]:
    try:
        from agents import (  # type: ignore
            Agent,
            AsyncOpenAI,
            ModelSettings,
            OpenAIChatCompletionsModel,
            RunConfig,
            Runner,
            SessionSettings,
            SQLiteSession,
            function_tool,
            set_tracing_disabled,
        )
        from agents.extensions import ToolOutputTrimmer  # type: ignore
        from agents.mcp import MCPServerStreamableHttp  # type: ignore
        from openai.types.shared.reasoning import Reasoning  # type: ignore
    except ImportError as exc:
        raise ConsoleAgentUnavailable(f"openai-agents package is not installed: {exc}") from exc
    return {
        "Agent": Agent,
        "AsyncOpenAI": AsyncOpenAI,
        "MCPServerStreamableHttp": MCPServerStreamableHttp,
        "ModelSettings": ModelSettings,
        "OpenAIChatCompletionsModel": OpenAIChatCompletionsModel,
        "Reasoning": Reasoning,
        "RunConfig": RunConfig,
        "Runner": Runner,
        "SessionSettings": SessionSettings,
        "SQLiteSession": SQLiteSession,
        "ToolOutputTrimmer": ToolOutputTrimmer,
        "function_tool": function_tool,
        "set_tracing_disabled": set_tracing_disabled,
    }


def _positive_int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _is_empty_assistant_output_message(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("type") != "message" or item.get("role") != "assistant":
        return False
    if item.get("tool_calls"):
        return False

    content = item.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return not content
    if not isinstance(content, list):
        return False
    if not content:
        return True

    for part in content:
        if not isinstance(part, dict):
            return False
        part_type = str(part.get("type") or "")
        if part_type in {"output_text", "text"}:
            if str(part.get("text") or ""):
                return False
            continue
        if part_type == "refusal":
            if str(part.get("refusal") or ""):
                return False
            continue
        return False
    return True


def _assistant_output_text(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") != "message" or item.get("role") != "assistant":
        return None

    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            return None
        part_type = str(part.get("type") or "")
        if part_type in {"output_text", "text"}:
            parts.append(str(part.get("text") or ""))
        elif part_type == "refusal":
            parts.append(str(part.get("refusal") or ""))
        else:
            return None
    return "\n".join(text for text in parts if text)


def _normalize_replay_items_for_chat_completions(items: Iterable[Any]) -> tuple[list[Any], bool]:
    original_items = list(items)
    output_call_ids = {
        str(item.get("call_id"))
        for item in original_items
        if isinstance(item, dict)
        and item.get("type") == "function_call_output"
        and item.get("call_id")
    }
    kept_call_ids: set[str] = set()
    cleaned: list[Any] = []
    changed = False

    for item in original_items:
        if _is_empty_assistant_output_message(item):
            changed = True
            continue

        if isinstance(item, dict) and item.get("type") == "function_call":
            call_id = str(item.get("call_id") or "")
            if not call_id or call_id not in output_call_ids:
                changed = True
                continue
            kept_call_ids.add(call_id)
            cleaned.append(item)
            continue

        if isinstance(item, dict) and item.get("type") == "function_call_output":
            call_id = str(item.get("call_id") or "")
            if not call_id or call_id not in kept_call_ids:
                changed = True
                continue
            cleaned.append(item)
            continue

        assistant_text = _assistant_output_text(item)
        if assistant_text is not None:
            cleaned.append({"role": "assistant", "content": assistant_text})
            changed = True
            continue

        cleaned.append(item)

    return cleaned, changed


class _ConsoleAgentInputFilter:
    """Keep Chat Completions history valid, then apply the optional SDK trimmer."""

    def __init__(self, inner_filter: Any | None = None):
        self.inner_filter = inner_filter

    def __getattr__(self, name: str) -> Any:
        if self.inner_filter is None:
            raise AttributeError(name)
        return getattr(self.inner_filter, name)

    def __call__(self, data: Any) -> Any:
        model_data = data.model_data
        cleaned_items, changed = _normalize_replay_items_for_chat_completions(
            model_data.input or []
        )
        if changed:
            model_data = type(model_data)(input=cleaned_items, instructions=model_data.instructions)
            try:
                data = replace(data, model_data=model_data)
            except TypeError:
                data = SimpleNamespace(
                    model_data=model_data,
                    agent=getattr(data, "agent", None),
                    context=getattr(data, "context", None),
                )

        if self.inner_filter is not None:
            return self.inner_filter(data)
        return model_data


def _build_run_config(sdk: dict[str, Any]) -> Any:
    if "RunConfig" not in sdk:
        return None

    session_limit = _positive_int_or_none(settings.console_agent_session_item_limit)
    session_settings = (
        sdk["SessionSettings"](limit=session_limit)
        if session_limit and "SessionSettings" in sdk
        else None
    )

    input_filter = None
    if bool(settings.console_agent_tool_output_trimmer_enabled) and "ToolOutputTrimmer" in sdk:
        recent_turns = _positive_int_or_none(settings.console_agent_tool_output_recent_turns) or 3
        max_output_chars = _positive_int_or_none(settings.console_agent_tool_output_max_chars) or 20000
        preview_chars = _positive_int_or_none(settings.console_agent_tool_output_preview_chars) or 20000
        preview_chars = min(preview_chars, max_output_chars)
        input_filter = sdk["ToolOutputTrimmer"](
            recent_turns=recent_turns,
            max_output_chars=max_output_chars,
            preview_chars=preview_chars,
        )
    input_filter = _ConsoleAgentInputFilter(input_filter)

    return sdk["RunConfig"](
        session_settings=session_settings,
        call_model_input_filter=input_filter,
        tool_not_found_behavior="return_error_to_model",
    )


_REASONING_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh"}
_REASONING_SUMMARY_VALUES = {"auto", "concise", "detailed"}


def _setting_choice(value: Any, allowed: set[str]) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    return text if text in allowed else None


def _build_model_settings(
    sdk: dict[str, Any],
    profile: agent_llm.AgentLlmProfile | None = None,
) -> Any:
    if profile is None:
        profile = agent_llm.get_agent_llm_profile()
    reasoning = None
    effort = _setting_choice(profile.reasoning_effort, _REASONING_EFFORT_VALUES)
    summary = _setting_choice(profile.reasoning_summary, _REASONING_SUMMARY_VALUES)
    if (effort or summary) and "Reasoning" in sdk:
        reasoning = sdk["Reasoning"](effort=effort, summary=summary)
    return sdk["ModelSettings"](
        temperature=float(profile.temperature if profile.temperature is not None else 0.0),
        reasoning=reasoning,
    )


def _is_openrouter_base(api_base: str | None) -> bool:
    return "openrouter.ai" in str(api_base or "").lower()


async def _estimate_openrouter_model_cost(
    *,
    api_base: str | None,
    api_key: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    http_client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    if not model or not _is_openrouter_base(api_base):
        return {"cost_amount": None, "cost_unit": None, "cost_source": "unavailable"}

    cache_key = f"{str(api_base or '').rstrip('/')}|{model}"
    pricing = _MODEL_PRICING_CACHE.get(cache_key)
    close_client = False
    client = http_client
    if pricing is None:
        if client is None:
            if httpx is None:
                return {"cost_amount": None, "cost_unit": None, "cost_source": "unavailable"}
            client = httpx.AsyncClient(timeout=30.0)
            close_client = True
        try:
            base = str(api_base or "https://openrouter.ai/api/v1").rstrip("/")
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            response = await client.get(f"{base}/models", headers=headers)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict) or item.get("id") != model:
                        continue
                    raw_pricing = item.get("pricing") or {}
                    prompt_price = float(raw_pricing.get("prompt") or 0)
                    completion_price = float(raw_pricing.get("completion") or 0)
                    pricing = (prompt_price, completion_price)
                    _MODEL_PRICING_CACHE[cache_key] = pricing
                    break
        except Exception as exc:
            logger.info("OpenRouter model pricing unavailable for %s: %s", model, exc)
        finally:
            if close_client:
                await client.aclose()

    if not pricing:
        return {"cost_amount": None, "cost_unit": None, "cost_source": "openrouter_pricing_unavailable"}

    prompt_price, completion_price = pricing
    cost = max(0, int(input_tokens or 0)) * prompt_price
    cost += max(0, int(output_tokens or 0)) * completion_price
    return {
        "cost_amount": cost if cost > 0 else None,
        "cost_unit": "usd",
        "cost_source": "openrouter_pricing_estimate",
    }


def _record_console_agent_runtime_usage(
    *,
    turn_usage: dict[str, Any] | None,
    provider: str,
    model: str,
    success: bool,
    duration_ms: int,
    user_id: str | None = None,
    login: str | None = None,
) -> None:
    usage = turn_usage or {}
    runtime_metrics.record_llm_usage(
        event_type="console_agent.llm",
        provider=provider or "unknown",
        model=model or "unknown",
        calls=int(usage.get("requests") or 1),
        success=success,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        total_tokens=usage.get("total_tokens"),
        cost_amount=usage.get("cost_amount"),
        cost_unit=usage.get("cost_unit"),
        cost_source=str(usage.get("cost_source") or "unknown"),
        duration_ms=duration_ms,
        actor_id=user_id,
        actor_login=login,
    )


async def stream_console_agent(
    *,
    session_id: str,
    message: str,
    context: dict[str, Any] | None = None,
    user_id: str | None = None,
    login: str | None = None,
    llm_profile_id: str | None = None,
) -> AsyncIterator[str]:
    """Run one streaming console-agent turn and yield stable SSE frames."""
    if not settings.console_agent_enabled:
        raise ConsoleAgentUnavailable("feature_disabled")
    try:
        llm_profile = agent_llm.resolve_agent_llm_profile(llm_profile_id)
        get_console_agent_mcp_servers()
    except agent_llm.AgentLlmConfigError as exc:
        raise ConsoleAgentUnavailable(str(exc)) from exc
    sdk = _load_agents_sdk()

    public_sid = _clean_session_id(session_id)
    sid = _user_scoped_session_id(user_id, public_sid)
    yield _sse("start", {
        "session_id": public_sid,
        "llm_profile": llm_profile.usage_dict(),
    })

    api_key = llm_profile.api_key
    api_base = llm_profile.api_base
    proxy = llm_profile.proxy
    runtime_provider = (
        llm_profile.endpoint_id
        if llm_profile.mode == "config_file"
        else runtime_metrics.detect_provider_from_api_base(api_base, fallback="openai-compatible")
    )
    if proxy and httpx is None:
        raise ConsoleAgentUnavailable("httpx is required for CONSOLE_AGENT_LLM_PROXY")
    http_client = httpx.AsyncClient(proxy=proxy) if proxy else None
    run_started = time.perf_counter()
    runtime_usage_recorded = False

    try:
        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": float(llm_profile.timeout or 120.0),
        }
        if api_base:
            client_kwargs["base_url"] = api_base
        if http_client:
            client_kwargs["http_client"] = http_client

        if api_base or llm_profile.api_key_env != "OPENAI_API_KEY":
            sdk["set_tracing_disabled"](True)

        openai_client = sdk["AsyncOpenAI"](**client_kwargs)
        model = sdk["OpenAIChatCompletionsModel"](
            model=llm_profile.model,
            openai_client=openai_client,
        )
        model_settings = _build_model_settings(sdk, llm_profile)

        session_path = Path(settings.console_agent_session_sqlite_path)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        session = sdk["SQLiteSession"](sid, str(session_path))
        await _sanitize_session_console_links(session)
        user_input = _build_user_input(message, context or {})
        session_usage = _get_session_usage(session_path, sid)
        try:
            history_items = await _maybe_await(session.get_items())
        except Exception:
            history_items = []
        run_config = _build_run_config(sdk)

        async with AsyncExitStack() as stack:
            mcp_servers = await _build_mcp_servers(stack, sdk)
            server_names = [str(getattr(server, "name", "")) for server in mcp_servers]
            planning_tools, plan_state = _build_planning_tools(sdk, server_names)
            local_tool_names = _local_mcp_tool_name_map()
            instructions = _build_instructions(local_tool_names)
            estimated_context_tokens = _token_estimate({
                "instructions": instructions,
                "history": history_items,
                "input": user_input,
            }, model_name=llm_profile.model)
            yield _sse("usage", {
                "session": session_usage,
                "model": llm_profile.model,
                "llm_profile": llm_profile.usage_dict(),
                "context": {
                    "tokens": estimated_context_tokens,
                    "source": "estimated_local",
                },
            })
            agent = sdk["Agent"](
                name="Метакод",
                instructions=instructions,
                model=model,
                model_settings=model_settings,
                tools=planning_tools,
                mcp_servers=mcp_servers,
                mcp_config={
                    "include_server_in_tool_names": True,
                    "convert_schemas_to_strict": True,
                },
            )
            run_result = sdk["Runner"].run_streamed(
                agent,
                input=user_input,
                max_turns=max(12, int(settings.console_agent_max_turns or 12)),
                session=session,
                run_config=run_config,
            )

            pending_tool_names: deque[str] = deque()
            plan_completed = False
            async for event in run_result.stream_events():
                event_type = getattr(event, "type", "")
                if event_type == "raw_response_event":
                    if bool(llm_profile.show_reasoning):
                        reasoning_delta = _extract_reasoning_delta(event)
                        if reasoning_delta:
                            yield _sse("reasoning_delta", {"text": reasoning_delta})
                            continue
                    delta = _extract_text_delta(event)
                    if delta:
                        yield _sse("delta", {"text": delta})
                    continue
                if event_type != "run_item_stream_event":
                    continue

                name = str(getattr(event, "name", "") or "")
                item = getattr(event, "item", None)
                item_type = str(getattr(item, "type", "") or "")
                if name == "tool_called" or item_type == "tool_call_item":
                    tool_name = _extract_tool_name(item)
                    if tool_name:
                        pending_tool_names.append(tool_name)
                    display_name = _display_tool_name(tool_name, server_names)
                    if display_name in _PLAN_TOOL_NAMES:
                        continue
                    yield _sse("tool_call", {
                        "name": display_name,
                        "server": _server_from_tool_name(tool_name, server_names),
                        "arguments_preview": _extract_tool_arguments(item),
                    })
                elif name == "tool_output" or item_type == "tool_call_output_item":
                    tool_name = _extract_tool_name(item)
                    if pending_tool_names and not tool_name:
                        tool_name = pending_tool_names.popleft()
                    elif pending_tool_names and pending_tool_names[0] == tool_name:
                        pending_tool_names.popleft()
                    display_name = _display_tool_name(tool_name, server_names)
                    output = _extract_item_output(item)
                    if display_name in _PLAN_TOOL_NAMES:
                        plan_event = _planning_sse_event(display_name, output)
                        if plan_event:
                            if plan_event[0] == "plan_done":
                                plan_completed = True
                            yield _sse(plan_event[0], plan_event[1])
                        continue
                    status = "error" if output.lower().startswith("error") else "ok"
                    yield _sse("tool_result", {
                        "name": display_name,
                        "server": _server_from_tool_name(tool_name, server_names),
                        "status": status,
                        "preview": output,
                    })

            final_output = getattr(run_result, "final_output", "") or ""
            if plan_state.get("steps") and not plan_completed:
                yield _sse("plan_done", _finalize_plan_state(plan_state))
            usage = getattr(getattr(run_result, "context_wrapper", None), "usage", None)
            turn_usage = _sdk_usage_dict(usage)
            cost_usage = await _estimate_openrouter_model_cost(
                api_base=api_base,
                api_key=api_key,
                model=llm_profile.model,
                input_tokens=turn_usage["input_tokens"],
                output_tokens=turn_usage["output_tokens"],
                http_client=http_client,
            )
            turn_usage.update(cost_usage)
            session_usage = _add_session_usage(
                session_path,
                sid,
                turn_usage,
                user_id=user_id,
                login=login,
                llm_profile=llm_profile.usage_dict(),
            )
            _record_console_agent_runtime_usage(
                turn_usage=turn_usage,
                provider=runtime_provider,
                model=llm_profile.model,
                success=True,
                duration_ms=int((time.perf_counter() - run_started) * 1000),
                user_id=user_id,
                login=login,
            )
            runtime_usage_recorded = True
            yield _sse("usage", {
                "turn": {
                    "requests": turn_usage["requests"],
                    "input_tokens": turn_usage["input_tokens"],
                    "output_tokens": turn_usage["output_tokens"],
                    "total_tokens": turn_usage["total_tokens"],
                    "cost_amount": turn_usage.get("cost_amount"),
                    "cost_unit": turn_usage.get("cost_unit"),
                    "cost_source": turn_usage.get("cost_source"),
                },
                "session": session_usage,
                "model": llm_profile.model,
                "llm_profile": llm_profile.usage_dict(),
                "context": {
                    "tokens": turn_usage["current_context_tokens"] or estimated_context_tokens,
                    "source": "api_usage_last_request"
                    if turn_usage["current_context_tokens"]
                    else "estimated_local",
                },
            })
            yield _sse("done", {"answer": str(final_output) if final_output else ""})
    except asyncio.CancelledError:
        logger.info("Console agent stream cancelled")
        raise
    except Exception as exc:
        logger.exception("Console agent stream failed")
        if not runtime_usage_recorded:
            _record_console_agent_runtime_usage(
                turn_usage=None,
                provider=runtime_provider,
                model=llm_profile.model,
                success=False,
                duration_ms=int((time.perf_counter() - run_started) * 1000),
                user_id=user_id,
                login=login,
            )
        exc_name = exc.__class__.__name__.lower()
        if "maxturn" in exc_name or "max_turn" in exc_name:
            yield _sse("error", {
                "error": "max_turns_exceeded",
                "message": (
                    "Агент Метакод уперся в лимит шагов. Попробуйте сузить вопрос "
                    "или увеличьте CONSOLE_AGENT_MAX_TURNS."
                ),
            })
        else:
            yield _sse("error", {"error": "agent_error", "message": str(exc)})
    finally:
        if http_client is not None:
            await http_client.aclose()
