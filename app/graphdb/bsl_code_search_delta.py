"""Scoped delta applier for the BSL code search subsystem (Phase 5).

Two transactional models live side-by-side with `BslCodeSearchIndexer`:

- `start_indexing()` — full rebuild via pending epoch swap.
- `BslCodeSearchDeltaApplier.apply()` — ledger-driven scoped mutator that
  stays inside the current epoch.

The applier is the single source of truth for cross-store consistency on
scoped changes (Neo4j + SQLite have no shared tx). It reads `bsl_code_pending_scoped_delta`
as a durable ledger, replays whatever stage was reached by a previous
crashed cycle, and only `commit_scoped_delta`s once all stores agree.

Cross-store ordering (see plan §11):

  5.   SQLite gate ON atomically with `scoped_retry_pending = 1` and
       `visibility_flip_done = 0`. Search service immediately treats every
       routine in `pending_routine_ids_json` as RLM-only.
  5.5. Neo4j visibility flip — set `code_embedding_visible = false` on
       both small `Routine` units and large `RoutineCodeUnit` chunks of
       the affected routines.
  5.7. SQLite `visibility_flip_done = 1`. Search service may now use the
       vector leg again, with the prefilter doing the work.
  6.a. Neo4j `REMOVE` (small) + `DETACH DELETE` (large) for routines whose
       ledger stage is still `snapshot_written`.
  6.b. SQLite tx: reverse counters + delete old units + insert new units +
       positive counters + clear snapshot rows + ledger stage -> `sqlite_applied`.
  7.   Scoped Phase B for `changed`/`added` routines (writes embeddings with
       `visible = false`); ledger stage -> `phase_b_done`. For `deleted` /
       `metadata_only` Phase B is skipped — they go straight to `phase_b_done`.
  8.   Module FTS rebuild for affected `rel_path`s from persisted fragments.
  9.   Recompute `source_state_hash`.
  9.5. Restore Neo4j visibility according to coverage policy for
       `change_kind in (changed, added)` routines that reached `phase_b_done`.
 10.   `commit_scoped_delta` — atomic UPDATE that clears all scoped flags
       and removes the ledger / snapshot rows.

On failure between any two steps, `scoped_retry_pending` stays at 1 and the
ledger is preserved so the next cycle replays from the correct stage. The
applier NEVER sets `reindex_requested` on its own — that flag belongs to
fingerprint mismatch / operational full rebuild path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .cypher_templates import CYPHER_DELETE_ROUTINE_CODE_UNITS_BY_IDS

logger = logging.getLogger(__name__)


class DeltaReadiness(Enum):
    """Semantic state of the fingerprint row."""
    READY = "ready"
    PENDING_REBUILD = "pending_rebuild"
    REINDEX_REQUIRED = "reindex_required"
    SCOPED_RETRY = "scoped_retry"


class ApplyResult(Enum):
    APPLIED = "applied"
    APPLIED_EMPTY = "applied_empty"
    SKIPPED_EMPTY = "skipped_empty"
    SKIPPED_FULL_REBUILD_REQUIRED = "skipped_full_rebuild_required"
    SKIPPED_PENDING_REBUILD = "skipped_pending_rebuild"
    SKIPPED_PENDING_RACE = "skipped_pending_race"
    SKIPPED_NO_BASE_INDEX = "skipped_no_base_index"
    PHASE_B_DEFERRED = "phase_b_deferred"
    FAILED = "failed"
    FAILED_RETRY_QUEUED = "failed_retry_queued"


@dataclass
class CodeSearchDelta:
    """Same shape as `incremental/bsl_routine_delta.CodeSearchDelta` but owned
    by the BSL code search subsystem on entry to `apply()`."""
    added_or_changed_routine_ids: Set[str] = field(default_factory=set)
    deleted_routine_ids: Set[str] = field(default_factory=set)
    metadata_only_routine_ids: Set[str] = field(default_factory=set)
    affected_rel_paths: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not (
            self.added_or_changed_routine_ids
            or self.deleted_routine_ids
            or self.metadata_only_routine_ids
            or self.affected_rel_paths
        )

    @classmethod
    def empty_placeholder(cls) -> "CodeSearchDelta":
        """Marker delta used by SCOPED_RETRY replay — applier rebuilds the
        actual work set from the persisted ledger."""
        return cls()


class _BslCodeSearchSnapshotFailed(Exception):
    """Raised by `_apply_bsl` step 4.5 if the snapshot/ledger could not be
    persisted before `load_bsl_signatures` overwrites Neo4j body. Surfaced
    as an applier-internal signal — outer caller aborts the BSL apply for
    this cycle to preserve the old body for the next scoped retry."""


class BslCodeSearchDeltaApplier:
    """Scoped Neo4j + SQLite mutator for routine-level changes."""

    def __init__(self, sqlite: Any, indexer: Any) -> None:
        self.sqlite = sqlite
        self.indexer = indexer

    # ------------------------------------------------------------------ entry

    def apply(
        self,
        scope: str,
        delta: CodeSearchDelta,
        lease: Optional[Any] = None,
    ) -> ApplyResult:
        # 1. Config fingerprint mismatch → full rebuild path owns the recovery.
        try:
            current_fp = self.indexer._compute_config_fingerprint()
        except Exception:
            logger.exception("apply: _compute_config_fingerprint failed")
            return ApplyResult.FAILED
        try:
            stored = self.sqlite.read_fingerprint(scope)
        except Exception:
            stored = None
        stored_fp = (stored.get("fingerprint") if stored else "") or ""
        if stored_fp and stored_fp != current_fp:
            try:
                self.sqlite.request_reindex(scope)
            except Exception:
                logger.exception(
                    "apply: failed to set reindex_requested on fingerprint mismatch"
                )
            return ApplyResult.SKIPPED_FULL_REBUILD_REQUIRED

        # 2. A full rebuild is already in flight — scoped apply must yield.
        if self.sqlite.has_active_pending(scope):
            return ApplyResult.SKIPPED_PENDING_REBUILD

        # 3. current_epoch must exist; no base index means initial full load
        # hasn't happened yet and scoped apply can't write into a missing epoch.
        current_epoch = self.sqlite.get_current_epoch(scope)
        if current_epoch is None or int(current_epoch) <= 0:
            try:
                self.sqlite.request_reindex(scope)
            except Exception:
                logger.exception(
                    "apply: failed to set reindex_requested on missing base index"
                )
            return ApplyResult.SKIPPED_NO_BASE_INDEX
        current_epoch = int(current_epoch)
        try:
            vector_state = self.sqlite.vector_state(scope)
            vector_epoch = int(getattr(vector_state, "vector_epoch", None)
                               or current_epoch)
        except Exception:
            vector_epoch = current_epoch

        # 4. Read ledger BEFORE gate (R20 F1 + R4 F2): single source of truth
        # for what work is pending and what target set the gate must cover.
        try:
            ledger = self.sqlite.read_pending_scoped_delta(scope)
        except Exception:
            logger.exception("apply: read_pending_scoped_delta failed")
            return ApplyResult.FAILED
        if not ledger:
            # Fresh apply with empty in-memory delta — nothing to do.
            if delta is None or delta.is_empty():
                return ApplyResult.APPLIED_EMPTY
            # Fresh apply was supposed to write a ledger row in step 4.5 of
            # `_apply_bsl` but the in-memory delta is non-empty here. This
            # means the caller skipped step 4.5 (BSL code search disabled,
            # snapshot failed gracefully). Treat as empty: nothing to apply.
            return ApplyResult.APPLIED_EMPTY

        ledger_routine_ids = {r["routine_id"] for r in ledger}
        ledger_rel_paths = self._collect_rel_paths_from_ledger(
            scope, current_epoch, ledger,
        )
        by_stage = self._group_by_stage(ledger)

        # 5. SQLite gate ON + scoped_retry_pending=1 + visibility_flip_done=0
        # atomically. From this point on, search service excludes the affected
        # routines from the RLM leg and uses conservative path for vector.
        try:
            self.sqlite.set_scoped_apply_in_progress_atomic(
                scope, True,
                routine_ids=ledger_routine_ids,
                rel_paths=ledger_rel_paths,
                also_set_scoped_retry_pending=True,
                visibility_flip_done=False,
            )
        except Exception:
            logger.exception("apply: set_scoped_apply_in_progress_atomic failed")
            return ApplyResult.FAILED

        try:
            # 5.5 Neo4j visibility flip (only for routines that actually get
            # invalidated — metadata_only stays visible).
            visibility_flip_ids = {
                r["routine_id"] for r in ledger
                if r["change_kind"] in ("changed", "added", "deleted")
            }
            if visibility_flip_ids:
                self.indexer._neo4j_set_visibility_false_for_routines(
                    scope, list(visibility_flip_ids),
                )

            # 5.7 Signal search service it may use the vector leg again.
            self.sqlite.mark_visibility_flip_done(scope, True)

            # 6. Per-stage work.
            sqlite_applied_ids: Set[str] = set()
            phase_b_done_via_embed: Set[str] = set()

            todo_sqlite = by_stage.get("snapshot_written", [])
            if todo_sqlite:
                # 6.a Neo4j clear + DETACH for changed/added/deleted.
                invalidated_ids = {
                    r["routine_id"] for r in todo_sqlite
                    if r["change_kind"] in ("changed", "added", "deleted")
                }
                if invalidated_ids:
                    self._neo4j_clear_routine_code_embeddings(
                        scope, list(invalidated_ids),
                    )
                    self._neo4j_delete_routine_code_units(
                        scope, list(invalidated_ids),
                    )

                # 6.b SQLite tx for snapshot_written rows (split by change_kind).
                snapshot = self.sqlite.read_pending_reverse_snapshot(
                    scope, [r["routine_id"] for r in todo_sqlite],
                )
                self._scoped_sqlite_apply(
                    scope, current_epoch, todo_sqlite, snapshot,
                    lease=lease,
                )
                sqlite_applied_ids = {r["routine_id"] for r in todo_sqlite}

            # 7. Scoped Phase B for stage='sqlite_applied' AND change_kind∈{added,changed}.
            change_kind_by_rid = {r["routine_id"]: r["change_kind"] for r in ledger}
            candidate_phase_b_targets = (
                sqlite_applied_ids
                | {
                    r["routine_id"]
                    for r in by_stage.get("sqlite_applied", [])
                    if r["change_kind"] in ("changed", "added")
                }
            ) & {
                rid
                for rid, ck in change_kind_by_rid.items()
                if ck in ("changed", "added")
            }

            try:
                from config import settings as _runtime_settings
                _phase_b_enabled = bool(
                    getattr(_runtime_settings, "enable_bsl_code_search", False)
                    and getattr(_runtime_settings, "enable_bsl_code_embedding", False)
                )
            except Exception:
                _phase_b_enabled = False

            if _phase_b_enabled and candidate_phase_b_targets:
                try:
                    result = asyncio.run(
                        self.indexer._embed_units_for_routines(
                            scope, current_epoch, vector_epoch,
                            candidate_phase_b_targets,
                            lease=lease,
                        )
                    )
                except Exception as e:
                    from .embedding_service import is_embedding_unavailable_error
                    if is_embedding_unavailable_error(e):
                        # Expected embedding outage: defer without traceback.
                        logger.warning(
                            "apply: scoped Phase B deferred (embedding unavailable): %s", e,
                        )
                        return ApplyResult.PHASE_B_DEFERRED
                    logger.exception("apply: scoped Phase B failed")
                    return ApplyResult.FAILED_RETRY_QUEUED
                from .bsl_code_indexer import PhaseBOutcome  # local import (cycle)
                if result.outcome == PhaseBOutcome.SUCCESS:
                    phase_b_done_via_embed = set(candidate_phase_b_targets)
                    self.sqlite.update_pending_scoped_delta_stage(
                        scope, phase_b_done_via_embed, stage="phase_b_done",
                    )
                else:
                    logger.info(
                        "BslCodeSearchDeltaApplier: scoped Phase B %s: %s",
                        getattr(result.outcome, "value", result.outcome),
                        result.reason or "(no reason)",
                    )
                    return ApplyResult.PHASE_B_DEFERRED

            # routines with no Phase B step (deleted, metadata_only, or
            # changed/added when embeddings are disabled) → straight to
            # phase_b_done.
            no_phase_b_ids = (
                sqlite_applied_ids
                | {r["routine_id"] for r in by_stage.get("sqlite_applied", [])}
            ) - phase_b_done_via_embed
            if no_phase_b_ids:
                self.sqlite.update_pending_scoped_delta_stage(
                    scope, no_phase_b_ids, stage="phase_b_done",
                )

            # 8. Module FTS rebuild for every affected rel_path.
            if ledger_rel_paths:
                self.indexer._rebuild_module_fts_for_rel_paths(
                    scope, current_epoch, ledger_rel_paths,
                )

            # 9. source_state_hash recompute.
            try:
                lightweight = self.indexer._fetch_routines_lightweight()
                new_src_hash = self.indexer._compute_source_state_hash(lightweight)
            except Exception:
                logger.exception("apply: source_state_hash recompute failed")
                return ApplyResult.FAILED_RETRY_QUEUED

            # 9.5 Scoped visibility restore (R18+R19+R20): only for
            # changed/added routines that reached phase_b_done in this
            # cycle (`phase_b_done_via_embed`) plus those that were already
            # `phase_b_done` from a crashed previous cycle.
            visibility_restore_ids = (
                phase_b_done_via_embed
                | {
                    r["routine_id"]
                    for r in by_stage.get("phase_b_done", [])
                    if r["change_kind"] in ("changed", "added")
                }
            )
            if visibility_restore_ids:
                from config import settings as _runtime_settings
                excluded_owner_categories = list(
                    getattr(
                        _runtime_settings,
                        "bsl_code_embedding_excluded_owner_categories",
                        (),
                    ) or ()
                )
                exclude_regulated_reports = bool(
                    getattr(
                        _runtime_settings,
                        "bsl_code_search_exclude_regulated_reports",
                        False,
                    )
                )
                try:
                    self.indexer._neo4j_restore_visibility_for_committed(
                        scope, vector_epoch, list(visibility_restore_ids),
                        excluded_owner_categories, exclude_regulated_reports,
                    )
                except Exception:
                    logger.exception(
                        "apply: scoped visibility restore failed"
                    )
                    return ApplyResult.FAILED_RETRY_QUEUED

            # 10. Final atomic commit.
            try:
                committed = self.sqlite.commit_scoped_delta(
                    scope, new_src_hash, current_fp,
                    clear_ledger_routine_ids=ledger_routine_ids,
                    clear_pending_rel_paths=ledger_rel_paths,
                )
            except Exception:
                logger.exception("apply: commit_scoped_delta failed")
                return ApplyResult.FAILED_RETRY_QUEUED
            if not committed:
                # Race with background full rebuild — ledger and scoped flags
                # remain, the next cycle classifies SCOPED_RETRY and replays.
                return ApplyResult.SKIPPED_PENDING_RACE
            return ApplyResult.APPLIED

        except Exception:
            logger.exception(
                "BslCodeSearchDeltaApplier.apply: unhandled error — "
                "scoped_retry_pending stays set; ledger preserved for retry"
            )
            return ApplyResult.FAILED_RETRY_QUEUED

    # --------------------------------------------------- legacy / compat API

    def invalidate_routines(self, scope: str, routine_ids: List[str]) -> None:
        """Direct Neo4j-side invalidation for backwards-compat callers.

        Used by `_apply_bsl` (`code_embeddings_to_clear` step) when scoped
        ledger machinery is disabled; no longer touches SQLite or sets
        `reindex_requested` (that flag is reserved for fingerprint mismatch).
        """
        rids = list(routine_ids or ())
        if not rids:
            return
        try:
            self._neo4j_clear_routine_code_embeddings(scope, rids)
        except Exception:
            logger.exception("invalidate_routines: Neo4j embedding clear failed")
            raise
        try:
            self._neo4j_delete_routine_code_units(scope, rids)
        except Exception:
            logger.exception("invalidate_routines: Neo4j RoutineCodeUnit delete failed")
            raise

    # ------------------------------------------------------------ internals

    def _group_by_stage(
        self, ledger: Sequence[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        out: Dict[str, List[Dict[str, Any]]] = {}
        for r in ledger:
            out.setdefault(r["stage"], []).append(r)
        return out

    def _collect_rel_paths_from_ledger(
        self,
        scope: str,
        current_epoch: int,
        ledger: Sequence[Dict[str, Any]],
    ) -> Set[str]:
        rel_paths: Set[str] = set()
        for r in ledger:
            if r.get("old_rel_path"):
                rel_paths.add(r["old_rel_path"])
            if r.get("new_rel_path"):
                rel_paths.add(r["new_rel_path"])
        # cover any stale rel_path still present in current SQLite units for
        # the affected routine_ids.
        ids = [r["routine_id"] for r in ledger]
        if ids:
            try:
                with self.sqlite._lock:  # type: ignore[attr-defined]
                    for start in range(0, len(ids), 500):
                        chunk = ids[start: start + 500]
                        placeholders = ",".join("?" * len(chunk))
                        cur = self.sqlite._conn.execute(  # type: ignore[attr-defined]
                            f"SELECT DISTINCT rel_path FROM bsl_code_units "
                            f"WHERE project_name = ? AND index_epoch = ? "
                            f"AND routine_id IN ({placeholders})",
                            (scope, int(current_epoch), *chunk),
                        )
                        for row in cur.fetchall():
                            rp = row[0] if not hasattr(row, "keys") else row["rel_path"]
                            if rp:
                                rel_paths.add(rp)
            except Exception:
                logger.exception(
                    "_collect_rel_paths_from_ledger: rel_path lookup failed"
                )
        return rel_paths

    def _scoped_sqlite_apply(
        self,
        scope: str,
        current_epoch: int,
        todo_sqlite: Sequence[Dict[str, Any]],
        snapshot: Dict[str, Dict[str, Any]],
        lease: Optional[Any] = None,
    ) -> None:
        """Dispatch each ledger row to its matching SQLite operation:
            changed/added → indexer._build_units_for_routines (replace path)
            deleted       → sqlite.delete_units_by_routine_ids (reverse-only)
            metadata_only → indexer._update_units_metadata_for_routines
        Aggregates per-path metrics and emits a single summary log line at the
        end. `lease` is threaded through to heartbeat the scheduler_lock on
        long-running deltas (chunked fetch, drained worker results, SQLite
        commits)."""
        import time as _time
        from config import settings as _runtime_settings
        from .bsl_code_indexer import _safe_heartbeat

        replace_ids: List[str] = []
        delete_ids: List[str] = []
        metadata_ids: List[str] = []
        for r in todo_sqlite:
            ck = r["change_kind"]
            rid = r["routine_id"]
            if ck in ("changed", "added"):
                replace_ids.append(rid)
            elif ck == "deleted":
                delete_ids.append(rid)
            elif ck == "metadata_only":
                metadata_ids.append(rid)

        t0 = _time.monotonic()
        builder_stats: Dict[str, Any] = {}
        delete_tx = 0
        metadata_count = 0

        if replace_ids:
            try:
                builder_stats = self.indexer._build_units_for_routines(
                    scope, replace_ids, set(),
                    current_epoch=current_epoch,
                    reverse_snapshot=snapshot,
                    lease=lease,
                ) or {}
            except Exception:
                logger.exception("_scoped_sqlite_apply: _build_units_for_routines failed")
                raise

        if delete_ids:
            chunk_size = max(
                1, int(getattr(_runtime_settings, "bsl_code_routine_fetch_batch_size", 1000)),
            )
            try:
                for start in range(0, len(delete_ids), chunk_size):
                    chunk = delete_ids[start: start + chunk_size]
                    idf_neg, stats_neg = self._invert_snapshot(snapshot, chunk)
                    self.sqlite.delete_units_by_routine_ids(
                        scope, current_epoch, chunk,
                        idf_reverse=idf_neg,
                        stats_reverse=stats_neg,
                        clear_snapshot_ids=chunk,
                        set_ledger_stage="sqlite_applied",
                    )
                    delete_tx += 1
                    _safe_heartbeat(lease)
            except Exception:
                logger.exception("_scoped_sqlite_apply: delete_units_by_routine_ids failed")
                raise

        if metadata_ids:
            try:
                metadata_count = self.indexer._update_units_metadata_for_routines(
                    scope, current_epoch, metadata_ids, lease=lease,
                )
                # ledger stage transition done inside update_unit_metadata_for_routines.
            except Exception:
                logger.exception(
                    "_scoped_sqlite_apply: _update_units_metadata_for_routines failed"
                )
                raise

        duration = _time.monotonic() - t0
        sqlite_tx_total = (
            int(builder_stats.get("sqlite_transactions", 0))
            + delete_tx
            + (1 if metadata_ids else 0)
        )
        logger.info(
            "BslCodeSearchSync: scoped Phase 5A complete "
            "replace=%d delete=%d metadata_only=%d "
            "records_fetched=%d missing=%d "
            "packs=%d workers=%d mode=%s "
            "units=%d methods=%d fragments=%d metadata_updated=%d "
            "sqlite_tx=%d duration=%.2fs",
            len(replace_ids), len(delete_ids), len(metadata_ids),
            int(builder_stats.get("records_fetched", 0)),
            int(builder_stats.get("missing", 0)),
            int(builder_stats.get("work_packs", 0)),
            int(builder_stats.get("workers_used", 0)),
            str(builder_stats.get("execution_mode", "n/a")),
            int(builder_stats.get("units_written", 0)),
            int(builder_stats.get("methods_written", 0)),
            int(builder_stats.get("fragments_written", 0)),
            int(metadata_count),
            sqlite_tx_total, duration,
        )

    def _invert_snapshot(
        self,
        snapshot: Dict[str, Dict[str, Any]],
        routine_ids: Iterable[str],
    ) -> Tuple[Dict[str, Dict[str, int]], Dict[str, Tuple[int, int]]]:
        idf_neg: Dict[str, Dict[str, int]] = {}
        stats_neg: Dict[str, Tuple[int, int]] = {}
        for rid in routine_ids:
            entry = snapshot.get(rid)
            if not entry:
                continue
            for fk, tok_map in (entry.get("idf") or {}).items():
                dst = idf_neg.setdefault(fk, {})
                for tok, df in tok_map.items():
                    dst[tok] = dst.get(tok, 0) - int(df)
            for fk, dc_tl in (entry.get("stats") or {}).items():
                if isinstance(dc_tl, (list, tuple)) and len(dc_tl) == 2:
                    dc, tl = dc_tl
                else:
                    dc, tl = 0, 0
                pdc, ptl = stats_neg.get(fk, (0, 0))
                stats_neg[fk] = (pdc - int(dc), ptl - int(tl))
        return idf_neg, stats_neg

    # ---------------------------------------------------------- Neo4j helpers

    def _neo4j_clear_routine_code_embeddings(
        self, scope: str, rids: List[str],
    ) -> None:
        """REMOVE r.code_embedding/_epoch/_visible + label for the small shape."""
        driver = getattr(self.indexer, "driver", None)
        if driver is None or not rids:
            return
        from config import settings
        with driver.session(database=getattr(settings, "neo4j_database", "neo4j")) as session:
            session.run(
                """
                UNWIND $ids AS rid
                MATCH (r:Routine {id: rid})
                WHERE r.project_name = $project_name
                REMOVE r:BslCodeSearchUnit
                REMOVE r.code_embedding
                REMOVE r.code_embedding_epoch
                REMOVE r.code_embedding_visible
                """,
                ids=rids,
                project_name=scope,
            )

    def _neo4j_delete_routine_code_units(
        self, scope: str, rids: List[str],
    ) -> None:
        """DETACH DELETE RoutineCodeUnit using denormalised `routine_id`.

        FIX: the previous version used `(r)<-[:OF_ROUTINE]-(u)` which referred
        to a non-existent relationship — the write contract is
        `MERGE (parent)-[:HAS_CODE_UNIT]->(u)`. The denormalised
        `u.routine_id` is the cross-cut source of truth.
        """
        driver = getattr(self.indexer, "driver", None)
        if driver is None or not rids:
            return
        from config import settings
        with driver.session(database=getattr(settings, "neo4j_database", "neo4j")) as session:
            session.run(
                CYPHER_DELETE_ROUTINE_CODE_UNITS_BY_IDS,
                routine_ids=rids,
                project_name=scope,
            )
