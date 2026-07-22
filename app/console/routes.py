"""
Web console routes: HTML page, static assets, and JSON API endpoints.
Routes are always registered when web_console_enabled=True;
token validation happens inside each handler so paths return 403, not 404.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from config import APP_VERSION, settings
from mcpsrv.neo4j_init import check_neo4j_connection
from console import analysis as analysis_data
from console import agent_chats
from console import agent_jobs
from console import agent_llm
from console.auth import (
    ConsoleAuth,
    ConsoleLastAdminError,
    ConsoleProtectedAdminError,
    ConsoleUserConflict,
    ConsoleUserNotFound,
    create_console_user,
    list_console_users,
    require_console_admin,
    require_console_auth,
    rotate_console_user_token,
    update_console_user,
)

logger = logging.getLogger(__name__)

_TEMPLATE   = Path(__file__).parent / "templates" / "index.html"
_STATIC_DIR = Path(__file__).parent / "static"


def _is_response(value: object) -> bool:
    return isinstance(value, Response)


def _auth_or_403(request: Request) -> ConsoleAuth | JSONResponse:
    return require_console_auth(request)


def _admin_or_403(request: Request) -> ConsoleAuth | JSONResponse:
    return require_console_admin(request)


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _parse_sse_frames(frame: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for raw in str(frame or "").replace("\r\n", "\n").split("\n\n"):
        if not raw.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in raw.split("\n"):
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        payload: dict[str, Any] = {}
        if data_lines:
            try:
                parsed = json.loads("\n".join(data_lines))
                if isinstance(parsed, dict):
                    payload = parsed
                else:
                    payload = {"value": parsed}
            except Exception:
                payload = {"message": "\n".join(data_lines)}
        events.append((event_name, payload))
    return events


async def _console_page_handler(request: Request) -> Response:
    path = request.url.path.rstrip("/")
    if path == f"{settings.web_console_path}/system":
        auth = require_console_admin(request)
    else:
        auth = require_console_auth(request)
    if _is_response(auth):
        return Response("403 Forbidden", status_code=403, media_type="text/plain")
    import json
    assert isinstance(auth, ConsoleAuth)
    config_json = json.dumps({
        "apiPrefix":   settings.web_console_api_prefix,
        "consolePath": settings.web_console_path,
        "appVersion":  APP_VERSION,
        "agentEnabled": settings.console_agent_enabled,
        "agentLlm": agent_llm.get_public_agent_llm_catalog(),
        "auth": auth.public_dict(),
    }, ensure_ascii=False)
    html = (
        _TEMPLATE.read_text(encoding="utf-8")
        .replace("__CONSOLE_CONFIG_JSON__", config_json)
        .replace("__CONSOLE_PATH__",        settings.web_console_path)
    )
    return HTMLResponse(html)


async def _static_css(request: Request) -> Response:
    return Response((_STATIC_DIR / "style.css").read_bytes(), media_type="text/css")


async def _static_js(request: Request) -> Response:
    return Response((_STATIC_DIR / "app.js").read_bytes(), media_type="application/javascript")


async def _static_named_js(request: Request) -> Response:
    name = request.path_params.get("name", "")
    if name not in {
        "bsl.js",
        "code-folding.js",
        "dom-to-image-more.min.js",
        "markdown.js",
        "mermaid.min.js",
        "xml.js",
    }:
        return Response("404 Not Found", status_code=404, media_type="text/plain")
    return Response((_STATIC_DIR / name).read_bytes(), media_type="application/javascript")


async def _static_icon(request: Request) -> Response:
    name = request.path_params.get("name", "")
    if not name or Path(name).name != name:
        return Response("404 Not Found", status_code=404, media_type="text/plain")

    path = _STATIC_DIR / "icons" / name
    if not path.is_file() or path.suffix.lower() != ".png":
        return Response("404 Not Found", status_code=404, media_type="text/plain")

    return Response(path.read_bytes(), media_type="image/png")


async def _console_stats_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    from console.cache import get_stats_cache
    cache = get_stats_cache()
    if cache is None:
        return JSONResponse({"error": "stats_not_ready"}, status_code=503)
    return JSONResponse(cache)


async def _console_stats_refresh_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    from console.cache import refresh_console_stats_cache, StatsRefreshError
    try:
        cache = refresh_console_stats_cache(source="manual", block=True, raise_on_error=True)
    except StatsRefreshError as e:
        return JSONResponse({"error": e.code, "message": e.message}, status_code=503)
    return JSONResponse(cache)


async def _mcp_tools_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    from console.mcp_tools import McpToolsUnavailable, list_local_mcp_tools

    try:
        return JSONResponse(await list_local_mcp_tools())
    except McpToolsUnavailable as exc:
        return JSONResponse(
            {
                "error": "mcp_tools_unavailable",
                "message": str(exc) or "MCP tools metadata is unavailable",
            },
            status_code=503,
        )


async def _mcp_tool_toggle_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    from console.mcp_tools import McpToolNotFound, McpToolToggleFailed, set_local_mcp_tool_enabled

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "invalid_body", "message": "JSON body is required"},
            status_code=400,
        )

    enabled = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(enabled, bool):
        return JSONResponse(
            {"error": "invalid_body", "message": "enabled must be boolean"},
            status_code=400,
        )

    tool_name = str(request.path_params.get("tool_name") or "").strip()
    if not tool_name:
        return JSONResponse({"error": "tool_not_found"}, status_code=404)

    try:
        return JSONResponse(await set_local_mcp_tool_enabled(tool_name, enabled))
    except McpToolNotFound:
        return JSONResponse({"error": "tool_not_found"}, status_code=404)
    except McpToolToggleFailed as exc:
        return JSONResponse(
            {
                "error": "mcp_tool_toggle_failed",
                "message": str(exc) or "Failed to toggle MCP tool",
            },
            status_code=503,
        )


async def _runtime_usage_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    from console.runtime_usage import RuntimeUsageUnavailable, get_runtime_usage

    scope = request.query_params.get("scope", "all")
    try:
        return JSONResponse(get_runtime_usage(scope))
    except ValueError as exc:
        return JSONResponse(
            {"error": "invalid_scope", "message": str(exc)},
            status_code=400,
        )
    except RuntimeUsageUnavailable as exc:
        return JSONResponse(
            {
                "error": "runtime_usage_unavailable",
                "message": str(exc) or "Runtime usage is unavailable",
            },
            status_code=503,
        )


def _analysis_error_response(exc: Exception) -> JSONResponse:
    message = str(exc) or exc.__class__.__name__
    if message.endswith("_required"):
        return JSONResponse({"error": message}, status_code=400)
    if message.endswith("_not_found") or message == "node_not_found":
        return JSONResponse({"error": message}, status_code=404)
    if "Neo4j database connection not available" in message:
        return JSONResponse({"error": "neo4j_not_available"}, status_code=503)
    logger.exception("Console analysis API failed")
    return JSONResponse({"error": "analysis_error", "message": message}, status_code=500)


def _require_param(request: Request, name: str) -> str:
    value = (request.query_params.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name}_required")
    return value


async def _analysis_tree_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_tree())
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_category_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_category(
            _require_param(request, "config"),
            _require_param(request, "category"),
            request.query_params.get("limit"),
            request.query_params.get("offset"),
        ))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_node_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_node(_require_param(request, "ref")))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_module_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_module(
            request.query_params.get("id"),
            request.query_params.get("owner_ref"),
            request.query_params.get("module_type"),
        ))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_module_code_units_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_module_code_units(
            request.query_params.get("id"),
            request.query_params.get("owner_ref"),
            request.query_params.get("module_type"),
        ))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_object_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_object(_require_param(request, "ref")))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_form_tree_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_form_tree(_require_param(request, "ref")))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_search_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_search(
            _require_param(request, "q"),
            request.query_params.get("limit"),
            request.query_params.get("offset"),
            request.query_params.get("config"),
            request.query_params.get("types"),
            request.query_params.get("fields"),
        ))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _analysis_relationships_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_relationships(
            _require_param(request, "ref"),
            request.query_params.get("limit"),
            request.query_params.get("offset"),
        ))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _object_summary_status_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    try:
        return JSONResponse(analysis_data.get_object_summary_status(
            _require_param(request, "ref"),
        ))
    except Exception as exc:
        return _analysis_error_response(exc)


async def _object_summary_run_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    if not getattr(settings, "object_summary_enabled", False):
        return JSONResponse({"error": "feature_disabled"}, status_code=403)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    ref = str(body.get("ref") or "").strip()
    action = str(body.get("action") or "").strip().lower()
    if not ref:
        return JSONResponse({"error": "ref_required"}, status_code=400)
    if action not in {"create", "refresh"}:
        return JSONResponse({"error": "invalid_action"}, status_code=400)

    try:
        status_payload = analysis_data.get_object_summary_status(ref)
    except Exception as exc:
        return _analysis_error_response(exc)

    if not status_payload.get("startup_ready"):
        return JSONResponse({"error": "startup_not_ready"}, status_code=409)
    if not status_payload.get("eligible"):
        return JSONResponse(
            {"error": status_payload.get("disabled_reason") or "object_not_eligible"},
            status_code=409,
        )
    has_summary = bool(status_payload.get("has_summary"))
    if action == "create" and has_summary:
        return JSONResponse({"error": "summary_already_exists"}, status_code=409)
    if action == "refresh" and not has_summary:
        return JSONResponse({"error": "summary_not_found"}, status_code=409)
    if action == "refresh" and not status_payload.get("regeneration_enabled"):
        return JSONResponse({"error": "regeneration_disabled"}, status_code=409)

    from object_summary.manual_jobs import get_manual_job_manager, JobConflict
    try:
        job = get_manual_job_manager().start_job(qualified_name=ref, action=action)
    except JobConflict:
        return JSONResponse({"error": "summary_job_running"}, status_code=409)
    except Exception as exc:
        logger.exception("Failed to start manual summary job")
        return JSONResponse(
            {"error": "job_start_failed", "message": str(exc)}, status_code=500,
        )

    return JSONResponse({"job_id": job.job_id, "status": job.status}, status_code=202)


def _agent_stream_response(generator: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _prepare_agent_turn_start(
    request: Request,
    *,
    chat_id_from_path: str | None = None,
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return None, auth
    if not getattr(settings, "console_agent_enabled", False):
        return None, JSONResponse({"error": "feature_disabled"}, status_code=403)
    agent_jobs.ensure_job_registry_started()

    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "invalid_json"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "invalid_json"}, status_code=400)

    chat_id = str(chat_id_from_path or body.get("chat_id") or body.get("session_id") or "").strip()
    message = str(body.get("message") or "").strip()
    context = body.get("context") or {}
    requested_profile_id = str(body.get("llm_profile_id") or "").strip()
    if not chat_id:
        return None, JSONResponse({"error": "chat_id_required"}, status_code=400)
    if not message:
        return None, JSONResponse({"error": "message_required"}, status_code=400)
    if not isinstance(context, dict):
        return None, JSONResponse({"error": "context_must_be_object"}, status_code=400)
    assert isinstance(auth, ConsoleAuth)
    try:
        chat = agent_chats.get_chat(auth.user_id, chat_id)
    except agent_chats.AgentChatNotFound:
        return None, JSONResponse({"error": "chat_not_found"}, status_code=404)

    from console import agent as console_agent

    try:
        llm_profile = agent_llm.resolve_agent_llm_profile(
            requested_profile_id or str(chat.get("llm_profile_id") or "") or None
        )
    except agent_llm.AgentLlmProfileNotFound as exc:
        return None, JSONResponse(
            {"error": "llm_profile_not_found", "message": str(exc)},
            status_code=400,
        )
    except agent_llm.AgentLlmConfigError as exc:
        return None, JSONResponse(
            {"error": "agent_unavailable", "message": str(exc) or "Console agent is unavailable"},
            status_code=503,
        )

    try:
        console_agent.get_console_agent_mcp_servers()
    except console_agent.ConsoleAgentUnavailable as exc:
        message_text = str(exc) or "Console agent is unavailable"
        if message_text == "feature_disabled":
            return None, JSONResponse({"error": "feature_disabled"}, status_code=403)
        return None, JSONResponse(
            {"error": "agent_unavailable", "message": message_text},
            status_code=503,
        )
    except Exception as exc:
        return None, JSONResponse(
            {"error": "agent_unavailable", "message": str(exc) or "Console agent is unavailable"},
            status_code=503,
        )

    chat = agent_chats.update_chat_llm_profile(auth.user_id, str(chat["id"]), llm_profile.id)
    try:
        turn = agent_jobs.start_agent_turn_job(
            console_agent=console_agent,
            auth=auth,
            chat_id=str(chat["id"]),
            message=message,
            context=context,
            llm_profile=llm_profile.usage_dict(),
        )
    except agent_chats.AgentChatRunning:
        return None, JSONResponse({"error": "turn_already_running"}, status_code=409)
    except agent_chats.AgentChatNotFound:
        return None, JSONResponse({"error": "chat_not_found"}, status_code=404)
    except Exception as exc:
        logger.exception("Failed to start console agent turn job")
        return None, JSONResponse(
            {"error": "agent_unavailable", "message": str(exc) or "Console agent is unavailable"},
            status_code=503,
        )

    return {
        "auth": auth,
        "chat": chat,
        "turn": turn,
    }, None


async def _stream_agent_turn_events(
    *,
    request: Request | None = None,
    auth: ConsoleAuth,
    chat_id: str,
    turn_id: str,
    after_seq: int = 0,
) -> AsyncIterator[str]:
    sent_seqs: set[int] = set()
    subscription = agent_jobs.open_agent_turn_subscription(auth.user_id, chat_id, turn_id)
    try:
        events = agent_chats.get_turn_events(auth.user_id, chat_id, turn_id, after_seq=after_seq)
        for item in events:
            if request is not None and await request.is_disconnected():
                return
            payload = dict(item.get("payload") or {})
            seq = int(payload.get("seq") or item.get("seq") or 0)
            if seq:
                sent_seqs.add(seq)
            yield _sse(str(item.get("event_name") or "message"), payload)

        buffered_events = agent_jobs.get_agent_turn_buffer_events(auth.user_id, chat_id, turn_id, after_seq=after_seq)
        for item in buffered_events:
            if request is not None and await request.is_disconnected():
                return
            payload = dict(item.get("payload") or {})
            seq = int(payload.get("seq") or item.get("seq") or 0)
            if seq and seq in sent_seqs:
                continue
            if seq:
                sent_seqs.add(seq)
            yield _sse(str(item.get("event_name") or "message"), payload)

        turn = agent_chats.get_turn(auth.user_id, chat_id, turn_id)
        if str(turn.get("status") or "") == "running" and subscription:
            async for frame in agent_jobs.iter_agent_turn_subscription(subscription):
                if request is not None and await request.is_disconnected():
                    return
                parsed = _parse_sse_frames(frame)
                if not parsed:
                    if request is not None and await request.is_disconnected():
                        return
                    yield frame
                    continue
                out: list[str] = []
                for event_name, payload in parsed:
                    seq = int(payload.get("seq") or 0)
                    if seq and seq in sent_seqs:
                        continue
                    if seq:
                        sent_seqs.add(seq)
                    out.append(_sse(event_name, payload))
                if out:
                    if request is not None and await request.is_disconnected():
                        return
                    yield "".join(out)
        else:
            if request is not None and await request.is_disconnected():
                return
            yield _sse(
                "turn_status",
                {
                    "chat_id": chat_id,
                    "turn_id": turn_id,
                    "status": str(turn.get("status") or ""),
                    "message": str(turn.get("error_message") or ""),
                },
            )
    except agent_chats.AgentChatNotFound:
        if request is not None and await request.is_disconnected():
            return
        yield _sse(
            "error",
            {"chat_id": chat_id, "turn_id": turn_id, "error": "turn_not_found", "message": "Turn not found"},
        )
    except asyncio.CancelledError:
        return
    finally:
        if subscription:
            subscription[0].subscribers.discard(subscription[1])


async def _agent_turns_handler(request: Request) -> Response:
    chat_id = str(request.path_params.get("chat_id") or "").strip()
    payload, error = await _prepare_agent_turn_start(request, chat_id_from_path=chat_id)
    if error:
        return error
    assert payload is not None
    return JSONResponse({"turn": payload["turn"]}, status_code=202)


async def _agent_turn_stream_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    if not getattr(settings, "console_agent_enabled", False):
        return JSONResponse({"error": "feature_disabled"}, status_code=403)
    agent_jobs.ensure_job_registry_started()
    assert isinstance(auth, ConsoleAuth)
    chat_id = str(request.path_params.get("chat_id") or "").strip()
    turn_id = str(request.path_params.get("turn_id") or "").strip()
    try:
        after_seq = int(request.query_params.get("after_seq") or 0)
    except (TypeError, ValueError):
        after_seq = 0
    try:
        agent_chats.get_turn(auth.user_id, chat_id, turn_id)
    except agent_chats.AgentChatNotFound:
        return JSONResponse({"error": "turn_not_found"}, status_code=404)
    return _agent_stream_response(
        _stream_agent_turn_events(
            request=request,
            auth=auth,
            chat_id=chat_id,
            turn_id=turn_id,
            after_seq=max(0, after_seq),
        )
    )


async def _agent_turn_stop_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    if not getattr(settings, "console_agent_enabled", False):
        return JSONResponse({"error": "feature_disabled"}, status_code=403)
    agent_jobs.ensure_job_registry_started()
    assert isinstance(auth, ConsoleAuth)
    chat_id = str(request.path_params.get("chat_id") or "").strip()
    turn_id = str(request.path_params.get("turn_id") or "").strip()
    try:
        turn = agent_jobs.stop_agent_turn_job(auth.user_id, chat_id, turn_id)
    except agent_chats.AgentChatNotFound:
        return JSONResponse({"error": "turn_not_found"}, status_code=404)
    return JSONResponse({"turn": turn})


async def _agent_chat_stream_handler(request: Request) -> Response:
    payload, error = await _prepare_agent_turn_start(request)
    if error:
        return error
    assert payload is not None
    auth = payload["auth"]
    turn = payload["turn"]
    assert isinstance(auth, ConsoleAuth)
    return _agent_stream_response(
        _stream_agent_turn_events(
            request=request,
            auth=auth,
            chat_id=str(turn["chat_id"]),
            turn_id=str(turn["id"]),
            after_seq=0,
        )
    )


async def _agent_chats_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    if not getattr(settings, "console_agent_enabled", False):
        return JSONResponse({"error": "feature_disabled"}, status_code=403)
    agent_jobs.ensure_job_registry_started()
    assert isinstance(auth, ConsoleAuth)
    if request.method == "GET":
        return JSONResponse(agent_chats.list_chats(auth.user_id))
    body: dict[str, Any] = {}
    try:
        raw_body = await request.body()
        if raw_body.strip():
            parsed = json.loads(raw_body.decode("utf-8"))
            if not isinstance(parsed, dict):
                return JSONResponse({"error": "invalid_body", "message": "JSON object is required"}, status_code=400)
            body = parsed
    except Exception:
        return JSONResponse({"error": "invalid_body", "message": "JSON body is invalid"}, status_code=400)
    requested_profile_id = str(body.get("llm_profile_id") or "").strip()
    try:
        llm_profile = agent_llm.get_agent_llm_profile(requested_profile_id or None)
    except agent_llm.AgentLlmProfileNotFound as exc:
        return JSONResponse({"error": "llm_profile_not_found", "message": str(exc)}, status_code=400)
    except agent_llm.AgentLlmConfigError as exc:
        return JSONResponse({"error": "agent_unavailable", "message": str(exc)}, status_code=503)
    chat = agent_chats.create_chat(auth.user_id, auth.login, llm_profile.id)
    return JSONResponse({"chat": chat}, status_code=201)


async def _agent_chat_detail_handler(request: Request) -> Response:
    auth = _auth_or_403(request)
    if _is_response(auth):
        return auth
    if not getattr(settings, "console_agent_enabled", False):
        return JSONResponse({"error": "feature_disabled"}, status_code=403)
    agent_jobs.ensure_job_registry_started()
    assert isinstance(auth, ConsoleAuth)
    chat_id = str(request.path_params.get("chat_id") or "").strip()
    if not chat_id:
        return JSONResponse({"error": "chat_id_required"}, status_code=400)

    from console import agent as console_agent

    if request.method == "GET":
        try:
            usage = console_agent.get_session_usage_for_user(auth.user_id, chat_id)
            return JSONResponse(agent_chats.get_chat_detail(auth.user_id, chat_id, usage=usage))
        except agent_chats.AgentChatNotFound:
            return JSONResponse({"error": "chat_not_found"}, status_code=404)

    if request.method == "PATCH":
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_body", "message": "JSON body is required"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid_body", "message": "JSON object is required"}, status_code=400)
        requested_profile_id = str(body.get("llm_profile_id") or "").strip()
        try:
            llm_profile = agent_llm.get_agent_llm_profile(requested_profile_id or None)
            chat = agent_chats.update_chat_llm_profile(auth.user_id, chat_id, llm_profile.id)
            return JSONResponse({"chat": chat})
        except agent_llm.AgentLlmProfileNotFound as exc:
            return JSONResponse({"error": "llm_profile_not_found", "message": str(exc)}, status_code=400)
        except agent_llm.AgentLlmConfigError as exc:
            return JSONResponse({"error": "agent_unavailable", "message": str(exc)}, status_code=503)
        except agent_chats.AgentChatNotFound:
            return JSONResponse({"error": "chat_not_found"}, status_code=404)

    try:
        agent_chats.delete_chat(auth.user_id, chat_id)
    except agent_chats.AgentChatNotFound:
        return JSONResponse({"error": "chat_not_found"}, status_code=404)
    except agent_chats.AgentChatRunning:
        return JSONResponse({"error": "chat_has_running_turn"}, status_code=409)
    console_agent.delete_session_usage_for_user(auth.user_id, chat_id)
    await console_agent.try_clear_sdk_session_for_user(auth.user_id, chat_id)
    return JSONResponse({"deleted": True, "chat_id": chat_id})


def _make_health_handler(transport: str):
    async def handler(request: Request) -> Response:
        auth = _admin_or_403(request)
        if _is_response(auth):
            return auth
        import config as cfg
        from mcpsrv import runtime_state as _runtime_state
        return JSONResponse({
            "project_name":   settings.project_name,
            "config_name":    cfg.onec_config_name,
            "neo4j_connected": check_neo4j_connection(),
            "mcp_transport":  transport,
            "mcp_path":       settings.mcp_path,
            "console_path":   settings.web_console_path,
            "startup":        _runtime_state.get_state(),
        })
    return handler


async def _health_index_handler(request: Request) -> Response:
    """Unauthenticated readiness probe for the graph bootstrap index.

    Intended for Prometheus blackbox (http_2xx, anonymous GET): distinguishes
    "container alive" (tcp probe) from "config graph index ready". Returns
    only a boolean status, no configuration/system data, so it is safe to
    leave without console auth.
    """
    from mcpsrv import runtime_state as _runtime_state
    ready = _runtime_state.is_ready()
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)


async def _console_users_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    if request.method == "GET":
        return JSONResponse(list_console_users())
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_body", "message": "JSON body is required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body", "message": "JSON object is required"}, status_code=400)
    try:
        return JSONResponse(create_console_user(body), status_code=201)
    except ValueError as exc:
        return JSONResponse({"error": "invalid_body", "message": str(exc)}, status_code=400)
    except ConsoleUserConflict as exc:
        return JSONResponse({"error": "user_conflict", "message": str(exc)}, status_code=409)


async def _console_user_update_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_body", "message": "JSON body is required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body", "message": "JSON object is required"}, status_code=400)
    user_id = str(request.path_params.get("user_id") or "").strip()
    try:
        return JSONResponse(update_console_user(user_id, body))
    except ValueError as exc:
        return JSONResponse({"error": "invalid_body", "message": str(exc)}, status_code=400)
    except ConsoleUserNotFound:
        return JSONResponse({"error": "user_not_found"}, status_code=404)
    except ConsoleLastAdminError as exc:
        return JSONResponse({"error": "last_admin", "message": str(exc)}, status_code=409)


async def _console_user_rotate_token_handler(request: Request) -> Response:
    auth = _admin_or_403(request)
    if _is_response(auth):
        return auth
    user_id = str(request.path_params.get("user_id") or "").strip()
    try:
        return JSONResponse(rotate_console_user_token(user_id))
    except ConsoleUserNotFound:
        return JSONResponse({"error": "user_not_found"}, status_code=404)
    except ConsoleProtectedAdminError as exc:
        return JSONResponse({"error": "protected_admin", "message": str(exc)}, status_code=409)


def build_console_routes(transport: str) -> list:
    prefix = settings.web_console_api_prefix
    cpath  = settings.web_console_path
    return [
        Route(cpath,                       _console_page_handler,          methods=["GET"]),
        Route(f"{cpath}/system",           _console_page_handler,          methods=["GET"]),
        Route(f"{cpath}/analysis",         _console_page_handler,          methods=["GET"]),
        Route(f"{cpath}/static/style.css", _static_css,                    methods=["GET"]),
        Route(f"{cpath}/static/app.js",    _static_js,                     methods=["GET"]),
        Route(f"{cpath}/static/{{name}}",  _static_named_js,               methods=["GET"]),
        Route(f"{cpath}/static/icons/{{name}}", _static_icon,              methods=["GET"]),
        Route(f"{prefix}/stats",           _console_stats_handler,         methods=["GET"]),
        Route(f"{prefix}/stats/refresh",   _console_stats_refresh_handler, methods=["POST"]),
        Route(f"{prefix}/health",          _make_health_handler(transport), methods=["GET"]),
        Route(f"{prefix}/health/index",    _health_index_handler,          methods=["GET"]),
        Route(f"{prefix}/runtime/usage",   _runtime_usage_handler,         methods=["GET"]),
        Route(f"{prefix}/mcp/tools",       _mcp_tools_handler,             methods=["GET"]),
        Route(f"{prefix}/mcp/tools/{{tool_name}}", _mcp_tool_toggle_handler, methods=["PATCH"]),
        Route(f"{prefix}/users",           _console_users_handler,         methods=["GET", "POST"]),
        Route(f"{prefix}/users/{{user_id}}", _console_user_update_handler, methods=["PATCH"]),
        Route(f"{prefix}/users/{{user_id}}/rotate-token", _console_user_rotate_token_handler, methods=["POST"]),
        Route(f"{prefix}/analysis/tree",          _analysis_tree_handler,          methods=["GET"]),
        Route(f"{prefix}/analysis/category",      _analysis_category_handler,      methods=["GET"]),
        Route(f"{prefix}/analysis/node",          _analysis_node_handler,          methods=["GET"]),
        Route(f"{prefix}/analysis/module",        _analysis_module_handler,        methods=["GET"]),
        Route(f"{prefix}/analysis/module/code-units", _analysis_module_code_units_handler, methods=["GET"]),
        Route(f"{prefix}/analysis/object",        _analysis_object_handler,        methods=["GET"]),
        Route(f"{prefix}/analysis/form-tree",     _analysis_form_tree_handler,     methods=["GET"]),
        Route(f"{prefix}/analysis/search",        _analysis_search_handler,        methods=["GET"]),
        Route(f"{prefix}/analysis/relationships", _analysis_relationships_handler, methods=["GET"]),
        Route(f"{prefix}/analysis/object-summary/status", _object_summary_status_handler, methods=["GET"]),
        Route(f"{prefix}/analysis/object-summary/run",    _object_summary_run_handler,    methods=["POST"]),
        Route(f"{prefix}/agent/chats",                    _agent_chats_handler,           methods=["GET", "POST"]),
        Route(f"{prefix}/agent/chats/{{chat_id}}",        _agent_chat_detail_handler,      methods=["GET", "PATCH", "DELETE"]),
        Route(f"{prefix}/agent/chats/{{chat_id}}/turns",  _agent_turns_handler,            methods=["POST"]),
        Route(f"{prefix}/agent/chats/{{chat_id}}/turns/{{turn_id}}/stream", _agent_turn_stream_handler, methods=["GET"]),
        Route(f"{prefix}/agent/chats/{{chat_id}}/turns/{{turn_id}}/stop",   _agent_turn_stop_handler,   methods=["POST"]),
        Route(f"{prefix}/agent/chat/stream",              _agent_chat_stream_handler,     methods=["POST"]),
    ]
