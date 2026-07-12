"""In-process lifecycle for manual object_summary jobs (web console button).

Owns only the lifecycle layer: one global lock guarantees a single active
job per process, ManualJob holds status/timestamps/error, a daemon thread
runs the actual domain work. The domain order (archive → build attempts →
write → atomic publish → embedding, with restore-from-archive safety net)
lives in `graphdb.object_summary_pipeline.run_single_object_summary_job`.

The manager does NOT know about S1/S2, Neo4j, file IO or runtime_metrics.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from config import settings

logger = logging.getLogger(__name__)


JobStatus = Literal["pending", "running", "succeeded", "failed"]
ManualAction = Literal["create", "refresh"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


@dataclass
class ManualJob:
    job_id: str
    qualified_name: str
    action: ManualAction
    status: JobStatus
    started_at: str
    finished_at: Optional[str] = None
    error: Optional[str] = None
    _started_ts: float = field(default_factory=_now_ts)

    def elapsed_seconds(self) -> int:
        end_ts = self._started_ts if self.status in ("pending", "running") else None
        if self.status in ("succeeded", "failed") and self.finished_at:
            try:
                end = datetime.strptime(self.finished_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                return max(0, int(end.timestamp() - self._started_ts))
            except ValueError:
                return 0
        return max(0, int(_now_ts() - self._started_ts))

    def snapshot(self) -> dict:
        return {
            "job_id": self.job_id,
            "qualified_name": self.qualified_name,
            "action": self.action,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": self.elapsed_seconds(),
            "error": self.error or "",
        }


class JobConflict(Exception):
    """Raised when another manual job is already running on this process."""


class ManualJobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: Optional[ManualJob] = None

    def get_active(self) -> Optional[ManualJob]:
        with self._lock:
            return self._active

    def start_job(self, *, qualified_name: str, action: ManualAction) -> ManualJob:
        with self._lock:
            if self._active is not None and self._active.status == "running":
                raise JobConflict("summary_job_running")
            job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + (
                f"{int(_now_ts() * 1000) % 100000:05d}"
            )
            job = ManualJob(
                job_id=job_id,
                qualified_name=qualified_name,
                action=action,
                status="running",
                started_at=_now_iso(),
            )
            self._active = job

        thread = threading.Thread(
            target=self._run, args=(job,),
            name=f"object_summary_manual_{job.job_id}", daemon=True,
        )
        thread.start()
        return job

    def _run(self, job: ManualJob) -> None:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._execute(job))
        except Exception as exc:
            logger.exception("Manual job %s crashed", job.job_id)
            with self._lock:
                job.status = "failed"
                job.finished_at = _now_iso()
                job.error = str(exc) or exc.__class__.__name__
        finally:
            try:
                loop.close()
            except Exception:
                pass

    async def _execute(self, job: ManualJob) -> None:
        # Delayed imports to avoid circular dependencies at process startup.
        from mcpsrv.neo4j_init import get_loader
        from mcpsrv import runtime_state
        from graphdb.object_summary_pipeline import run_single_object_summary_job

        if not runtime_state.is_ready():
            self._finish(job, ok=False, error="startup_not_ready")
            return

        loader = get_loader()
        if loader is None:
            self._finish(job, ok=False, error="neo4j_unavailable")
            return

        try:
            result = await asyncio.to_thread(
                run_single_object_summary_job,
                loader.driver,
                project_name=settings.project_name,
                qualified_name=job.qualified_name,
                action=job.action,
            )
        except Exception as exc:
            logger.exception("run_single_object_summary_job raised for %s", job.job_id)
            self._finish(job, ok=False, error=str(exc) or exc.__class__.__name__)
            return

        if result.ok:
            self._finish(job, ok=True)
        else:
            self._finish(job, ok=False, error=result.error or "job_failed")

    def _finish(self, job: ManualJob, *, ok: bool, error: Optional[str] = None) -> None:
        with self._lock:
            job.finished_at = _now_iso()
            job.status = "succeeded" if ok else "failed"
            job.error = error
            # Keep `_active` pointing at the last finished job so polling
            # status can render the terminal state on the very next request.
            # `start_job` only blocks on a job whose status is still
            # "running", so a finished job does not prevent the next one.


_manager: Optional[ManualJobManager] = None
_manager_lock = threading.Lock()


def get_manual_job_manager() -> ManualJobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ManualJobManager()
        return _manager
