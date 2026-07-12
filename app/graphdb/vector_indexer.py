"""
Vector indexer coordinator - manages background embedding indexing.
"""
from __future__ import annotations

import logging
import asyncio
import dataclasses
import random
import time
from typing import Optional, Union
from config import settings
from graphdb.embedding_service import (
    EmbeddingService,
    get_embedding_service,
    is_embedding_unavailable_error,
    format_embedding_error,
)
from graphdb import embedding_usage_metrics as embedding_metrics
from graphdb.routine_indexer import RoutineDescriptionIndexer
from graphdb.metadata_indexer import MetadataObjectDescriptionIndexer

logger = logging.getLogger(__name__)

# Degraded-reason key owned by this indexer (routine + metadata descriptions).
_DEGRADED_KEY = "embedding:routine_metadata"


def _set_degraded(reason: str) -> None:
    """Record embedding degradation for observability; never raises."""
    try:
        from mcpsrv import runtime_state
        runtime_state.set_degraded_reason(_DEGRADED_KEY, reason)
    except Exception:
        pass


def _clear_degraded() -> None:
    """Clear embedding degradation on a successful pass (recovery); never raises."""
    try:
        from mcpsrv import runtime_state
        runtime_state.clear_degraded_reason(_DEGRADED_KEY)
    except Exception:
        pass


@dataclasses.dataclass
class _DescriptionPhaseOutcome:
    """Aggregate result of one description phase across all its outer rounds.

    `outage_exhausted` is True only when the final round ended in an embedding
    outage and the round budget was used up; the remaining `..._embedding IS NULL`
    nodes are then deferred to the next pass. Totals sum every round, not just the
    last worker set.
    """

    rounds_run: int
    max_rounds: int
    outage_exhausted: bool
    outage_reason: str
    total_processed: int
    total_failed: int
    usage: embedding_metrics.EmbeddingUsageStats


class _EmbeddingOutageSignal:
    """Shared per-pass flag set by workers when the embedding endpoint becomes
    known-unavailable mid-run (after a successful preflight/fingerprint).

    Lets the coordinator stop the pass, keep the degraded reason instead of
    clearing it, and suppress per-batch tracebacks. `signal` returns True only
    for the first hit so the outage is logged once, not per batch/worker.
    """

    def __init__(self) -> None:
        self.hit = False
        self.reason = ""

    def signal(self, reason: str) -> bool:
        first = not self.hit
        self.hit = True
        if reason and not self.reason:
            self.reason = reason
        return first


