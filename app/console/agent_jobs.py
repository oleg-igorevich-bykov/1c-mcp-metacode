"""Process-local background jobs for console-agent chat turns."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from config import settings
from console import agent_chats
from console.auth import ConsoleAuth

logger = logging.getLogger(__name__)

_JobKey = tuple[str, str, str]
_jobs: dict[_JobKey, "AgentRunJob"] = {}
_cleanup_paths: set[str] = set()
_SNAPSHOT_MIN_INTERVAL_SEC = 0.75
_SNAPSHOT_EVENT_INTERVAL = 20
_PERSIST_BATCH_MAX = 50
_PERSIST_FLUSH_INTERVAL_SEC = 0.12


def sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def parse_sse_frames(frame: str) -> list[tuple[str, dict[str, Any]]]:
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
                payload = parsed if isinstance(parsed, dict) else {"value": parsed}
            except Exception:
                payload = {"message": "\n".join(data_lines)}
        events.append((event_name, payload))
    return events


class AgentTurnCapture:
    def __init__(self) -> None:
        self.answer_parts: list[str] = []
        self.final_answer = ""
        self.reasoning_parts: list[str] = []
        self.plan_events: list[dict[str, Any]] = []
        self.tool_events: list[dict[str, Any]] = []
        self.notices: list[dict[str, Any]] = []
        self.usage: dict[str, Any] = {}
        self.error_message = ""
        self.seen_done = False

    def apply(self, event_name: str, payload: dict[str, Any]) -> None:
        if event_name == "delta":
            self.answer_parts.append(str(payload.get("text") or ""))
            return
        if event_name == "reasoning_delta":
            self.reasoning_parts.append(str(payload.get("text") or ""))
            return
        if event_name in {"plan", "plan_step", "plan_done"}:
            self.plan_events.append({"event": event_name, "payload": payload})
            return
        if event_name in {"tool_call", "tool_result"}:
            self.tool_events.append({"event": event_name, "payload": payload})
            return
        if event_name == "usage":
            self.usage = payload
            return
        if event_name == "warning":
            self.notices.append({"kind": "warning", "message": str(payload.get("message") or "")})
            return
        if event_name == "error":
            self.error_message = str(payload.get("message") or payload.get("error") or "Ошибка агента")
            self.notices.append({"kind": "error", "message": self.error_message})
            return
        if event_name == "done":
            self.seen_done = True
            self.final_answer = str(payload.get("answer") or "").strip()

    @property
    def assistant_text(self) -> str:
        return self.final_answer or "".join(self.answer_parts)

    @property
    def reasoning_text(self) -> str:
        return "".join(self.reasoning_parts)

    @property
    def status(self) -> str:
        if self.error_message:
            return "error"
        if self.seen_done:
            return "done"
        return "stopped"


@dataclass
class AgentRunJob:
    user_id: str
    login: str
    chat_id: str
    turn_id: str
    message: str
    context: dict[str, Any]
    llm_profile_id: str
    llm_profile: dict[str, Any]
    task: asyncio.Task[None] | None = None
    persist_task: asyncio.Task[None] | None = None
    persist_queue: asyncio.Queue[dict[str, Any] | None] = field(default_factory=asyncio.Queue)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    subscribers: set[asyncio.Queue[str | None]] = field(default_factory=set)
    last_seq: int = 0
    last_snapshot_seq: int = 0
    last_snapshot_at: float = 0.0
    event_buffer: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> _JobKey:
        return (self.user_id, self.chat_id, self.turn_id)


def ensure_job_registry_started() -> None:
    path = str(settings.console_agent_chats_sqlite_path)
    if path in _cleanup_paths:
        return
    agent_chats.mark_stale_running_turns()
    _cleanup_paths.add(path)


def _publish_to_subscribers(job: AgentRunJob, frame: str) -> None:
    for queue in list(job.subscribers):
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            logger.warning("Console agent subscriber queue is full; dropping event")


def _close_subscribers(job: AgentRunJob) -> None:
    for queue in list(job.subscribers):
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
    job.subscribers.clear()


async def _persist_snapshot(job: AgentRunJob, capture: AgentTurnCapture) -> None:
    await asyncio.to_thread(
        agent_chats.update_running_turn_snapshot,
        job.user_id,
        job.chat_id,
        job.turn_id,
        assistant_text=capture.assistant_text,
        reasoning_text=capture.reasoning_text,
        plan={"events": capture.plan_events} if capture.plan_events else {},
        tool_events=capture.tool_events,
        notices=capture.notices,
        usage=capture.usage,
        error_message=capture.error_message,
    )


async def _maybe_persist_snapshot(job: AgentRunJob, capture: AgentTurnCapture, *, force: bool = False) -> None:
    now = time.monotonic()
    if not force:
        if job.last_seq <= job.last_snapshot_seq:
            return
        if (job.last_seq - job.last_snapshot_seq) < _SNAPSHOT_EVENT_INTERVAL and (
            now - job.last_snapshot_at
        ) < _SNAPSHOT_MIN_INTERVAL_SEC:
            return
    await _persist_snapshot(job, capture)
    job.last_snapshot_seq = job.last_seq
    job.last_snapshot_at = now


async def _persist_event_worker(job: AgentRunJob) -> None:
    pending: list[dict[str, Any]] = []
    should_stop = False
    try:
        while not should_stop:
            item = await job.persist_queue.get()
            if item is None:
                job.persist_queue.task_done()
                break
            pending.append(item)

            deadline = time.monotonic() + _PERSIST_FLUSH_INTERVAL_SEC
            while len(pending) < _PERSIST_BATCH_MAX:
                timeout = max(0.0, deadline - time.monotonic())
                if timeout <= 0:
                    break
                try:
                    item = await asyncio.wait_for(job.persist_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                if item is None:
                    job.persist_queue.task_done()
                    should_stop = True
                    break
                pending.append(item)

            if pending:
                batch = pending
                pending = []
                try:
                    await asyncio.to_thread(
                        agent_chats.append_turn_events_batch,
                        job.user_id,
                        job.chat_id,
                        job.turn_id,
                        batch,
                    )
                except Exception:
                    logger.exception("Failed to persist console agent turn event batch")
                finally:
                    for _ in batch:
                        job.persist_queue.task_done()
    except asyncio.CancelledError:
        raise
    finally:
        if pending:
            try:
                await asyncio.to_thread(
                    agent_chats.append_turn_events_batch,
                    job.user_id,
                    job.chat_id,
                    job.turn_id,
                    pending,
                )
            except Exception:
                logger.exception("Failed to persist final console agent turn event batch")
            finally:
                for _ in pending:
                    job.persist_queue.task_done()


async def _stop_persist_worker(job: AgentRunJob) -> None:
    if not job.persist_task:
        return
    if job.persist_task.done():
        try:
            await job.persist_task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Console agent persist worker failed")
        return
    await job.persist_queue.put(None)
    await job.persist_queue.join()
    try:
        await job.persist_task
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Console agent persist worker failed")


async def _flush_persist_queue(job: AgentRunJob) -> None:
    if job.persist_task:
        await job.persist_queue.join()


def _buffered_events_after(job: AgentRunJob, after_seq: int = 0) -> list[dict[str, Any]]:
    try:
        seq = max(0, int(after_seq or 0))
    except (TypeError, ValueError):
        seq = 0
    return [
        {"event_name": str(item.get("event_name") or "message"), "payload": dict(item.get("payload") or {})}
        for item in list(job.event_buffer)
        if int((item.get("payload") or {}).get("seq") or 0) > seq
    ]


def get_agent_turn_buffer_events(user_id: str, chat_id: str, turn_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    job = get_agent_turn_job(user_id, chat_id, turn_id)
    if not job:
        return []
    return _buffered_events_after(job, after_seq)


def _append_and_publish(job: AgentRunJob, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    job.last_seq += 1
    event_payload = {
        **dict(payload or {}),
        "chat_id": job.chat_id,
        "turn_id": job.turn_id,
        "seq": job.last_seq,
    }
    event = {"event_name": str(event_name or "message"), "payload": event_payload}
    job.event_buffer.append(event)
    _publish_to_subscribers(job, sse(event["event_name"], event_payload))
    job.persist_queue.put_nowait(event)
    return event_payload


async def _run_agent_turn_job(job: AgentRunJob, console_agent: Any) -> None:
    capture = AgentTurnCapture()
    status = "stopped"
    error_message = ""
    try:
        async for frame in console_agent.stream_console_agent(
            session_id=job.chat_id,
            message=job.message,
            context=job.context,
            user_id=job.user_id,
            login=job.login,
            llm_profile_id=job.llm_profile_id,
        ):
            for event_name, payload in parse_sse_frames(frame):
                if event_name == "start":
                    payload = {**payload, "chat_id": job.chat_id, "session_id": job.chat_id}
                event_payload = _append_and_publish(job, event_name, payload)
                capture.apply(event_name, event_payload)
                await _maybe_persist_snapshot(job, capture)
        status = capture.status
        error_message = capture.error_message if status == "error" else ""
    except asyncio.CancelledError:
        status = "stopped"
        error_message = "Остановлено пользователем"
        capture.notices.append({"kind": "warning", "message": error_message})
    except Exception as exc:
        logger.exception("Console agent background job failed")
        status = "error"
        error_message = str(exc) or "Ошибка агента"
        capture.error_message = error_message
        capture.notices.append({"kind": "error", "message": error_message})
        try:
            _append_and_publish(job, "error", {"error": "agent_error", "message": error_message})
        except Exception:
            logger.exception("Failed to persist console agent job error event")
    finally:
        try:
            await _maybe_persist_snapshot(job, capture, force=True)
            await _flush_persist_queue(job)
        except Exception:
            logger.exception("Failed to persist final console agent running snapshot")
        try:
            await asyncio.to_thread(
                agent_chats.finish_turn,
                job.user_id,
                job.chat_id,
                job.turn_id,
                status=status,
                assistant_text=capture.assistant_text,
                reasoning_text=capture.reasoning_text,
                plan={"events": capture.plan_events} if capture.plan_events else {},
                tool_events=capture.tool_events,
                notices=capture.notices,
                usage=capture.usage,
                error_message=error_message if status in {"error", "stopped"} else "",
            )
            _append_and_publish(
                job,
                "turn_status",
                {"status": status, "message": error_message},
            )
            await _stop_persist_worker(job)
        except Exception:
            logger.exception("Failed to finalize console agent background job")
        _jobs.pop(job.key, None)
        _close_subscribers(job)


def start_agent_turn_job(
    *,
    console_agent: Any,
    auth: ConsoleAuth,
    chat_id: str,
    message: str,
    context: dict[str, Any],
    llm_profile: dict[str, Any],
) -> dict[str, Any]:
    ensure_job_registry_started()
    profile_id = str(llm_profile.get("profile_id") or "")
    turn = agent_chats.start_turn(
        auth.user_id,
        chat_id,
        message,
        login=auth.login,
        llm_profile=llm_profile,
    )
    job = AgentRunJob(
        user_id=auth.user_id,
        login=auth.login,
        chat_id=str(turn["chat_id"]),
        turn_id=str(turn["id"]),
        message=message,
        context=context,
        llm_profile_id=profile_id,
        llm_profile=llm_profile,
    )
    _jobs[job.key] = job
    job.persist_task = asyncio.create_task(_persist_event_worker(job))
    job.task = asyncio.create_task(_run_agent_turn_job(job, console_agent))
    return turn


def get_agent_turn_job(user_id: str, chat_id: str, turn_id: str) -> AgentRunJob | None:
    return _jobs.get((user_id, chat_id, turn_id))


def open_agent_turn_subscription(user_id: str, chat_id: str, turn_id: str) -> tuple[AgentRunJob, asyncio.Queue[str | None]] | None:
    job = get_agent_turn_job(user_id, chat_id, turn_id)
    if not job:
        return None
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=500)
    job.subscribers.add(queue)
    return job, queue


async def iter_agent_turn_subscription(
    subscription: tuple[AgentRunJob, asyncio.Queue[str | None]],
) -> AsyncIterator[str]:
    job, queue = subscription
    try:
        while True:
            frame = await queue.get()
            if frame is None:
                break
            yield frame
    finally:
        job.subscribers.discard(queue)


async def subscribe_agent_turn(user_id: str, chat_id: str, turn_id: str) -> AsyncIterator[str]:
    subscription = open_agent_turn_subscription(user_id, chat_id, turn_id)
    if not subscription:
        return
    async for frame in iter_agent_turn_subscription(subscription):
        yield frame


def stop_agent_turn_job(user_id: str, chat_id: str, turn_id: str) -> dict[str, Any]:
    ensure_job_registry_started()
    turn = agent_chats.request_stop_turn(user_id, chat_id, turn_id)
    if turn.get("status") != "running":
        return turn
    job = get_agent_turn_job(user_id, chat_id, turn_id)
    if job and job.task:
        job.stop_requested.set()
        job.task.cancel()
        return {**agent_chats.get_turn(user_id, chat_id, turn_id), "status": "stopped"}
    return agent_chats.finish_turn(
        user_id,
        chat_id,
        turn_id,
        status="stopped",
        assistant_text=str(turn.get("assistant_text") or ""),
        reasoning_text=str(turn.get("reasoning_text") or ""),
        plan=turn.get("plan") or {},
        tool_events=turn.get("tool_events") or [],
        notices=[*(turn.get("notices") or []), {"kind": "warning", "message": "Остановлено пользователем"}],
        usage=turn.get("usage") or {},
        error_message="Остановлено пользователем",
    )


def list_running_jobs_for_user(user_id: str) -> list[dict[str, Any]]:
    return [
        {"chat_id": job.chat_id, "turn_id": job.turn_id}
        for job in _jobs.values()
        if job.user_id == user_id
    ]


def reset_for_tests() -> None:
    for job in list(_jobs.values()):
        if job.task and not job.task.done():
            job.task.cancel()
        if job.persist_task and not job.persist_task.done():
            job.persist_task.cancel()
    _jobs.clear()
    _cleanup_paths.clear()
