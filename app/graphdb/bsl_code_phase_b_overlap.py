"""Startup-only overlapped Phase B controller for BSL code search.

Phase A streams committed unit rows into this controller (via the best-effort
`submit` sink) while it is still building the SQLite/RLM index. A single
consumer thread runs an asyncio loop that embeds those already-committed units
early — a *fast path* only. SQLite remains the source of truth: after Phase A
finishes, the caller runs the normal Phase B finalize (`_run_phase_b_and_finalize`
under `run_mode="startup"`) which catches up every still-not-done unit, ensures
the vector index is ONLINE, syncs visibility and commits `ready`.

Design invariants:
  * Phase A never blocks on or fails because of this controller. `submit` drops
    on a full queue; consumer errors are swallowed. Dropped/failed units are
    recovered by the SQLite catch-up.
  * Direct writes use `visible_on_upsert=False` and the pending epoch; they are
    never search-visible before `ready` (the search gate requires
    vector_status=ready anyway).
  * Done-recheck before each provider sub-batch drops units already marked done
    (e.g. by transfer / a prior catch-up) so the endpoint is not called twice.
  * On an embedding outage the direct path opens a circuit for one startup
    backoff window instead of hammering the endpoint per batch; the SQLite
    catch-up recovers later with the full 12×300s policy.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import random
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from config import settings

from .bsl_code_phase_b_metrics import (
    BSL_PHASE_B_PROGRESS_UNITS,
    BSL_PROGRESS_SECONDS,
    _format_cost,
    _format_usage_tokens,
    _PhaseBStats,
    _ProgressLogger,
)
from .embedding_service import is_embedding_unavailable_error

logger = logging.getLogger(__name__)


class BslPhaseBOverlapController:
    """Producer/consumer bridge between streaming Phase A and an early Phase B.

    Lifecycle (owned by the startup branch of `start_indexing`, R1-F1):
        controller = BslPhaseBOverlapController(indexer, embedding_service, doc_spec)
        controller.start()
        indexer._run_phase_a(..., committed_units_sink=controller.submit)
        controller.producer_done()
        controller.join()
        # then the caller runs _run_phase_b_and_finalize (SQLite catch-up).
    """

    def __init__(
        self,
        indexer: Any,
        *,
        embedding_service: Any,
        doc_spec: Any,
    ) -> None:
        self.indexer = indexer
        self.sqlite = indexer.sqlite
        self.scope = indexer.scope
        self.embedding_service = embedding_service
        self.doc_spec = doc_spec

        maxsize = max(
            1, int(settings.bsl_code_startup_phase_b_overlap_queue_batches)
        )
        self._queue: "queue.Queue[List[Dict[str, Any]]]" = queue.Queue(
            maxsize=maxsize
        )
        self._chunk_units = max(
            1, int(settings.bsl_code_startup_phase_b_overlap_chunk_units)
        )
        self._provider_batch = max(
            1, int(settings.bsl_code_embedding_batch_size)
        )
        self._pending_epoch: Optional[int] = None
        self._done_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._circuit_open_until = 0.0

        # Metrics. `submit` mutates the offered/submitted/queue_dropped counters
        # from the Phase A thread; the consumer thread mutates the rest. The lock
        # guards short counter updates/snapshots so the progress heartbeat sees a
        # consistent view — never held across logging or an await.
        self._counters_lock = threading.Lock()
        self.offered = 0            # all units Phase A tried to hand over
        self.submitted = 0          # accepted into the queue
        self.queue_dropped = 0      # dropped at submit (queue full)
        self.direct_dropped = 0     # accepted then dropped (circuit/outage)
        self.dropped = 0            # compat total = queue_dropped + direct_dropped
        self.rechecked_skipped = 0  # already-done before provider call
        self.direct_done = 0        # units_written by the direct path
        self.failed_sub_batches = 0
        self.failed_units = 0       # non-outage failures, left for catch-up
        self.circuit_windows = 0
        # Real Phase B usage/cost, accumulated only from actual embedding calls.
        self.phase_b_stats = _PhaseBStats()

    # ------------------------------------------------------------------ producer

    def submit(self, pending_epoch: int, unit_rows: Sequence[Dict[str, Any]]) -> None:
        """Best-effort Phase A sink. Never blocks Phase A: a full queue drops
        the batch (SQLite catch-up recovers it). Learns the pending epoch from
        the first call."""
        if not unit_rows:
            return
        if self._pending_epoch is None:
            self._pending_epoch = int(pending_epoch)
        rows = list(unit_rows)
        n = len(rows)
        try:
            self._queue.put_nowait(rows)
        except queue.Full:
            with self._counters_lock:
                self.offered += n
                self.queue_dropped += n
                self.dropped += n
            return
        with self._counters_lock:
            self.offered += n
            self.submitted += n

    def producer_done(self) -> None:
        """Signal Phase A finished producing. The consumer drains whatever is
        already queued, then stops."""
        self._done_event.set()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="bsl_phase_b_overlap", daemon=True,
        )
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:
        try:
            asyncio.run(self._consume())
        except Exception as e:  # noqa: BLE001 — overlap must never crash startup
            logger.error(
                "BSL overlap controller crashed (ignored, catch-up recovers): %s",
                e, exc_info=True,
            )

    # ------------------------------------------------------------------ consumer

    def _drain_chunk(self) -> Optional[List[Dict[str, Any]]]:
        """Blocking drain of one chunk. Returns None to stop (producer done and
        queue drained), [] on an idle poll, else a list of unit rows up to
        `chunk_units`."""
        try:
            first = self._queue.get(timeout=0.5)
        except queue.Empty:
            if self._done_event.is_set():
                return None
            return []
        rows = list(first)
        while len(rows) < self._chunk_units:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            rows.extend(item)
        return rows

    async def _consume(self) -> None:
        loop = asyncio.get_running_loop()
        # Reuse the ordinary Phase B progress trigger (every N units / 30s /
        # final). item_name/label differ; logger namespace stays this module's so
        # overlap progress groups with the existing overlap summary line.
        progress = _ProgressLogger(
            "BSL Phase B overlap",
            0,
            BSL_PHASE_B_PROGRESS_UNITS,
            item_name="submitted units",
            log=logger,
        )

        async def _heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(BSL_PROGRESS_SECONDS)
                self._log_progress(progress)

        heartbeat = asyncio.create_task(
            _heartbeat_loop(), name="bsl_overlap_progress_heartbeat",
        )
        try:
            while True:
                chunk = await loop.run_in_executor(None, self._drain_chunk)
                if chunk is None:
                    break
                if not chunk:
                    continue
                epoch = self._pending_epoch
                if epoch is None:
                    continue
                await self._process_chunk(int(epoch), chunk)
                self._log_progress(progress)  # item-due after each chunk
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            # Detailed final progress/usage line first, then the short summary
            # kept for existing grep/debug habits.
            self._log_progress(progress, final=True)
        logger.info(
            "BSL overlap: submitted=%d dropped=%d rechecked_skipped=%d "
            "direct_done=%d circuit_windows=%d",
            self.submitted, self.dropped, self.rechecked_skipped,
            self.direct_done, self.circuit_windows,
        )

    def _snapshot(self) -> Dict[str, Any]:
        """Consistent counter view for the progress logger. `phase_b_stats` is
        copied so formatting outside the lock never races the live accumulator."""
        with self._counters_lock:
            return {
                "offered": self.offered,
                "submitted": self.submitted,
                "queue_dropped": self.queue_dropped,
                "direct_dropped": self.direct_dropped,
                "dropped": self.dropped,
                "rechecked_skipped": self.rechecked_skipped,
                "direct_done": self.direct_done,
                "failed_sub_batches": self.failed_sub_batches,
                "failed_units": self.failed_units,
                "circuit_windows": self.circuit_windows,
                "stats": self.phase_b_stats.copy(),
            }

    def _log_progress(
        self, progress: _ProgressLogger, *, final: bool = False,
    ) -> None:
        snap = self._snapshot()
        stats = snap["stats"]
        # numerator = consumed submitted units (embedded + recheck-skipped +
        # circuit/outage dropped + non-outage failed), so it stays in the same
        # coordinate system as denominator=submitted — an all-rechecked stream
        # reads N/N, not 0/N. total is clamped so pct never exceeds 100% on the
        # outage+recheck double-count edge.
        consumed = (
            stats.units_requested
            + snap["rechecked_skipped"]
            + snap["direct_dropped"]
            + snap["failed_units"]
        )
        progress.total = max(snap["submitted"], consumed)
        progress.maybe_log(
            consumed,
            final=final,
            offered=snap["offered"],
            submitted=snap["submitted"],
            queue_dropped=snap["queue_dropped"],
            direct_dropped=snap["direct_dropped"],
            dropped=snap["dropped"],
            rechecked_skipped=snap["rechecked_skipped"],
            units_prepared=stats.units_prepared,
            units_written=stats.units_written,
            skipped_missing_body=stats.skipped_missing_body,
            skipped_hash_mismatch=stats.skipped_hash_mismatch,
            skipped_empty_text=stats.skipped_empty_text,
            batches=stats.batches,
            embedding_api_calls=stats.embedding_api_calls,
            input_tokens=_format_usage_tokens(stats.input_tokens),
            total_tokens=_format_usage_tokens(stats.total_tokens),
            cost=_format_cost(*stats.primary_cost()),
            failed_sub_batches=snap["failed_sub_batches"],
            failed_units=snap["failed_units"],
            queue_size=self._queue.qsize(),
            circuit_windows=snap["circuit_windows"],
        )

    async def _process_chunk(
        self, epoch: int, rows: List[Dict[str, Any]],
    ) -> None:
        if time.monotonic() < self._circuit_open_until:
            # Circuit open: drop without calling the endpoint; catch-up recovers.
            with self._counters_lock:
                self.direct_dropped += len(rows)
                self.dropped += len(rows)
            return
        for i in range(0, len(rows), self._provider_batch):
            sub = rows[i: i + self._provider_batch]
            requested = len(sub)
            # Done-recheck immediately before the provider call (at-least-once
            # transport): drop units already marked done meanwhile.
            sub = self.sqlite.filter_phase_b_still_not_done(
                self.scope, epoch, sub,
            )
            skipped = requested - len(sub)
            if skipped:
                with self._counters_lock:
                    self.rechecked_skipped += skipped
            if not sub:
                continue
            try:
                stats = await self.indexer._phase_b_process_batch(
                    batch=sub,
                    current_epoch=epoch,
                    embedding_service=self.embedding_service,
                    doc_spec=self.doc_spec,
                    visible_on_upsert=False,
                )
            except Exception as e:  # noqa: BLE001
                if is_embedding_unavailable_error(e):
                    self._open_circuit()
                    # Drop only the filtered current sub-batch plus the still
                    # not-rechecked tail of this chunk. The current sub-batch's
                    # already-done units were counted in rechecked_skipped, so
                    # using len(sub) (not the original requested) here avoids
                    # double-counting them into direct_dropped/dropped/progress.
                    remaining = len(sub) + max(0, len(rows) - (i + requested))
                    with self._counters_lock:
                        self.direct_dropped += remaining
                        self.dropped += remaining
                    return
                # Neo4j / transient error: leave units not-done for catch-up.
                with self._counters_lock:
                    self.failed_sub_batches += 1
                    self.failed_units += len(sub)
                logger.debug(
                    "BSL overlap: sub-batch failed (left for catch-up): %s", e,
                )
                continue
            with self._counters_lock:
                self.phase_b_stats.add(stats)
                self.direct_done += int(getattr(stats, "units_written", 0) or 0)

    def _open_circuit(self) -> None:
        base = float(settings.bsl_code_startup_phase_b_round_backoff_seconds)
        jitter = random.uniform(
            0.0,
            max(0.0, float(
                settings.bsl_code_startup_phase_b_round_backoff_jitter_seconds
            )),
        )
        delay = base + jitter
        self._circuit_open_until = time.monotonic() + delay
        with self._counters_lock:
            self.circuit_windows += 1
        logger.warning(
            "BSL overlap: embedding endpoint unavailable — direct circuit open "
            "for %.0fs; SQLite catch-up will recover these units", delay,
        )
