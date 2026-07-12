"""FastMCP middleware that records aggregate local tool usage."""

from __future__ import annotations

import logging
import time
from typing import Any

import runtime_metrics

logger = logging.getLogger(__name__)

try:
    from fastmcp.server.middleware import Middleware
except Exception:  # pragma: no cover - local test env may not install FastMCP
    class Middleware:  # type: ignore[no-redef]
        pass


def _tool_name_from_context(context: Any) -> str:
    message = getattr(context, "message", None)
    name = getattr(message, "name", None)
    if name is None and isinstance(message, dict):
        name = message.get("name")
    return str(name or "unknown")


def _result_is_error(result: Any) -> bool:
    for attr in ("is_error", "isError"):
        value = getattr(result, attr, None)
        if isinstance(value, bool):
            return value
    return False


class RuntimeToolUsageMiddleware(Middleware):
    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        tool_name = _tool_name_from_context(context)
        started_at = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception:
            runtime_metrics.record_mcp_tool_call(
                tool_name=tool_name,
                success=False,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
            raise

        runtime_metrics.record_mcp_tool_call(
            tool_name=tool_name,
            success=not _result_is_error(result),
            duration_ms=int((time.perf_counter() - started_at) * 1000),
        )
        return result


def install_tool_usage_metrics(mcp: Any) -> None:
    if getattr(mcp, "_runtime_tool_usage_metrics_installed", False):
        return
    add_middleware = getattr(mcp, "add_middleware", None)
    if not callable(add_middleware):
        logger.warning("FastMCP add_middleware is unavailable; MCP tool usage metrics disabled")
        return
    add_middleware(RuntimeToolUsageMiddleware())
    setattr(mcp, "_runtime_tool_usage_metrics_installed", True)
