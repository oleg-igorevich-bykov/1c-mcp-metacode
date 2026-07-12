"""Console-facing MCP tool catalog helpers."""

from __future__ import annotations

import inspect
from typing import Any

from mcpsrv.tool_return_docs import TOOL_RETURN_DOCS


class McpToolsUnavailable(RuntimeError):
    """Raised when local MCP tool metadata cannot be read."""


class McpToolNotFound(RuntimeError):
    """Raised when a requested MCP tool is not registered."""


class McpToolToggleFailed(RuntimeError):
    """Raised when MCP tool visibility cannot be changed."""


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            return _json_safe(value.dict())
        except Exception:
            pass
    return str(value)


def _tool_attr(tool: Any, name: str, default: Any = None) -> Any:
    if isinstance(tool, dict):
        return tool.get(name, default)
    return getattr(tool, name, default)


def _param_count(parameters: Any) -> int:
    schema = parameters if isinstance(parameters, dict) else {}
    props = schema.get("properties")
    return len(props) if isinstance(props, dict) else 0


def _normalize_tool(tool: Any, *, enabled: bool = True) -> dict[str, Any]:
    name = str(_tool_attr(tool, "name", "") or "").strip()
    parameters = _json_safe(_tool_attr(tool, "parameters", {}) or {})
    output_schema = _json_safe(_tool_attr(tool, "output_schema", {}) or {})
    return_type = _json_safe(_tool_attr(tool, "return_type", "") or "")
    tags = _json_safe(_tool_attr(tool, "tags", []) or [])
    return_doc = TOOL_RETURN_DOCS.get(name)

    return {
        "name": name,
        "description": str(_tool_attr(tool, "description", "") or ""),
        "parameters": parameters,
        "parameter_count": _param_count(parameters),
        "output_schema": output_schema,
        "return_type": return_type,
        "tags": tags if isinstance(tags, list) else [tags],
        "documented": bool(return_doc),
        "enabled": bool(enabled),
        "return_doc": _json_safe(return_doc or {}),
    }


async def _call_list_tools(mcp: Any) -> list[Any]:
    list_tools = getattr(mcp, "list_tools", None)
    if not callable(list_tools):
        raise McpToolsUnavailable("FastMCP list_tools is not available")
    try:
        result = list_tools(run_middleware=False)
    except TypeError:
        result = list_tools()
    if inspect.isawaitable(result):
        result = await result
    return list(result or [])


async def list_local_mcp_tools() -> dict[str, Any]:
    """Return local FastMCP tools plus hand-written return payload docs."""
    try:
        from mcpsrv import tool_visibility

        registered = tool_visibility.list_registered_tools()
        if registered or tool_visibility.is_initialized():
            tools = [
                _normalize_tool(item.tool, enabled=item.enabled)
                for item in registered
            ]
        else:
            from mcpsrv import server as mcp_server

            tools = [
                _normalize_tool(tool, enabled=True)
                for tool in await _call_list_tools(mcp_server.mcp)
            ]

    except McpToolsUnavailable:
        raise
    except Exception as exc:  # pragma: no cover - message matters more than type here.
        raise McpToolsUnavailable(str(exc) or exc.__class__.__name__) from exc

    tools = [tool for tool in tools if tool["name"]]
    tools.sort(key=lambda item: item["name"])
    undocumented = [tool["name"] for tool in tools if not tool["documented"]]
    enabled_count = sum(1 for tool in tools if tool["enabled"])
    return {
        "count": len(tools),
        "enabled_count": enabled_count,
        "disabled_count": len(tools) - enabled_count,
        "undocumented": undocumented,
        "tools": tools,
    }


async def set_local_mcp_tool_enabled(tool_name: str, enabled: bool) -> dict[str, Any]:
    """Toggle local FastMCP tool visibility and persist the setting."""
    from mcpsrv.tool_visibility import ToolNotRegisteredError

    try:
        from mcpsrv import server as mcp_server
        from mcpsrv.tool_visibility import set_tool_enabled_async

        item = await set_tool_enabled_async(mcp_server.mcp, tool_name, enabled)
        return {"name": str(tool_name), "enabled": bool(item.enabled)}
    except ToolNotRegisteredError as exc:
        raise McpToolNotFound(str(exc)) from exc
    except Exception as exc:
        raise McpToolToggleFailed(str(exc) or exc.__class__.__name__) from exc


__all__ = [
    "McpToolNotFound",
    "McpToolToggleFailed",
    "McpToolsUnavailable",
    "list_local_mcp_tools",
    "set_local_mcp_tool_enabled",
]