class VectorIndexer:
    """Coordinates background vector indexing with multiple workers"""

    def __init__(self, driver, *, embedding_availability=None):
        """
        Initialize vector indexer coordinator.

        Args:
            driver: Neo4j driver instance
            embedding_availability: optional startup EmbeddingAvailability. When
                set with available=False, this pass short-circuits before the
                embedding service, fingerprint and workers.
        """
        self.driver = driver
        self.num_workers = settings.embedding_indexing_workers
        self.embedding_service: Optional[EmbeddingService] = None
        self.routine_workers: list[RoutineDescriptionIndexer] = []
        self.metadata_workers: list[MetadataObjectDescriptionIndexer] = []
        self.is_running = False
        self._embedding_availability = embedding_availability
        # Per-round signal set by workers on a mid-run embedding outage. Rebound to
        # a fresh signal at the start of every phase round (see
        # _run_description_phase_rounds) so a routine outage cannot bleed into the
        # metadata phase.
        self._outage = _EmbeddingOutageSignal()
        # Degraded reasons accumulated across the phases of the current vector pass
        # (one entry per phase that exhausted its rounds in an outage). Drives
        # _finalize_degraded so an earlier degraded phase is not masked by a later
        # clean one. Reset at the start of each pass / single-phase entry-point.
        self._description_outage_reasons: list[str] = []
        # Current phase round state (round-local live progress; the durable phase
        # progress lives in Neo4j and the phase totals in the completion log).
        self._current_phase_round: int = 0
        self._current_phase_max_rounds: int = 0

        logger.info(f"VectorIndexer initialized with {self.num_workers} workers")

    async def start_indexing(self) -> None:
        """Start background indexing process - sequentially indexes routines then metadata objects"""
        # Check if any indexing is enabled
        if not settings.enable_routine_description_embedding and not settings.enable_metadata_description_embedding:
            logger.info("Both routine and metadata description embeddings are disabled, skipping vector indexing")
            return

        if self.is_running:
            logger.warning("Vector indexing is already running")
            return

        # EARLY CHECK: Verify that required vector indexes exist BEFORE initializing embedding service
        # This prevents unnecessary errors when embedding model is unavailable at startup
        indexing_plan = []
        missing_indexes = []

        if settings.enable_routine_description_embedding:
            indexing_plan.append("routine descriptions")
            if not self._check_vector_index_exists('vec_routine_doc_description'):
                missing_indexes.append('vec_routine_doc_description')

        if settings.enable_metadata_description_embedding:
            indexing_plan.append("metadata objects")
            if not self._check_vector_index_exists('vec_metadataobject_description'):
                missing_indexes.append('vec_metadataobject_description')

        # If any required indexes are missing, abort early
        if missing_indexes:
            logger.error(
                f"Required vector indexes are missing: {', '.join(missing_indexes)}. "
                f"Vector indexing cannot proceed this pass. This typically happens when the "
                f"embedding service was not accessible when the indexes would have been created. "
                f"They are recreated automatically on the next startup once the embedding API is "
                f"available."
            )
            return

        # Startup availability short-circuit: a known-unavailable endpoint skips
        # this pass before the embedding service, fingerprint and workers. No
        # traceback; deferred to the next startup/scheduler pass.
        avail = self._embedding_availability
        if avail is not None and avail.enabled and not avail.available:
            logger.warning(
                "Vector indexing skipped this pass: embedding unavailable (%s)",
                avail.reason,
            )
            _set_degraded(f"embedding unavailable: {avail.reason}")
            return

        # Fresh outage signal + degraded reasons for this pass (each phase round
        # rebinds _outage; reasons accumulate per exhausted phase).
        self._outage = _EmbeddingOutageSignal()
        self._description_outage_reasons = []

        logger.info(f"Starting vector indexing: {' -> '.join(indexing_plan)}")

        # Start timing
        total_start_time = time.time()

        try:
            # Initialize embedding service (singleton)
            self.embedding_service = get_embedding_service()

            if self.embedding_service is None:
                logger.error("Embedding service is not available (embeddings disabled)")
                return

            # Ensure project fingerprint and clear stale embeddings if model/provider changed
            await self._ensure_project_fingerprint()

            self.is_running = True

            # Step 1: Index Routine descriptions (if enabled)
            if settings.enable_routine_description_embedding:
                await self._index_routine_descriptions()

            # Step 2: Index MetadataObject descriptions (if enabled)
            if settings.enable_metadata_description_embedding:
                await self._index_metadata_descriptions()

            # Calculate total time
            total_elapsed = time.time() - total_start_time
            hours = int(total_elapsed // 3600)
            minutes = int((total_elapsed % 3600) // 60)
            seconds = int(total_elapsed % 60)

            if hours > 0:
                time_str = f"{hours}h {minutes}m {seconds}s"
            elif minutes > 0:
                time_str = f"{minutes}m {seconds}s"
            else:
                time_str = f"{seconds}s"

            logger.info(f"Sequential vector indexing completed in {time_str} (total: {total_elapsed:.1f}s)")
            # A worker may have hit a mid-run outage: keep the degraded reason
            # instead of clearing it as if the pass had fully succeeded.
            self._finalize_degraded()

        except Exception as e:
            if is_embedding_unavailable_error(e):
                # Endpoint dropped after preflight (e.g. during fingerprint or the
                # first embed batch): one line, no traceback, stop this pass.
                reason = format_embedding_error(e)
                logger.warning(
                    "Vector indexing stopped this pass: embedding endpoint unavailable (%s)",
                    reason,
                )
                _set_degraded(f"embedding unavailable: {reason}")
            else:
                logger.error(f"Failed to start vector indexing: {e}", exc_info=True)
        finally:
            self.is_running = False

    def _finalize_degraded(self) -> None:
        """Set or clear the degraded reason based on this pass's accumulated
        per-phase outage reasons.

        Each phase appends a reason only when it exhausted all its rounds in an
        embedding outage. Clearing only when no phase degraded makes recovery (a
        later successful re-pass) remove the reason, while an exhausted phase keeps
        it even if a subsequent phase completed cleanly.
        """
        reasons = self._description_outage_reasons
        if reasons:
            combined = "; ".join(reasons)
            logger.warning(
                "Vector indexing degraded this pass: embedding endpoint unavailable (%s)",
                combined,
            )
            _set_degraded(f"embedding unavailable: {combined}")
        else:
            _clear_degraded()

    def _description_round_backoff(self) -> float:
        """Backoff (with jitter) applied only between description phase rounds,
        after a round ended in an embedding outage. Mirrors the BSL Phase B
        inter-round backoff; it does not replace EMBEDDING_MAX_RETRIES."""
        base = float(settings.embedding_description_indexing_round_backoff_seconds)
        jitter = float(settings.embedding_description_indexing_round_backoff_jitter_seconds)
        return base + random.uniform(0.0, max(0.0, jitter))

    async def _run_description_phase_rounds(
        self,
        *,
        phase_label: str,
        worker_kind_label: str,
        worker_cls,
        workers_attr: str,
        task_name_prefix: str,
    ) -> _DescriptionPhaseOutcome:
        """Run one description phase as a sequence of outer rounds.

        Each round gets a fresh `_EmbeddingOutageSignal` and a fresh worker set;
        workers fetch only `..._embedding IS NULL` nodes, so a round naturally
        continues the durable remainder left by a prior outage round. A clean
        round finishes the phase; an outage round retries after a backoff until the
        round budget (`embedding_description_indexing_max_rounds`, clamped to >=1)
        is exhausted, after which the remainder is deferred to the next pass.

        Totals (processed/failed/usage) are aggregated across every round. Returns
        a `_DescriptionPhaseOutcome`; the caller records the degraded reason when
        `outage_exhausted` is True.
        """
        max_rounds = max(1, int(settings.embedding_description_indexing_max_rounds))
        phase_start_time = time.time()

        total_processed = 0
        total_failed = 0
        phase_usage = embedding_metrics.EmbeddingUsageStats()
        rounds_run = 0
        outage_exhausted = False
        outage_reason = ""

        self._current_phase_max_rounds = max_rounds

        for round_idx in range(1, max_rounds + 1):
            rounds_run = round_idx
            self._current_phase_round = round_idx

            # Fresh per-round outage signal: a prior phase's/round's outage never
            # bleeds into this round (workers read it via outage_signal).
            round_outage = _EmbeddingOutageSignal()
            self._outage = round_outage

            workers = [
                worker_cls(
                    driver=self.driver,
                    worker_id=i,
                    total_workers=self.num_workers,
                    embedding_service=self.embedding_service,
                    outage_signal=round_outage,
                )
                for i in range(self.num_workers)
            ]
            # Expose the current round's workers for live status.
            setattr(self, workers_attr, workers)

            logger.info(f"{phase_label} round {round_idx}/{max_rounds} started")
            tasks = [
                asyncio.create_task(
                    self._run_worker(worker),
                    name=f"{task_name_prefix}_{worker.worker_id}_r{round_idx}",
                )
                for worker in workers
            ]
            logger.info(
                f"Started {len(tasks)} {worker_kind_label} workers for round {round_idx}/{max_rounds}"
            )

            await asyncio.gather(*tasks, return_exceptions=True)

            for worker in workers:
                phase_usage.add(worker.embedding_usage)
            total_processed += sum(w.total_processed for w in workers)
            total_failed += sum(w.total_failed for w in workers)

            if not round_outage.hit:
                logger.info(
                    f"{phase_label} round {round_idx}/{max_rounds} completed cleanly"
                )
                break

            if round_idx < max_rounds:
                delay = self._description_round_backoff()
                logger.warning(
                    f"{phase_label} round {round_idx}/{max_rounds} stopped due to "
                    f"embedding outage ({round_outage.reason}); retrying remaining "
                    f"descriptions in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue

            # Last round still in outage: defer the remainder to the next pass.
            outage_exhausted = True
            outage_reason = round_outage.reason
            logger.warning(
                f"{phase_label} exhausted {max_rounds}/{max_rounds} rounds due to "
                f"embedding outage ({round_outage.reason}); remaining descriptions "
                f"deferred to next pass"
            )

        phase_elapsed = time.time() - phase_start_time
        hours = int(phase_elapsed // 3600)
        minutes = int((phase_elapsed % 3600) // 60)
        seconds = int(phase_elapsed % 60)
        if hours > 0:
            time_str = f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s"
        else:
            time_str = f"{seconds}s"

        status_verb = "stopped after exhausting rounds" if outage_exhausted else "completed"
        logger.info(
            f"{phase_label} {status_verb} in {time_str} ({phase_elapsed:.1f}s), "
            f"rounds={rounds_run}/{max_rounds}, "
            f"processed={total_processed}, failed={total_failed}, "
            f"embedding_api_calls={phase_usage.embedding_api_calls}, "
            f"input_tokens={embedding_metrics.format_usage_tokens(phase_usage.input_tokens)}, "
            f"total_tokens={embedding_metrics.format_usage_tokens(phase_usage.total_tokens)}, "
            f"cost={embedding_metrics.format_cost(*phase_usage.primary_cost())}"
        )

        return _DescriptionPhaseOutcome(
            rounds_run=rounds_run,
            max_rounds=max_rounds,
            outage_exhausted=outage_exhausted,
            outage_reason=outage_reason,
            total_processed=total_processed,
            total_failed=total_failed,
            usage=phase_usage,
        )

    async def _index_routine_descriptions(self) -> None:
        """Index routine descriptions across outer rounds (multiple workers)."""
        logger.info("Phase 1: Starting routine description indexing")
        try:
            outcome = await self._run_description_phase_rounds(
                phase_label="Phase 1: Routine description indexing",
                worker_kind_label="routine description embedding",
                worker_cls=RoutineDescriptionIndexer,  # module global -> monkeypatch-friendly
                workers_attr="routine_workers",
                task_name_prefix="routine_worker",
            )
            if outcome.outage_exhausted:
                self._description_outage_reasons.append(f"routine: {outcome.outage_reason}")
        except Exception as e:
            logger.error(f"Failed during routine description indexing: {e}", exc_info=True)

    async def run_routine_descriptions_pass(self, project_name: str) -> None:
        """Public entry-point для post-sync routine doc embedding re-pass (scheduler).

        Выполняет routine-only preflight (gate + index check + embedding service init)
        БЕЗ fingerprint policy — она прерогатива boot path (`_ensure_project_fingerprint`).
        После preflight вызывает существующий `_index_routine_descriptions`.
        """
        if not settings.enable_routine_description_embedding:
            logger.info("run_routine_descriptions_pass: feature disabled, skipping")
            return
        if not self._check_vector_index_exists("vec_routine_doc_description"):
            logger.warning(
                "run_routine_descriptions_pass: vec_routine_doc_description index missing; "
                "skipping post-sync routine embedding re-pass"
            )
            return
        if self.embedding_service is None:
            self.embedding_service = get_embedding_service()
        if self.embedding_service is None:
            logger.warning("run_routine_descriptions_pass: embedding service unavailable")
            return
        self._outage = _EmbeddingOutageSignal()
        self._description_outage_reasons = []
        await self._index_routine_descriptions()
        self._finalize_degraded()

    async def run_metadata_descriptions_pass(self, project_name: str) -> None:
        """Public entry-point для post-sync metadata embedding re-pass (scheduler).

        Выполняет metadata-only preflight (gate + index check + embedding service init)
        БЕЗ fingerprint policy — она прерогатива boot path (`_ensure_project_fingerprint`).
        После preflight вызывает существующий `_index_metadata_descriptions`.

        См. план Architectural decision 11.
        """
        if not settings.enable_metadata_description_embedding:
            logger.info("run_metadata_descriptions_pass: feature disabled, skipping")
            return
        if not self._check_vector_index_exists("vec_metadataobject_description"):
            logger.warning(
                "run_metadata_descriptions_pass: vec_metadataobject_description index missing; "
                "skipping post-sync embedding re-pass"
            )
            return
        if self.embedding_service is None:
            self.embedding_service = get_embedding_service()
        if self.embedding_service is None:
            logger.warning("run_metadata_descriptions_pass: embedding service unavailable")
            return
        self._outage = _EmbeddingOutageSignal()
        self._description_outage_reasons = []
        await self._index_metadata_descriptions()
        self._finalize_degraded()

    async def _index_metadata_descriptions(self) -> None:
        """Index metadata object descriptions across outer rounds (multiple workers).

        Each round gets its own fresh outage signal, so a prior routine-phase
        outage never suppresses this phase.
        """
        logger.info("Phase 2: Starting MetadataObject description indexing")
        try:
            outcome = await self._run_description_phase_rounds(
                phase_label="Phase 2: MetadataObject description indexing",
                worker_kind_label="MetadataObject description embedding",
                worker_cls=MetadataObjectDescriptionIndexer,  # module global -> monkeypatch-friendly
                workers_attr="metadata_workers",
                task_name_prefix="metadata_worker",
            )
            if outcome.outage_exhausted:
                self._description_outage_reasons.append(f"metadata: {outcome.outage_reason}")
        except Exception as e:
            logger.error(f"Failed during MetadataObject description indexing: {e}", exc_info=True)

    def _check_vector_index_exists(self, index_name: str) -> bool:
        """
        Check if a vector index exists.

        Args:
            index_name: Name of the vector index to check

        Returns:
            True if index exists, False otherwise
        """
        try:
            with self.driver.session(database=settings.neo4j_database) as session:
                result = session.run(
                    """
                    SHOW INDEXES YIELD name, type
                    WHERE name = $index_name AND type = 'VECTOR'
                    RETURN count(*) as exists
                    """,
                    index_name=index_name
                )
                record = result.single()
                exists = record['exists'] > 0 if record else False

                if exists:
                    logger.info(f"Vector index '{index_name}' exists")
                else:
                    logger.warning(f"Vector index '{index_name}' does not exist")

                return exists
        except Exception as e:
            logger.error(f"Failed to check vector index existence: {e}", exc_info=True)
            return False

    def _recreate_vector_indexes_if_needed(self) -> None:
        """
        Recreate vector indexes if the embedding dimension has changed.
        This method checks current dimension from the embedding model and compares with existing indexes.
        If dimensions don't match, indexes are dropped and recreated.
        """
        try:
            # Get current dimension from embedding model
            from graphdb.indexes import IndexManagementMixin

            # Create a temporary instance just to get dimension
            class TempIndexManager(IndexManagementMixin):
                def __init__(self, driver):
                    self.driver = driver

            temp_manager = TempIndexManager(self.driver)
            dimension = temp_manager.get_embedding_dimension_from_config()

            if dimension is None:
                logger.warning("Could not determine embedding dimension, skipping index recreation")
                return

            logger.info(f"Current embedding dimension: {dimension}")

            with self.driver.session(database=settings.neo4j_database) as session:
                from graphdb.indexes import ensure_vector_index

                index_names: list[str] = []
                if settings.enable_routine_description_embedding:
                    index_names.append("vec_routine_doc_description")
                if settings.enable_metadata_description_embedding:
                    index_names.append("vec_metadataobject_description")

                for index_name in index_names:
                    # ensure_vector_index checks dimension AND properties; drops + recreates only when needed.
                    # This avoids the race condition described previously: if another container already
                    # aligned the index, ensure_vector_index returns 'kept' and no DROP is issued.
                    ensure_vector_index(session, index_name, dimension)

        except Exception as e:
            logger.error(f"Failed to recreate vector indexes: {e}", exc_info=True)

    async def _run_worker(self, worker: Union[RoutineDescriptionIndexer, MetadataObjectDescriptionIndexer]) -> None:
        """
        Run a single worker with error handling.

        Args:
            worker: Worker instance to run (either RoutineDescriptionIndexer or MetadataObjectDescriptionIndexer)
        """
        try:

            await worker.run()

        except Exception as e:
            if is_embedding_unavailable_error(e):
                # Expected outage that escaped the worker's own quiet handling:
                # one warning, no traceback. The outage signal / _finalize_degraded
                # carries the degraded state; the exception is swallowed as before.
                logger.warning(
                    "Worker %s stopped: embedding endpoint unavailable (%s)",
                    worker.worker_id, format_embedding_error(e),
                )
            else:
                logger.error(f"Worker {worker.worker_id} failed: {e}", exc_info=True)

    async def _ensure_project_fingerprint(self) -> None:
        """
        Delegate to the shared embedding fingerprint helper.

        The helper owns invalidation of all vector fields that depend on
        the project embedding model, including `MetadataObject.object_summary_embedding`,
        regardless of feature flags. This indexer only needs to know whether
        the fingerprint changed so it can rebuild the vector indexes with a
        possibly new dimension.
        """
        try:
            from graphdb.embedding_fingerprint import ensure_project_embedding_fingerprint

            unchanged = await ensure_project_embedding_fingerprint(
                self.driver, self.embedding_service
            )
            if not unchanged:
                logger.info("Recreating vector indexes for new embedding model...")
                self._recreate_vector_indexes_if_needed()
        except Exception as e:
            if is_embedding_unavailable_error(e):
                # Endpoint outage during the fingerprint probe: one warning, no
                # traceback, then re-raise so the outer quiet handler stops this
                # pass without printing an already-logged stack trace.
                logger.warning(
                    "Project embedding fingerprint check skipped: "
                    "embedding endpoint unavailable (%s)",
                    format_embedding_error(e),
                )
            else:
                logger.error(
                    "Project embedding fingerprint check failed: %s. "
                    "This typically means the embedding model is not accessible. "
                    "Vector indexing cannot proceed without a working embedding service.",
                    e,
                    exc_info=True
                )
            raise  # Re-raise to stop indexing - we cannot proceed without fingerprint

    async def stop_indexing(self) -> None:
        """No-op stop: indexing runs to natural completion."""
        logger.info("Stop requested (no-op): indexing will complete naturally")

    def get_status(self) -> dict:
        """
        Get current indexing status for both routine and metadata indexing.

        Returns:
            Dictionary with status information for both phases
        """
        if not self.is_running:
            return {
                'running': False,
                'routine': {
                    'workers': 0,
                    'total_processed': 0,
                    'total_failed': 0
                },
                'metadata': {
                    'workers': 0,
                    'total_processed': 0,
                    'total_failed': 0
                }
            }

        # Routine indexing status
        routine_total_processed = sum(w.total_processed for w in self.routine_workers)
        routine_total_failed = sum(w.total_failed for w in self.routine_workers)
        routine_total_to_index = sum(w.total_to_index for w in self.routine_workers)
        routine_progress_pct = (routine_total_processed / routine_total_to_index * 100) if routine_total_to_index > 0 else 0

        # Metadata indexing status
        metadata_total_processed = sum(w.total_processed for w in self.metadata_workers)
        metadata_total_failed = sum(w.total_failed for w in self.metadata_workers)
        metadata_total_to_index = sum(w.total_to_index for w in self.metadata_workers)
        metadata_progress_pct = (metadata_total_processed / metadata_total_to_index * 100) if metadata_total_to_index > 0 else 0

        return {
            'running': True,
            'routine': {
                'workers': len(self.routine_workers),
                'total_processed': routine_total_processed,
                'total_to_index': routine_total_to_index,
                'progress_percent': round(routine_progress_pct, 1),
                'total_failed': routine_total_failed,
                'workers_detail': [
                    {
                        'worker_id': w.worker_id,
                        'processed': w.total_processed,
                        'to_index': w.total_to_index,
                        'progress_percent': round((w.total_processed / w.total_to_index * 100) if w.total_to_index > 0 else 0, 1),
                        'failed': w.total_failed,
                        'is_running': w.is_running
                    }
                    for w in self.routine_workers
                ]
            },
            'metadata': {
                'workers': len(self.metadata_workers),
                'total_processed': metadata_total_processed,
                'total_to_index': metadata_total_to_index,
                'progress_percent': round(metadata_progress_pct, 1),
                'total_failed': metadata_total_failed,
                'workers_detail': [
                    {
                        'worker_id': w.worker_id,
                        'processed': w.total_processed,
                        'to_index': w.total_to_index,
                        'progress_percent': round((w.total_processed / w.total_to_index * 100) if w.total_to_index > 0 else 0, 1),
                        'failed': w.total_failed,
                        'is_running': w.is_running
                    }
                    for w in self.metadata_workers
                ]
            }
        }
