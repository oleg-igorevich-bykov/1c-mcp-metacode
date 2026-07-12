"""ObjectSummaryIndexer — runs S0 reconcile, S1 LLM generation and S2 embedding.

S0 (reconcile, version-aware):
  Phase A first-match: missing summary.json → clear all; missing/broken
  meta.json → clear all; source versions stale → clear all.
  Phase B accumulating: derived versions stale — embedding-only and/or
  search_text-only rebuild without an LLM call.

S1 (auto only): pick candidates, build profile, encode to TOON, call LLM,
  validate, render summary.md, write 4 files atomically, then update Neo4j.

S2: pick objects with summary but no embedding, build the embedding document,
  chunk + pool through EmbeddingService.get_embeddings_with_usage, write back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from config import settings

from object_summary.constants import (
    EMBEDDING_DOCUMENT_BUILDER_VERSION,
    PROFILE_SCHEMA_VERSION,
    SEARCH_TEXT_BUILDER_VERSION,
    SUMMARY_CONTRACT_NAME,
    SUMMARY_SCHEMA_VERSION,
    filter_supported_categories,
)
from object_summary import storage as os_storage
from object_summary.profile import build_profile
from object_summary.render import (
    build_embedding_document,
    build_search_text,
    render_markdown,
)
from object_summary.toon import encode_profile

from graphdb import object_summary_queries as q
from graphdb.embedding_chunks import split_text_for_embedding, weighted_mean_pool
from runtime_context import get_run_id
import runtime_metrics

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _format_elapsed(seconds: float) -> str:
    s = int(max(0.0, seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _format_usage_tokens(value: Optional[int]) -> str:
    return str(int(value)) if isinstance(value, int) and value >= 0 else "unknown"


def _format_cost(amount: Optional[float], unit: Optional[str], source: str) -> str:
    if source != "provider_reported" or amount is None:
        return "unknown"
    unit_label = (unit or "").strip() or "units"
    return f"{amount:.4f} {unit_label}"


@dataclass
class ObjectSummaryGenerateResult:
    ok: bool
    qualified_name: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_amount: Optional[float] = None
    cost_unit: Optional[str] = None
    cost_source: str = "unknown"
    elapsed_seconds: float = 0.0


@dataclass
class ObjectSummaryEmbeddingResult:
    ok: bool
    qualified_name: str
    input_tokens: Optional[int] = None
    cost_amount: Optional[float] = None
    cost_unit: Optional[str] = None
    cost_source: str = "unknown"
    elapsed_seconds: float = 0.0
    # Set when the failure is a known external embedding-endpoint outage. The S2
    # phase (and the manual job) use this to stop the pass with one line instead
    # of logging a traceback per object.
    embedding_unavailable: bool = False
    error_message: Optional[str] = None


# Degraded-reason key owned by the object summary S2 phase.
_OS_DEGRADED_KEY = "embedding:object_summary"


def _os_set_degraded(reason: str) -> None:
    """Record object-summary embedding degradation; never raises."""
    try:
        from mcpsrv import runtime_state
        runtime_state.set_degraded_reason(_OS_DEGRADED_KEY, reason)
    except Exception:
        pass


def _os_clear_degraded() -> None:
    """Clear object-summary embedding degradation on a successful pass; never raises."""
    try:
        from mcpsrv import runtime_state
        runtime_state.clear_degraded_reason(_OS_DEGRADED_KEY)
    except Exception:
        pass


@dataclass
class _PhaseTotals:
    processed: int = 0
    created: int = 0
    failed: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_by_unit: Dict[Tuple[str, str], float] = field(default_factory=dict)

    def primary_cost(self) -> Tuple[Optional[float], Optional[str], str]:
        """Return (amount, unit, source) for the single cost the user sees in logs.

        If at least one batch reported provider cost — sum them under that
        unit (currently always 'credits' for OpenRouter). Mixed units are
        improbable in practice but we pick the largest by amount and surface
        only that line; the SQLite aggregate keeps per-unit breakdown intact.
        """
        if not self.cost_by_unit:
            return None, None, "unknown"
        unit, amount = max(self.cost_by_unit.items(), key=lambda kv: kv[1])
        return amount, unit[1] or None, unit[0]

    def add_cost(self, amount: Optional[float], unit: Optional[str], source: str) -> None:
        if amount is None or source != "provider_reported":
            return
        key = (source, (unit or "").strip())
        self.cost_by_unit[key] = self.cost_by_unit.get(key, 0.0) + float(amount)


@dataclass
class _BatchDelta:
    """Accumulator for one progress window before runtime_metrics flush."""
    by_key: Dict[Tuple[str, str], runtime_metrics.UsageDelta] = field(default_factory=dict)

    def add(
        self,
        *,
        cost_source: str,
        cost_unit: Optional[str],
        success: bool,
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        total_tokens: Optional[int],
        cost_amount: Optional[float],
        duration_ms: int,
    ) -> None:
        key = (cost_source, (cost_unit or "").strip())
        delta = self.by_key.setdefault(key, runtime_metrics.UsageDelta())
        delta.calls += 1
        if success:
            delta.successes += 1
        else:
            delta.failures += 1
        delta.input_tokens = _add_optional(delta.input_tokens, input_tokens)
        delta.output_tokens = _add_optional(delta.output_tokens, output_tokens)
        delta.total_tokens = _add_optional(delta.total_tokens, total_tokens)
        delta.cost_amount = _add_optional(delta.cost_amount, cost_amount)
        delta.duration_ms_total += duration_ms

    def flush(self, *, event_type: str, provider: str, model: str) -> None:
        for (cost_source, cost_unit), delta in self.by_key.items():
            runtime_metrics.flush_delta(
                event_type=event_type,
                provider=provider,
                model=model,
                cost_source=cost_source,
                cost_unit=cost_unit or None,
                delta=delta,
            )
        self.by_key.clear()


def _add_optional(a, b):
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def _sum_optional(a: Optional[int], b: Optional[int]) -> Optional[int]:
    if a is None and b is None:
        return None
    return int(a or 0) + int(b or 0)


def _chunked(items: List[Dict[str, Any]], size: int):
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


# =====================================================================
# Module-level single-object helpers — split of the legacy
# _generate_for_object into independent build / write / publish phases
# (см. план §3, §6.1).
# =====================================================================


@dataclass
class SummaryArtifacts:
    qualified_name: str
    config_name: str
    category: str
    name: str
    publish_id: str
    profile_toon: str
    summary_payload: dict          # human_summary + machine_summary + _publish_id
    markdown: str
    search_text: str
    meta: dict                     # includes generated_at, llm_usage, _publish_id


@dataclass
class BuildAttemptResult:
    ok: bool
    qualified_name: str
    artifacts: Optional[SummaryArtifacts]
    provider: str
    model: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    cost_amount: Optional[float]
    cost_unit: Optional[str]
    cost_source: str
    duration_ms: int
    error: Optional[str] = None


@dataclass
class SingleObjectJobResult:
    ok: bool
    qualified_name: str
    action: Literal["create", "refresh"]
    archive_dir: Optional[Path]
    published: bool
    embedding_ok: bool
    error: Optional[str] = None


def _build_meta_dict(
    *, qualified_name: str, category: str, name: str, config_name: str,
    llm_model: str, llm_elapsed_seconds: float,
    llm_usage_block: Dict[str, Any], validation_warnings: List[str],
    publish_id: str,
) -> Dict[str, Any]:
    return {
        "object_qualified_name": qualified_name,
        "category": category,
        "name": name,
        "config_name": config_name,
        "contract_name": SUMMARY_CONTRACT_NAME,
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "embedding_document_builder_version": EMBEDDING_DOCUMENT_BUILDER_VERSION,
        "search_text_builder_version": SEARCH_TEXT_BUILDER_VERSION,
        "llm_model": llm_model,
        "llm_elapsed_seconds": round(float(llm_elapsed_seconds), 3),
        "llm_usage": llm_usage_block,
        "validation_warnings": list(validation_warnings),
        "generated_at": _now_iso(),
        os_storage.PUBLISH_ID_FIELD: publish_id,
    }


def build_summary_artifacts(
    driver, *, project_name: str, llm, obj: Dict[str, Any],
) -> BuildAttemptResult:
    """Single-object S1 build: evidence → profile → LLM → render artifacts.

    Pure build phase — no disk writes, no Neo4j writes. Returns a
    BuildAttemptResult with success/failure flag and usage/cost that the
    caller must flush into runtime_metrics regardless of outcome.
    """
    qn = obj["qualified_name"]
    category = obj.get("category") or ""
    name = obj.get("name") or ""
    config_name = obj.get("config_name") or ""
    provider = getattr(llm, "provider", "") or ""
    model = getattr(llm, "model", "") or ""

    started = time.perf_counter()

    def _fail(error: str) -> BuildAttemptResult:
        return BuildAttemptResult(
            ok=False, qualified_name=qn, artifacts=None,
            provider=provider, model=model,
            input_tokens=None, output_tokens=None, total_tokens=None,
            cost_amount=None, cost_unit=None, cost_source="unknown",
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=error,
        )

    evidence = q.collect_evidence(driver, project_name=project_name, qualified_name=qn)
    if not evidence:
        logger.warning("No evidence for %s; skipping", qn)
        return _fail("no_evidence")

    profile = build_profile(evidence, size_policy=settings.object_summary_profile_size_policy)
    profile_toon = encode_profile(profile)

    try:
        llm_result = llm.generate(profile_toon, profile_format="toon")
    except Exception as exc:
        logger.warning("LLM call failed for %s: %s", qn, exc)
        return _fail(f"llm_error: {exc}")

    publish_id = uuid.uuid4().hex
    summary_payload = dict(llm_result.summary.payload)
    summary_payload[os_storage.PUBLISH_ID_FIELD] = publish_id

    markdown = render_markdown(llm_result.summary.payload)
    search_text = build_search_text(llm_result.summary.payload)

    llm_usage_block = {
        "input_tokens": llm_result.usage.prompt_tokens,
        "output_tokens": llm_result.usage.completion_tokens,
        "total_tokens": llm_result.usage.total_tokens,
        "cost_amount": llm_result.cost_amount,
        "cost_unit": llm_result.cost_unit,
        "cost_source": llm_result.cost_source,
    }
    meta = _build_meta_dict(
        qualified_name=qn, category=category, name=name, config_name=config_name,
        llm_model=llm_result.model,
        llm_elapsed_seconds=llm_result.elapsed_seconds,
        llm_usage_block=llm_usage_block,
        validation_warnings=list(llm_result.summary.warnings),
        publish_id=publish_id,
    )

    artifacts = SummaryArtifacts(
        qualified_name=qn, config_name=config_name, category=category, name=name,
        publish_id=publish_id,
        profile_toon=profile_toon,
        summary_payload=summary_payload,
        markdown=markdown,
        search_text=search_text,
        meta=meta,
    )

    return BuildAttemptResult(
        ok=True, qualified_name=qn, artifacts=artifacts,
        provider=provider, model=llm_result.model or model,
        input_tokens=llm_result.usage.prompt_tokens or None,
        output_tokens=llm_result.usage.completion_tokens or None,
        total_tokens=llm_result.usage.total_tokens or None,
        cost_amount=llm_result.cost_amount,
        cost_unit=llm_result.cost_unit,
        cost_source=llm_result.cost_source,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def write_summary_artifacts(artifacts: SummaryArtifacts) -> Path:
    """Write the four artifact files in publish order.

    Order: profile.toon → summary.md → summary.json → meta.json LAST.
    `meta.json` is the file-level publish marker (cross-checked with
    `summary.json._publish_id` by S0 Phase A.4 on the next start).
    Both `summary.json` and `meta.json` carry the same `_publish_id`.

    Returns the path to summary.json (used by callers as Neo4j
    `object_summary_path` value).
    """
    profile_p = os_storage.profile_path(artifacts.config_name, artifacts.category, artifacts.name)
    summary_md_p = os_storage.summary_md_path(artifacts.config_name, artifacts.category, artifacts.name)
    summary_json_p = os_storage.summary_json_path(artifacts.config_name, artifacts.category, artifacts.name)
    meta_p = os_storage.meta_path(artifacts.config_name, artifacts.category, artifacts.name)

    os_storage.atomic_write_text(profile_p, artifacts.profile_toon)
    os_storage.atomic_write_text(summary_md_p, artifacts.markdown)
    os_storage.atomic_write_json(summary_json_p, artifacts.summary_payload)
    os_storage.atomic_write_json(meta_p, artifacts.meta)
    return summary_json_p


def _detect_embedding_provider(embedding_service) -> str:
    return runtime_metrics.detect_provider_from_api_base(
        getattr(embedding_service, "api_base", None),
        fallback="unknown",
    )


def _write_embedding_usage_to_meta(
    *, summary_json_path: Path, model: str,
    input_tokens: Optional[int], cost_amount: Optional[float],
    cost_unit: Optional[str], cost_source: str, elapsed_seconds: float,
) -> None:
    meta_p = summary_json_path.with_name(os_storage.META_FILE)
    try:
        meta = os_storage.read_json(meta_p) if meta_p.exists() else None
        if not isinstance(meta, dict):
            logger.warning("embedding_usage skipped: meta.json missing/unreadable at %s", meta_p)
            return
        meta = dict(meta)
        meta["embedding_usage"] = {
            "model": model,
            "input_tokens": input_tokens,
            "cost_amount": cost_amount,
            "cost_unit": cost_unit,
            "cost_source": cost_source,
            "elapsed_seconds": round(float(elapsed_seconds), 3),
        }
        os_storage.atomic_write_json(meta_p, meta)
    except Exception as exc:
        logger.warning("Failed to write embedding_usage to %s: %s", meta_p, exc)


def embed_summary_for_object(
    driver, *, project_name: str, embedding_service, format_spec,
    chunk_chars: int, overlap: int, max_chunks: int, s2_batch: int,
    embedding_model: str, obj: Dict[str, Any], persist_to_neo4j: bool = True,
    write_usage_to_meta: bool = True,
) -> "ObjectSummaryEmbeddingResult":
    """Build embedding for one object's summary and (optionally) persist it.

    Mirrors the previous inner `_embed_for_object` body so startup S2 and
    manual single-object job share one implementation.
    """
    qn = obj["qualified_name"]
    t0 = time.perf_counter()

    def _fail() -> "ObjectSummaryEmbeddingResult":
        return ObjectSummaryEmbeddingResult(
            ok=False, qualified_name=qn, elapsed_seconds=time.perf_counter() - t0,
        )

    path = Path(obj["path"])
    payload = os_storage.read_json(path)
    if not isinstance(payload, dict):
        return _fail()
    doc = build_embedding_document(payload)
    if not doc.strip():
        return _fail()
    chunks, lengths = split_text_for_embedding(
        doc, max_chars=chunk_chars, overlap_chars=overlap, max_chunks=max_chunks,
    )
    if not chunks:
        return _fail()

    agg_input: Optional[int] = None
    agg_cost: Optional[float] = None
    agg_unit: Optional[str] = None
    cost_source = "unknown"
    try:
        vectors: List[Any] = []
        for start in range(0, len(chunks), s2_batch):
            sub = chunks[start: start + s2_batch]
            batch_res = embedding_service.get_embeddings_with_usage(
                sub, format_spec=format_spec,
            )
            vectors.extend(batch_res.embeddings)
            if batch_res.input_tokens is not None:
                agg_input = (agg_input or 0) + int(batch_res.input_tokens)
            if batch_res.cost_amount is not None:
                agg_cost = (agg_cost or 0.0) + float(batch_res.cost_amount)
                agg_unit = batch_res.cost_unit or agg_unit
                cost_source = "provider_reported"
    except Exception as e:
        from graphdb.embedding_service import (
            is_embedding_unavailable_error,
            format_embedding_error,
        )
        if is_embedding_unavailable_error(e):
            # Known external outage: no traceback. The caller (_phase_s2_embed or
            # the manual job) stops the pass with a single summary line.
            reason = format_embedding_error(e)
            return ObjectSummaryEmbeddingResult(
                ok=False, qualified_name=qn,
                elapsed_seconds=time.perf_counter() - t0,
                embedding_unavailable=True, error_message=reason,
            )
        logger.exception("Embedding call failed for %s", qn)
        return _fail()

    usable = [(v, w) for v, w in zip(vectors, lengths) if v is not None]
    if not usable:
        return _fail()
    vecs = [v for v, _ in usable]
    weights = [w for _, w in usable]
    pooled = weighted_mean_pool(vecs, weights)
    if not pooled:
        return _fail()

    if persist_to_neo4j:
        try:
            q.set_summary_embedding(
                driver, project_name=project_name, qualified_name=qn,
                embedding=list(pooled),
            )
        except Exception:
            logger.exception("Failed to persist embedding for %s", qn)
            return _fail()

    elapsed = time.perf_counter() - t0
    if write_usage_to_meta:
        _write_embedding_usage_to_meta(
            summary_json_path=path,
            model=embedding_model,
            input_tokens=agg_input,
            cost_amount=agg_cost,
            cost_unit=agg_unit,
            cost_source=cost_source,
            elapsed_seconds=elapsed,
        )

    return ObjectSummaryEmbeddingResult(
        ok=True,
        qualified_name=qn,
        input_tokens=agg_input,
        cost_amount=agg_cost,
        cost_unit=agg_unit,
        cost_source=cost_source,
        elapsed_seconds=elapsed,
    )


class ObjectSummaryIndexer:
    def __init__(self, driver, *, embedding_availability=None) -> None:
        self.driver = driver
        self.project_name = settings.project_name
        self.embedding_service = None  # lazy
        # Optional startup EmbeddingAvailability. available=False skips the
        # shared fingerprint check and S2 (S0/S1 still run).
        self._embedding_availability = embedding_availability

    async def start(self) -> None:
        if not settings.object_summary_enabled:
            logger.info("Object summary disabled; skipping pipeline")
            return

        # Touch run_id once so a single line appears at the top of the log
        # before any S0/S1/S2 chatter.
        get_run_id()

        from graphdb.embedding_service import (
            is_embedding_unavailable_error,
            format_embedding_error,
        )

        # Startup availability gate: a known-unavailable endpoint skips the
        # shared fingerprint check and S2 for this pass; S0/S1 still run.
        avail = self._embedding_availability
        startup_unavailable = bool(
            avail is not None and avail.enabled and not avail.available
        )
        embedding_unavailable = startup_unavailable

        # Invalidate stale embeddings under the shared project fingerprint
        # before any S1/S2 work. The helper is a no-op when nothing changed
        # and when called twice in a row (VectorIndexer may have already run).
        if startup_unavailable:
            logger.warning(
                "ObjectSummaryIndexer: embedding unavailable at startup (%s); "
                "shared fingerprint check and S2 skipped this pass",
                avail.reason,
            )
        else:
            try:
                from graphdb.embedding_fingerprint import ensure_project_embedding_fingerprint
                from graphdb.embedding_service import get_embedding_service

                svc = get_embedding_service()
                if svc is not None:
                    await ensure_project_embedding_fingerprint(self.driver, svc)
                else:
                    logger.warning(
                        "ObjectSummaryIndexer: embedding service unavailable; "
                        "shared fingerprint check skipped"
                    )
            except Exception as e:
                if is_embedding_unavailable_error(e):
                    logger.warning(
                        "ObjectSummaryIndexer: embedding endpoint unavailable during "
                        "fingerprint check (%s); S2 will be skipped this pass",
                        format_embedding_error(e),
                    )
                    embedding_unavailable = True
                else:
                    logger.exception("Object summary fingerprint check failed")

        try:
            await self._phase_s0_reconcile()
        except Exception:
            logger.exception("Object summary S0 reconcile failed")

        if (settings.object_summary_generation_mode or "auto").lower() == "auto":
            try:
                await self._phase_s1_generate()
            except Exception:
                logger.exception("Object summary S1 generation failed")
        else:
            logger.info("Object summary generation_mode=manual; skipping S1")

        if embedding_unavailable:
            _os_set_degraded(
                f"embedding unavailable: "
                f"{avail.reason if startup_unavailable else 'endpoint error during fingerprint'}"
            )
            logger.info(
                "ObjectSummaryIndexer: S2 skipped this pass (embedding unavailable)"
            )
            return

        try:
            await self._phase_s2_embed()
        except Exception:
            logger.exception("Object summary S2 embedding failed")

    # ------------------------------------------------------------------
    # S0
    # ------------------------------------------------------------------

    async def _phase_s0_bootstrap_from_disk(self) -> None:
        """Re-attach orphan summary files on disk to MetadataObject rows.

        Conditional publish: only writes Neo4j when `object_summary_path` is
        empty. Does NOT touch `meta.json`; derived-version repair stays with
        Phase B of the existing reconcile so an already-published row with
        stale `search_text_builder_version` keeps getting fixed.
        """
        scanned = 0
        published = 0
        skipped_existing = 0
        skipped_invalid = 0
        skipped_missing_object = 0
        skipped_stale = 0
        skipped_publish_id_mismatch = 0

        for cfg_name, category, obj_name, dir_path in os_storage.iter_summary_dirs_on_disk():
            scanned += 1
            summary_p = dir_path / os_storage.SUMMARY_JSON_FILE
            meta_p = dir_path / os_storage.META_FILE

            summary = os_storage.read_json(summary_p)
            if not isinstance(summary, dict):
                skipped_invalid += 1
                continue
            meta = os_storage.read_json(meta_p)
            if not isinstance(meta, dict):
                skipped_invalid += 1
                continue

            qn = meta.get("object_qualified_name")
            if not isinstance(qn, str) or not qn.strip():
                skipped_invalid += 1
                continue

            src_summary = _int(meta.get("summary_schema_version"))
            src_profile = _int(meta.get("profile_schema_version"))
            if src_summary is None or src_summary < SUMMARY_SCHEMA_VERSION:
                skipped_stale += 1
                continue
            if src_profile is None:
                skipped_stale += 1
                continue
            if (
                src_profile < PROFILE_SCHEMA_VERSION
                and settings.object_summary_regenerate_on_profile_version_change
            ):
                skipped_stale += 1
                continue

            s_pid = summary.get(os_storage.PUBLISH_ID_FIELD)
            m_pid = meta.get(os_storage.PUBLISH_ID_FIELD)
            if s_pid != m_pid:
                skipped_publish_id_mismatch += 1
                continue

            text = build_search_text(summary)
            result = q.publish_summary_from_disk_if_missing(
                self.driver,
                project_name=self.project_name,
                qualified_name=qn,
                path=str(summary_p),
                search_text=text,
            )
            if result == "published":
                published += 1
            elif result == "already_published":
                skipped_existing += 1
            else:
                skipped_missing_object += 1

        logger.info(
            "ObjectSummaryIndexer: S0 disk bootstrap done; "
            "scanned=%d, published=%d, skipped_existing=%d, "
            "skipped_invalid=%d, skipped_missing_object=%d, "
            "skipped_stale=%d, skipped_publish_id_mismatch=%d",
            scanned, published, skipped_existing, skipped_invalid,
            skipped_missing_object, skipped_stale, skipped_publish_id_mismatch,
        )

    async def _phase_s0_reconcile(self) -> None:
        await self._phase_s0_bootstrap_from_disk()
        logger.info("ObjectSummaryIndexer: S0 reconcile started")
        cleared = 0
        rebuilt_search = 0
        marked_embedding_stale = 0
        batch_size = int(settings.object_summary_reconcile_batch_size or 500)
        for row in q.list_objects_with_summary_path(
            self.driver, project_name=self.project_name, batch_size=batch_size,
        ):
            qn = row["qualified_name"]
            path = Path(row["path"])
            # Phase A.1: summary.json missing
            if not os_storage.summary_exists(path):
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                )
                cleared += 1
                continue
            # Phase A.2: meta.json missing/unparseable
            meta_p = path.with_name(os_storage.META_FILE)
            meta = os_storage.read_json(meta_p) if meta_p.exists() else None
            if not isinstance(meta, dict):
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                )
                cleared += 1
                continue
            # Phase A.3: source versions stale
            src_summary = _int(meta.get("summary_schema_version"))
            src_profile = _int(meta.get("profile_schema_version"))
            if src_summary is None or src_profile is None:
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                )
                cleared += 1
                continue
            if src_summary < SUMMARY_SCHEMA_VERSION:
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                )
                cleared += 1
                continue
            if (
                src_profile < PROFILE_SCHEMA_VERSION
                and settings.object_summary_regenerate_on_profile_version_change
            ):
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                )
                cleared += 1
                continue

            # Phase A.4: cross-check `_publish_id` between summary.json and
            # meta.json. Mismatch means an interrupted publish left a mixed
            # version on disk — clear all and let S1 (auto) or the user
            # (manual) regenerate. Summaries written before publish_id was
            # introduced have (None, None) and pass through.
            summary_payload = os_storage.read_json(path)
            summary_publish_id = (
                summary_payload.get(os_storage.PUBLISH_ID_FIELD)
                if isinstance(summary_payload, dict) else None
            )
            meta_publish_id = meta.get(os_storage.PUBLISH_ID_FIELD)
            if summary_publish_id != meta_publish_id:
                logger.warning(
                    "S0 Phase A.4: _publish_id mismatch for %s "
                    "(summary=%r, meta=%r); clearing path",
                    qn, summary_publish_id, meta_publish_id,
                )
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                )
                cleared += 1
                continue

            # Phase B: derived versions — accumulating repair
            derived_embedding = _int(meta.get("embedding_document_builder_version"))
            derived_search = _int(meta.get("search_text_builder_version"))
            actions_meta = dict(meta)

            if derived_embedding is None or derived_embedding < EMBEDDING_DOCUMENT_BUILDER_VERSION:
                q.clear_summary_fields(
                    self.driver, project_name=self.project_name, qualified_name=qn,
                    clear_path=False, clear_embedding=True, clear_search_text=False,
                )
                actions_meta["embedding_document_builder_version"] = EMBEDDING_DOCUMENT_BUILDER_VERSION
                marked_embedding_stale += 1

            if derived_search is None or derived_search < SEARCH_TEXT_BUILDER_VERSION:
                # rebuild search_text locally without an LLM call
                summary_json_path = path
                payload = os_storage.read_json(summary_json_path)
                if isinstance(payload, dict):
                    new_text = build_search_text(payload)
                    q.set_summary_search_text(
                        self.driver, project_name=self.project_name, qualified_name=qn,
                        search_text=new_text,
                    )
                    actions_meta["search_text_builder_version"] = SEARCH_TEXT_BUILDER_VERSION
                    rebuilt_search += 1

            if actions_meta != meta:
                os_storage.atomic_write_json(meta_p, actions_meta)

            # Restore search_text in Neo4j when meta is fine but field is empty
            if derived_search == SEARCH_TEXT_BUILDER_VERSION and not (row.get("search_text") or "").strip():
                payload = os_storage.read_json(path)
                if isinstance(payload, dict):
                    q.set_summary_search_text(
                        self.driver, project_name=self.project_name, qualified_name=qn,
                        search_text=build_search_text(payload),
                    )

        logger.info(
            "ObjectSummaryIndexer: S0 done; cleared=%d, search_text rebuilt=%d, embedding marked stale=%d",
            cleared, rebuilt_search, marked_embedding_stale,
        )

    # ------------------------------------------------------------------
    # S1
    # ------------------------------------------------------------------

    async def _phase_s1_generate(self) -> None:
        from object_summary.llm import get_object_summary_llm

        llm = get_object_summary_llm()
        if llm is None:
            logger.warning("ObjectSummaryLLM is not available; skipping S1")
            return

        categories = filter_supported_categories(list(settings.object_summary_categories or []))
        if not categories:
            logger.warning("OBJECT_SUMMARY_CATEGORIES has no supported categories; skipping S1")
            return

        batch_size = int(settings.object_summary_generation_batch_size or 10)
        max_workers = max(1, int(settings.object_summary_generation_workers or 2))
        max_attempts = max(1, int(settings.object_summary_generation_attempts))

        total_base = q.count_generation_candidates(
            self.driver, project_name=self.project_name, categories=categories,
        )
        total_ext = 0
        if settings.object_summary_generate_for_extensions:
            ext_names = list(settings.object_summary_extension_names or []) or ["*"]
            only_own = (settings.object_summary_extension_object_scope or "own").lower() != "all"
            total_ext = q.count_extension_objects(
                self.driver, project_name=self.project_name,
                extension_names=ext_names, only_own=only_own, categories=categories,
            )

        if total_base + total_ext == 0:
            logger.info("ObjectSummaryIndexer: S1 has nothing to do")
            return

        started_at = time.perf_counter()
        logger.info(
            "ObjectSummaryIndexer: S1 started: total=%d, batch_size=%d, workers=%d, attempts=%d, model=%s",
            total_base + total_ext, batch_size, max_workers, max_attempts, llm.model,
        )

        semaphore = asyncio.Semaphore(max_workers)
        base_totals = _PhaseTotals()
        ext_totals = _PhaseTotals()
        retry_totals = _PhaseTotals()
        failed_qns: List[str] = []  # excluded from subsequent SELECT to avoid an infinite loop
        failed_objects: List[Dict[str, Any]] = []  # in-memory retry pool, base + ext combined

        async def _one(obj: Dict[str, Any]) -> ObjectSummaryGenerateResult:
            async with semaphore:
                try:
                    return await asyncio.to_thread(self._generate_for_object, llm, obj)
                except Exception:
                    logger.exception("S1 worker failed for %s", obj.get("qualified_name"))
                    return ObjectSummaryGenerateResult(
                        ok=False, qualified_name=str(obj.get("qualified_name") or ""),
                    )

        # Base configuration objects (extension context is folded into every base profile
        # via `fetch_extension_context` in evidence collection).
        while True:
            candidates = q.list_generation_candidates(
                self.driver,
                project_name=self.project_name,
                categories=categories,
                limit=batch_size,
                exclude_qns=failed_qns,
            )
            if not candidates:
                break

            results = await asyncio.gather(*[_one(o) for o in candidates])
            batch_delta = _BatchDelta()
            for obj, res in zip(candidates, results):
                self._record_s1_result(res, base_totals, batch_delta)
                if not res.ok and res.qualified_name:
                    failed_qns.append(res.qualified_name)
                    failed_objects.append(obj)
            batch_delta.flush(event_type="object_summary.llm", provider=llm.provider, model=llm.model)
            cost_amount, cost_unit, cost_source = base_totals.primary_cost()
            logger.info(
                "ObjectSummaryIndexer: S1 base progress: attempt=1/%d, processed=%d/%d, created=%d, failed=%d, "
                "input_tokens=%s, output_tokens=%s, cost=%s, elapsed=%s",
                max_attempts,
                base_totals.processed, total_base, base_totals.created, base_totals.failed,
                _format_usage_tokens(base_totals.input_tokens),
                _format_usage_tokens(base_totals.output_tokens),
                _format_cost(cost_amount, cost_unit, cost_source),
                _format_elapsed(time.perf_counter() - started_at),
            )
            if len(candidates) < batch_size:
                break

        # Extension objects — separate generation when explicitly enabled.
        if settings.object_summary_generate_for_extensions:
            ext_names = list(settings.object_summary_extension_names or []) or ["*"]
            only_own = (settings.object_summary_extension_object_scope or "own").lower() != "all"
            ext_failed_qns: List[str] = []
            while True:
                ext_candidates = q.list_extension_objects(
                    self.driver,
                    project_name=self.project_name,
                    extension_names=ext_names,
                    only_own=only_own,
                    categories=categories,
                    limit=batch_size,
                    exclude_qns=ext_failed_qns,
                )
                if not ext_candidates:
                    break

                results = await asyncio.gather(*[_one(o) for o in ext_candidates])
                batch_delta = _BatchDelta()
                for obj, res in zip(ext_candidates, results):
                    self._record_s1_result(res, ext_totals, batch_delta)
                    if not res.ok and res.qualified_name:
                        ext_failed_qns.append(res.qualified_name)
                        failed_objects.append(obj)
                batch_delta.flush(event_type="object_summary.llm", provider=llm.provider, model=llm.model)
                cost_amount, cost_unit, cost_source = ext_totals.primary_cost()
                logger.info(
                    "ObjectSummaryIndexer: S1 extension progress: attempt=1/%d, processed=%d/%d, created=%d, failed=%d, "
                    "input_tokens=%s, output_tokens=%s, cost=%s, elapsed=%s",
                    max_attempts,
                    ext_totals.processed, total_ext, ext_totals.created, ext_totals.failed,
                    _format_usage_tokens(ext_totals.input_tokens),
                    _format_usage_tokens(ext_totals.output_tokens),
                    _format_cost(cost_amount, cost_unit, cost_source),
                    _format_elapsed(time.perf_counter() - started_at),
                )
                if len(ext_candidates) < batch_size:
                    break

        # Retry rounds for objects that failed on the first pass.
        # Goes only over the in-memory failed_objects list — no extra Neo4j SELECT.
        attempt = 1
        while attempt < max_attempts and failed_objects:
            attempt += 1
            round_input = failed_objects
            failed_objects = []
            round_started_at = time.perf_counter()
            logger.info(
                "ObjectSummaryIndexer: S1 retry started: attempt=%d/%d, objects=%d, batch_size=%d, workers=%d",
                attempt, max_attempts, len(round_input), batch_size, max_workers,
            )
            round_processed = 0
            round_created = 0
            round_failed = 0
            for chunk in _chunked(round_input, batch_size):
                results = await asyncio.gather(*[_one(o) for o in chunk])
                batch_delta = _BatchDelta()
                for obj, res in zip(chunk, results):
                    self._record_s1_result(res, retry_totals, batch_delta)
                    round_processed += 1
                    if res.ok:
                        round_created += 1
                    else:
                        round_failed += 1
                        failed_objects.append(obj)
                batch_delta.flush(event_type="object_summary.llm", provider=llm.provider, model=llm.model)
                cost_amount, cost_unit, cost_source = retry_totals.primary_cost()
                logger.info(
                    "ObjectSummaryIndexer: S1 retry progress: attempt=%d/%d, done=%d/%d, ok=%d, failed=%d, "
                    "input_tokens=%s, output_tokens=%s, cost=%s, elapsed=%s",
                    attempt, max_attempts, round_processed, len(round_input), round_created, round_failed,
                    _format_usage_tokens(retry_totals.input_tokens),
                    _format_usage_tokens(retry_totals.output_tokens),
                    _format_cost(cost_amount, cost_unit, cost_source),
                    _format_elapsed(time.perf_counter() - started_at),
                )
            logger.info(
                "ObjectSummaryIndexer: S1 retry done: attempt=%d/%d, retried=%d, recovered=%d, "
                "remaining_failed=%d, elapsed=%s",
                attempt, max_attempts, len(round_input), round_created, len(failed_objects),
                _format_elapsed(time.perf_counter() - round_started_at),
            )

        objects_total = total_base + total_ext
        attempts_processed = base_totals.processed + ext_totals.processed + retry_totals.processed
        ok_count = base_totals.created + ext_totals.created + retry_totals.created
        failed_attempts = base_totals.failed + ext_totals.failed + retry_totals.failed
        remaining_failed = len(failed_objects)
        input_tokens = _sum_optional(
            _sum_optional(base_totals.input_tokens, ext_totals.input_tokens),
            retry_totals.input_tokens,
        )
        output_tokens = _sum_optional(
            _sum_optional(base_totals.output_tokens, ext_totals.output_tokens),
            retry_totals.output_tokens,
        )
        combined_cost: Dict[Tuple[str, str], float] = dict(base_totals.cost_by_unit)
        for k, v in ext_totals.cost_by_unit.items():
            combined_cost[k] = combined_cost.get(k, 0.0) + v
        for k, v in retry_totals.cost_by_unit.items():
            combined_cost[k] = combined_cost.get(k, 0.0) + v
        if combined_cost:
            unit_key, amount = max(combined_cost.items(), key=lambda kv: kv[1])
            final_cost_str = _format_cost(amount, unit_key[1] or None, unit_key[0])
        else:
            final_cost_str = "unknown"
        logger.info(
            "ObjectSummaryIndexer: S1 done: objects_total=%d, attempts_processed=%d, ok=%d, "
            "failed_attempts=%d, remaining_failed=%d, "
            "input_tokens=%s, output_tokens=%s, cost=%s, elapsed=%s",
            objects_total, attempts_processed, ok_count, failed_attempts, remaining_failed,
            _format_usage_tokens(input_tokens), _format_usage_tokens(output_tokens),
            final_cost_str, _format_elapsed(time.perf_counter() - started_at),
        )

    def _record_s1_result(
        self, res: ObjectSummaryGenerateResult, totals: _PhaseTotals, delta: _BatchDelta,
    ) -> None:
        totals.processed += 1
        if res.ok:
            totals.created += 1
        else:
            totals.failed += 1
        if res.input_tokens is not None:
            totals.input_tokens = (totals.input_tokens or 0) + int(res.input_tokens)
        if res.output_tokens is not None:
            totals.output_tokens = (totals.output_tokens or 0) + int(res.output_tokens)
        totals.add_cost(res.cost_amount, res.cost_unit, res.cost_source)
        delta.add(
            cost_source=res.cost_source,
            cost_unit=res.cost_unit,
            success=res.ok,
            input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            total_tokens=res.total_tokens,
            cost_amount=res.cost_amount,
            duration_ms=int(res.elapsed_seconds * 1000),
        )

    def _generate_for_object(self, llm, obj: Dict[str, Any]) -> ObjectSummaryGenerateResult:
        """Startup S1 single-object generation.

        Thin wrapper around the module-level
        `build_summary_artifacts` / `write_summary_artifacts` /
        `publish_summary_atomic` triple. Shares the same publish order
        (`meta.json` last, both files carry `_publish_id`) with the
        manual `run_single_object_summary_job` runner.

        Startup precondition `object_summary_path IS NULL` makes the new
        write order invariant-safe: any partial file set after a crash is
        ignored on the next start because S0 doesn't see a path and S1
        will pick the object up again as a candidate.
        """
        qn = obj["qualified_name"]
        started = time.perf_counter()

        build = build_summary_artifacts(
            self.driver, project_name=self.project_name, llm=llm, obj=obj,
        )
        if not build.ok or build.artifacts is None:
            return ObjectSummaryGenerateResult(
                ok=False, qualified_name=qn,
                input_tokens=build.input_tokens,
                output_tokens=build.output_tokens,
                total_tokens=build.total_tokens,
                cost_amount=build.cost_amount,
                cost_unit=build.cost_unit,
                cost_source=build.cost_source,
                elapsed_seconds=time.perf_counter() - started,
            )

        try:
            summary_json_p = write_summary_artifacts(build.artifacts)
        except Exception:
            logger.exception("Atomic write failed for %s", qn)
            return ObjectSummaryGenerateResult(
                ok=False, qualified_name=qn,
                input_tokens=build.input_tokens,
                output_tokens=build.output_tokens,
                total_tokens=build.total_tokens,
                cost_amount=build.cost_amount,
                cost_unit=build.cost_unit,
                cost_source=build.cost_source,
                elapsed_seconds=time.perf_counter() - started,
            )

        try:
            q.publish_summary_atomic(
                self.driver, project_name=self.project_name, qualified_name=qn,
                path=str(summary_json_p), search_text=build.artifacts.search_text,
                clear_embedding=True,
            )
        except Exception:
            logger.exception("Neo4j publish failed for %s", qn)
            return ObjectSummaryGenerateResult(
                ok=False, qualified_name=qn,
                input_tokens=build.input_tokens,
                output_tokens=build.output_tokens,
                total_tokens=build.total_tokens,
                cost_amount=build.cost_amount,
                cost_unit=build.cost_unit,
                cost_source=build.cost_source,
                elapsed_seconds=time.perf_counter() - started,
            )

        return ObjectSummaryGenerateResult(
            ok=True,
            qualified_name=qn,
            input_tokens=build.input_tokens,
            output_tokens=build.output_tokens,
            total_tokens=build.total_tokens,
            cost_amount=build.cost_amount,
            cost_unit=build.cost_unit,
            cost_source=build.cost_source,
            elapsed_seconds=time.perf_counter() - started,
        )

    # ------------------------------------------------------------------
    # S2
    # ------------------------------------------------------------------

    async def _phase_s2_embed(self) -> None:
        candidates = q.list_objects_needing_summary_embedding(
            self.driver, project_name=self.project_name, limit=10_000,
        )
        if not candidates:
            logger.info("ObjectSummaryIndexer: S2 has nothing to do")
            return

        # Lazy-init the shared EmbeddingService
        from graphdb.embedding_service import get_embedding_service
        from graphdb.embedding_text_format import build_embedding_format_spec

        self.embedding_service = get_embedding_service()
        if self.embedding_service is None:
            logger.warning("EmbeddingService unavailable; S2 skipped (%d candidates)", len(candidates))
            return

        format_spec = build_embedding_format_spec(
            profile=self.embedding_service.text_format_profile,
            transport=self.embedding_service.transport,
            side="document",
            purpose="description",
            description_instruction=settings.embedding_description_query_instruction or "",
        )

        max_chunks = int(settings.embedding_max_chunks_per_object or 12)
        overlap = int(settings.embedding_chunk_overlap_chars or 200)
        chunk_chars = int(getattr(self.embedding_service, "effective_chunk_chars", 4000) or 4000)
        # S2-specific batch size for the embedding API and parallelism between
        # objects. Keeps object_summary independent from EMBEDDING_BATCH_SIZE,
        # which other indexer phases tune for their own workloads.
        s2_batch = max(1, int(settings.object_summary_embedding_batch_size or 8))
        s2_workers = max(1, int(settings.object_summary_embedding_workers or 4))
        sem = asyncio.Semaphore(s2_workers)

        total = len(candidates)
        started_at = time.perf_counter()
        embedding_model = getattr(self.embedding_service, "model", "")
        embedding_provider = _detect_embedding_provider(self.embedding_service)
        logger.info(
            "ObjectSummaryIndexer: S2 started: total=%d, workers=%d, embedding_batch_size=%d, model=%s",
            total, s2_workers, s2_batch, embedding_model,
        )

        def _embed_for_object(obj: Dict[str, Any]) -> ObjectSummaryEmbeddingResult:
            return embed_summary_for_object(
                self.driver, project_name=self.project_name,
                embedding_service=self.embedding_service, format_spec=format_spec,
                chunk_chars=chunk_chars, overlap=overlap, max_chunks=max_chunks,
                s2_batch=s2_batch, embedding_model=embedding_model, obj=obj,
            )

        async def _one(obj: Dict[str, Any]) -> ObjectSummaryEmbeddingResult:
            async with sem:
                return await asyncio.to_thread(_embed_for_object, obj)

        totals = _PhaseTotals()
        pending_delta = _BatchDelta()
        last_logged = 0

        tasks = [asyncio.create_task(_one(o)) for o in candidates]
        embedding_unavailable = False
        try:
            for fut in asyncio.as_completed(tasks):
                res = await fut
                self._record_s2_result(res, totals, pending_delta)
                if getattr(res, "embedding_unavailable", False):
                    # First known outage: stop the whole pass with one line
                    # instead of a traceback per object. The finally cancels the
                    # remaining tasks; the rest are deferred to the next pass.
                    embedding_unavailable = True
                    logger.warning(
                        "ObjectSummaryIndexer: S2 stopped — embedding endpoint "
                        "unavailable (%s); processed=%d/%d, remaining deferred",
                        res.error_message or "", totals.processed, total,
                    )
                    _os_set_degraded(
                        f"embedding unavailable: {res.error_message or 'endpoint error'}"
                    )
                    break
                processed = totals.processed
                if processed - last_logged >= 50:
                    pending_delta.flush(
                        event_type="object_summary.embedding",
                        provider=embedding_provider, model=embedding_model,
                    )
                    cost_amount, cost_unit, cost_source = totals.primary_cost()
                    logger.info(
                        "ObjectSummaryIndexer: S2 progress: processed=%d/%d, embedded=%d, failed=%d, "
                        "input_tokens=%s, cost=%s, elapsed=%s",
                        processed, total, totals.created, totals.failed,
                        _format_usage_tokens(totals.input_tokens),
                        _format_cost(cost_amount, cost_unit, cost_source),
                        _format_elapsed(time.perf_counter() - started_at),
                    )
                    last_logged = processed
        finally:
            pending = [t for t in tasks if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        pending_delta.flush(
            event_type="object_summary.embedding",
            provider=embedding_provider, model=embedding_model,
        )
        cost_amount, cost_unit, cost_source = totals.primary_cost()
        logger.info(
            "ObjectSummaryIndexer: S2 done: processed=%d/%d, embedded=%d, failed=%d, "
            "input_tokens=%s, cost=%s, elapsed=%s",
            totals.processed, total, totals.created, totals.failed,
            _format_usage_tokens(totals.input_tokens),
            _format_cost(cost_amount, cost_unit, cost_source),
            _format_elapsed(time.perf_counter() - started_at),
        )
        if not embedding_unavailable:
            # Clean pass (endpoint was reachable throughout): recovery clears
            # any prior degraded reason.
            _os_clear_degraded()

    def _record_s2_result(
        self, res: ObjectSummaryEmbeddingResult, totals: _PhaseTotals, delta: _BatchDelta,
    ) -> None:
        totals.processed += 1
        if res.ok:
            totals.created += 1
        else:
            totals.failed += 1
        if res.input_tokens is not None:
            totals.input_tokens = (totals.input_tokens or 0) + int(res.input_tokens)
        totals.add_cost(res.cost_amount, res.cost_unit, res.cost_source)
        delta.add(
            cost_source=res.cost_source,
            cost_unit=res.cost_unit,
            success=res.ok,
            input_tokens=res.input_tokens,
            output_tokens=None,
            total_tokens=res.input_tokens,
            cost_amount=res.cost_amount,
            duration_ms=int(res.elapsed_seconds * 1000),
        )

    # `_detect_embedding_provider` / `_write_embedding_usage_to_meta` removed:
    # both live as module-level helpers (`_detect_embedding_provider`,
    # `_write_embedding_usage_to_meta`) so startup S2 and the manual single
    # object job share one implementation.


# =====================================================================
# Manual single-object job entrypoint (web console "Создать"/"Обновить").
# Owns the full domain order: archive → loop attempts (build + flush
# metrics) → write → atomic publish → embed. Lifecycle (lock, thread,
# status) lives in object_summary.manual_jobs; this entrypoint only
# implements the domain order.
# =====================================================================


def _flush_llm_attempt_metrics(build: BuildAttemptResult) -> None:
    delta = _BatchDelta()
    delta.add(
        cost_source=build.cost_source,
        cost_unit=build.cost_unit,
        success=build.ok,
        input_tokens=build.input_tokens,
        output_tokens=build.output_tokens,
        total_tokens=build.total_tokens,
        cost_amount=build.cost_amount,
        duration_ms=build.duration_ms,
    )
    delta.flush(event_type="object_summary.llm", provider=build.provider, model=build.model)


def _flush_embedding_attempt_metrics(
    embed: "ObjectSummaryEmbeddingResult", *, provider: str, model: str,
) -> None:
    delta = _BatchDelta()
    delta.add(
        cost_source=embed.cost_source,
        cost_unit=embed.cost_unit,
        success=embed.ok,
        input_tokens=embed.input_tokens,
        output_tokens=None,
        total_tokens=embed.input_tokens,
        cost_amount=embed.cost_amount,
        duration_ms=int(embed.elapsed_seconds * 1000),
    )
    delta.flush(event_type="object_summary.embedding", provider=provider, model=model)


def run_single_object_summary_job(
    driver, *,
    project_name: str,
    qualified_name: str,
    action: Literal["create", "refresh"],
    progress_cb: Optional[Callable[[str], None]] = None,
) -> SingleObjectJobResult:
    """Manual single-object summary job entrypoint.

    Owns the contract described in plan §6.1: archive (refresh only),
    loop OBJECT_SUMMARY_GENERATION_ATTEMPTS, write files
    (meta.json last), atomic publish, embedding. Restore-from-archive
    safety net on write/publish failure (§6.1.2). All runtime_metrics
    flushes happen here, not in `manual_jobs.py`.
    """
    from object_summary.llm import get_object_summary_llm

    def _emit(stage: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(stage)
            except Exception:
                logger.debug("progress_cb raised", exc_info=True)

    obj_row = q.get_generation_object_by_qn(
        driver, project_name=project_name, qualified_name=qualified_name,
    )
    if obj_row is None:
        return SingleObjectJobResult(
            ok=False, qualified_name=qualified_name, action=action,
            archive_dir=None, published=False, embedding_ok=False,
            error="object_not_found",
        )

    config_name = obj_row.get("config_name") or ""
    category = obj_row.get("category") or ""
    name = obj_row.get("name") or ""

    llm = get_object_summary_llm()
    if llm is None:
        return SingleObjectJobResult(
            ok=False, qualified_name=qualified_name, action=action,
            archive_dir=None, published=False, embedding_ok=False,
            error="llm_unavailable",
        )

    target_dir = os_storage.object_dir(config_name, category, name)
    archive_dir: Optional[Path] = None
    if action == "refresh":
        _emit("archive")
        try:
            archive_dir = os_storage.archive_summary_files(config_name, category, name)
        except Exception as exc:
            logger.exception("archive_summary_files failed for %s", qualified_name)
            return SingleObjectJobResult(
                ok=False, qualified_name=qualified_name, action=action,
                archive_dir=None, published=False, embedding_ok=False,
                error=f"archive_failed: {exc}",
            )

    def _restore_and_force_clear_on_failure() -> None:
        if archive_dir is None:
            return
        restored = 0
        try:
            restored = os_storage.restore_summary_files_from_archive(archive_dir, target_dir)
        except Exception:
            logger.exception("restore_summary_files_from_archive raised for %s", qualified_name)
        if restored < 4:
            try:
                q.clear_summary_fields(
                    driver, project_name=project_name, qualified_name=qualified_name,
                )
            except Exception:
                logger.exception(
                    "Force-clear after partial restore failed; "
                    "Neo4j may stay OLD pointing to NEW disk content for %s",
                    qualified_name,
                )

    max_attempts = max(1, int(settings.object_summary_generation_attempts or 1))
    build: Optional[BuildAttemptResult] = None
    for attempt in range(1, max_attempts + 1):
        _emit(f"build_attempt_{attempt}")
        build = build_summary_artifacts(
            driver, project_name=project_name, llm=llm, obj=obj_row,
        )
        _flush_llm_attempt_metrics(build)
        if build.ok and build.artifacts is not None:
            break

    if build is None or not build.ok or build.artifacts is None:
        # Class A failure: no write/publish was attempted. Disk and Neo4j
        # untouched, OLD search/UI keep working.
        return SingleObjectJobResult(
            ok=False, qualified_name=qualified_name, action=action,
            archive_dir=archive_dir, published=False, embedding_ok=False,
            error=(build.error if build is not None else "no_build_result")
                  or "build_failed",
        )

    _emit("write")
    try:
        summary_json_p = write_summary_artifacts(build.artifacts)
    except Exception as exc:
        logger.exception("write_summary_artifacts failed for %s", qualified_name)
        _restore_and_force_clear_on_failure()
        return SingleObjectJobResult(
            ok=False, qualified_name=qualified_name, action=action,
            archive_dir=archive_dir, published=False, embedding_ok=False,
            error=f"write_failed: {exc}",
        )

    _emit("publish")
    try:
        q.publish_summary_atomic(
            driver, project_name=project_name, qualified_name=qualified_name,
            path=str(summary_json_p), search_text=build.artifacts.search_text,
            clear_embedding=True,
        )
    except Exception as exc:
        logger.exception("publish_summary_atomic failed for %s", qualified_name)
        _restore_and_force_clear_on_failure()
        return SingleObjectJobResult(
            ok=False, qualified_name=qualified_name, action=action,
            archive_dir=archive_dir, published=False, embedding_ok=False,
            error=f"publish_failed: {exc}",
        )

    # Embedding step. Failure here is non-fatal for the job: path and
    # search_text are already published, S2 startup will rebuild embedding
    # on the next run if it stays NULL.
    _emit("embed")
    embedding_ok = False
    try:
        from graphdb.embedding_service import get_embedding_service
        from graphdb.embedding_text_format import build_embedding_format_spec
        embedding_service = get_embedding_service()
        if embedding_service is None:
            logger.warning(
                "EmbeddingService unavailable; manual job for %s leaves embedding NULL",
                qualified_name,
            )
        else:
            format_spec = build_embedding_format_spec(
                profile=embedding_service.text_format_profile,
                transport=embedding_service.transport,
                side="document",
                purpose="description",
                description_instruction=settings.embedding_description_query_instruction or "",
            )
            max_chunks = int(settings.embedding_max_chunks_per_object or 12)
            overlap = int(settings.embedding_chunk_overlap_chars or 200)
            chunk_chars = int(getattr(embedding_service, "effective_chunk_chars", 4000) or 4000)
            s2_batch = max(1, int(settings.object_summary_embedding_batch_size or 8))
            embedding_model = getattr(embedding_service, "model", "")
            embedding_provider = _detect_embedding_provider(embedding_service)

            embed_res = embed_summary_for_object(
                driver, project_name=project_name,
                embedding_service=embedding_service, format_spec=format_spec,
                chunk_chars=chunk_chars, overlap=overlap, max_chunks=max_chunks,
                s2_batch=s2_batch, embedding_model=embedding_model,
                obj={"qualified_name": qualified_name, "path": str(summary_json_p)},
            )
            _flush_embedding_attempt_metrics(
                embed_res, provider=embedding_provider, model=embedding_model,
            )
            embedding_ok = embed_res.ok
    except Exception:
        logger.exception("Manual embedding step failed for %s", qualified_name)

    return SingleObjectJobResult(
        ok=True, qualified_name=qualified_name, action=action,
        archive_dir=archive_dir, published=True, embedding_ok=embedding_ok,
    )
