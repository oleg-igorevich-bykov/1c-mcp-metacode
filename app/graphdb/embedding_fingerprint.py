"""Shared project embedding fingerprint helper.

Owns the project-wide invalidation contract for vector fields that depend on
the embedding model: `Routine.doc_description_embedding`,
`MetadataObject.description_embedding` and
`MetadataObject.object_summary_embedding`.

Important: invalidation runs **regardless of current feature flags**. The
toggle cycle `enable → build → disable → change EMBEDDING_MODEL → enable`
must not leave a stale `object_summary_embedding` from the old model. Feature
flags decide which indexer to run and which index to create — they do not
decide which old vectors to clear.

BSL `code_embedding` is **not** managed here. It has its own fingerprint
protocol via `bsl_code_units_version` in the BSL pipeline.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


def _cosine_sim(a, b) -> Optional[float]:
    try:
        if a is None or b is None or len(a) != len(b):
            return None
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b):
            try:
                xv = float(x)
                yv = float(y)
            except (TypeError, ValueError):
                xv = 0.0
                yv = 0.0
            dot += xv * yv
            na += xv * xv
            nb += yv * yv
        if na <= 0.0 or nb <= 0.0:
            return None
        return dot / ((na ** 0.5) * (nb ** 0.5))
    except Exception:  # noqa: BLE001 - defensive
        return None


def _clear_dependent_embeddings(session, project_name: str) -> None:
    """Clear all vector fields managed by the shared embedding fingerprint.

    Runs unconditionally — feature flags must not gate invalidation.
    """
    session.run(
        """
        MATCH (r:Routine)
        WHERE toLower(r.project_name) = toLower($prj)
          AND r.doc_description_embedding IS NOT NULL
        REMOVE r.doc_description_embedding
        """,
        prj=project_name,
    )
    session.run(
        """
        MATCH (m:MetadataObject)
        WHERE toLower(m.project_name) = toLower($prj)
          AND m.description_embedding IS NOT NULL
        REMOVE m.description_embedding
        """,
        prj=project_name,
    )
    session.run(
        """
        MATCH (m:MetadataObject)
        WHERE toLower(m.project_name) = toLower($prj)
          AND m.object_summary_embedding IS NOT NULL
        REMOVE m.object_summary_embedding
        """,
        prj=project_name,
    )


async def ensure_project_embedding_fingerprint(driver, embedding_service) -> bool:
    """Verify the project-level embedding fingerprint and clear stale vectors.

    Returns True if the fingerprint was found unchanged (no invalidation
    needed) or freshly initialised; returns False if the fingerprint changed
    and dependent embeddings were cleared — callers may want to recreate
    vector indexes in that case.

    `driver` is a Neo4j driver, `embedding_service` is the project-wide
    EmbeddingService (used only for `embed_for_fingerprint`).
    """
    if embedding_service is None:
        logger.warning("ensure_project_embedding_fingerprint called without embedding_service; skipping")
        return True

    new_vec = await asyncio.to_thread(
        embedding_service.embed_for_fingerprint, settings.project_name
    )
    current_fp = f"{settings.embedding_model}|{settings.embedding_api_base}"
    cos_threshold = float(getattr(settings, "embedding_fingerprint_cosine_threshold", 0.999) or 0.999)

    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            "MATCH (p:Project {name: $name}) "
            "RETURN p.project_name_embedding AS prev, p.embedding_model_fingerprint AS prev_fp",
            name=settings.project_name,
        ).single()
        prev = rec["prev"] if rec else None
        prev_fp = rec["prev_fp"] if rec else None

        if prev is None and prev_fp is None:
            session.run(
                "MATCH (p:Project {name: $name}) "
                "SET p.project_name_embedding = $vec, p.embedding_model_fingerprint = $fp",
                name=settings.project_name, vec=new_vec, fp=current_fp,
            )
            logger.info(
                "Stored initial project embedding fingerprint for '%s' (model/provider: %s)",
                settings.project_name, current_fp,
            )
            return True

        need_reindex = False
        reason = "no change"
        if prev_fp != current_fp:
            need_reindex = True
            reason = f"model/provider changed from {prev_fp!r} to {current_fp!r}"
        else:
            sim = _cosine_sim(prev, new_vec)
            if sim is None or sim < cos_threshold:
                need_reindex = True
                reason = f"project embedding cosine below threshold (sim={sim} < {cos_threshold})"

        if not need_reindex:
            logger.info(
                "Project fingerprint unchanged for '%s' (model/provider: %s)",
                settings.project_name, current_fp,
            )
            return True

        logger.info(
            "Project fingerprint changed (%s) → clearing dependent embeddings for '%s'",
            reason, settings.project_name,
        )
        _clear_dependent_embeddings(session, settings.project_name)
        session.run(
            "MATCH (p:Project {name: $name}) "
            "SET p.project_name_embedding = $vec, p.embedding_model_fingerprint = $fp",
            name=settings.project_name, vec=new_vec, fp=current_fp,
        )
        return False
