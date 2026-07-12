"""
MetadataObject description indexer for generating and storing embeddings.
"""
from __future__ import annotations

import logging
import asyncio
import time
from typing import List, Dict, Any, Optional, Tuple
from config import settings
from graphdb import embedding_usage_metrics as embedding_metrics
from graphdb.embedding_service import (
    EmbeddingService,
    is_embedding_unavailable_error,
    format_embedding_error,
)
from graphdb.embedding_chunks import split_text_for_embedding, weighted_mean_pool
from graphdb.embedding_text_format import build_embedding_format_spec

logger = logging.getLogger(__name__)


class MetadataObjectDescriptionIndexer:
    """Indexes MetadataObject descriptions using hash-based distribution across workers"""

    def __init__(self, driver, worker_id: int, total_workers: int, embedding_service: EmbeddingService, outage_signal=None):
        """
        Initialize indexer for a specific worker.

        Args:
            driver: Neo4j driver instance
            worker_id: ID of this worker (0-based)
            total_workers: Total number of workers
            embedding_service: Service for generating embeddings
            outage_signal: shared per-pass signal set on a known embedding outage
                so the pass stops and the coordinator keeps the degraded reason.
        """
        self.driver = driver
        self.worker_id = worker_id
        self.total_workers = total_workers
        self.embedding_service = embedding_service
        self._outage = outage_signal
        self.batch_size = settings.embedding_batch_size
        self.save_batch_size = settings.embedding_save_batch_size
        self.rate_limit_delay = settings.embedding_rate_limit_delay
        self.database = settings.neo4j_database

        self.total_processed = 0
        self.total_failed = 0
        self.total_to_index = 0  # Will be set in run()
        self.is_running = False

        # Live usage/cost counters for progress logs (not runtime_metrics).
        self.embedding_usage = embedding_metrics.EmbeddingUsageStats()
        self._last_batch_usage = embedding_metrics.EmbeddingUsageStats()
        self._started_at = time.perf_counter()

        logger.info(
            f"MetadataObjectDescriptionIndexer initialized: worker_id={worker_id}, "
            f"total_workers={total_workers}, batch_size={self.batch_size}"
        )

    def get_total_count(self) -> int:
        """Get total count of metadata objects to index for this worker"""
        query = """
        MATCH (m:MetadataObject)
        WHERE m.description_embedding IS NULL
          AND (m.name IS NOT NULL OR m.`Синоним` IS NOT NULL
               OR m.`Комментарий` IS NOT NULL OR m.`Справка` IS NOT NULL
               OR m.`Пояснение` IS NOT NULL)
          AND toLower(m.project_name) = toLower($project_name)
          AND id(m) % $total_workers = $worker_id
        RETURN count(m) as total
        """

        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(
                    query,
                    project_name=settings.project_name,
                    total_workers=self.total_workers,
                    worker_id=self.worker_id
                )
                record = result.single()
                return record['total'] if record else 0
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Failed to get total count: {e}")
            return 0

    async def run(self) -> None:
        """Main indexing loop - processes metadata objects until no more remain"""
        self.is_running = True
        last_node_id = 0
        self._started_at = time.perf_counter()

        try:
            # Get total count for this worker
            self.total_to_index = self.get_total_count()
            logger.info(
                f"Worker {self.worker_id}: Starting MetadataObject description embedding indexing. "
                f"Total metadata objects to process: {self.total_to_index}"
            )

            while self.is_running:
                # Stop early if another worker already hit an embedding outage.
                if self._outage is not None and self._outage.hit:
                    break

                # Fetch batch of metadata objects without embeddings
                metadata_objects = self.fetch_batch(last_node_id)

                if not metadata_objects:
                    logger.info(f"Worker {self.worker_id}: No more metadata object descriptions to process. Indexing complete.")
                    break

                # Process batch
                success = await self.process_batch(metadata_objects)

                if success:
                    last_node_id = metadata_objects[-1]['node_id']
                    self.total_processed += len(metadata_objects)

                    # Calculate progress percentage
                    progress_pct = (self.total_processed / self.total_to_index * 100) if self.total_to_index > 0 else 0

                    batch = self._last_batch_usage
                    cum = self.embedding_usage
                    elapsed = embedding_metrics.elapsed_ms(self._started_at) / 1000.0
                    logger.info(
                        f"Worker {self.worker_id}: Processed batch of {len(metadata_objects)} metadata object descriptions. "
                        f"Progress: {self.total_processed}/{self.total_to_index} ({progress_pct:.1f}%), "
                        f"failed: {self.total_failed}, "
                        f"batch_embedding_api_calls={batch.embedding_api_calls}, "
                        f"embedding_api_calls={cum.embedding_api_calls}, "
                        f"batch_input_tokens={embedding_metrics.format_usage_tokens(batch.input_tokens)}, "
                        f"input_tokens={embedding_metrics.format_usage_tokens(cum.input_tokens)}, "
                        f"batch_total_tokens={embedding_metrics.format_usage_tokens(batch.total_tokens)}, "
                        f"total_tokens={embedding_metrics.format_usage_tokens(cum.total_tokens)}, "
                        f"batch_cost={embedding_metrics.format_cost(*batch.primary_cost())}, "
                        f"cost={embedding_metrics.format_cost(*cum.primary_cost())}, "
                        f"elapsed={elapsed:.1f}s"
                    )
                else:
                    # Embedding outage: stop this pass (deferred to the next one).
                    if self._outage is not None and self._outage.hit:
                        break
                    # On failure, skip this batch to avoid infinite loop
                    last_node_id = metadata_objects[-1]['node_id']
                    self.total_failed += len(metadata_objects)
                    logger.warning(f"Worker {self.worker_id}: Failed to process batch, skipping")

                # Rate limiting delay
                if self.rate_limit_delay > 0:
                    await asyncio.sleep(self.rate_limit_delay)

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Fatal error during indexing: {e}", exc_info=True)
        finally:
            self.is_running = False
            success_rate = (self.total_processed / self.total_to_index * 100) if self.total_to_index > 0 else 0
            cum = self.embedding_usage
            elapsed = embedding_metrics.elapsed_ms(self._started_at) / 1000.0
            logger.info(
                f"Worker {self.worker_id}: MetadataObject description indexing stopped. "
                f"Processed: {self.total_processed}/{self.total_to_index} ({success_rate:.1f}%), "
                f"failed: {self.total_failed}, "
                f"embedding_api_calls={cum.embedding_api_calls}, "
                f"input_tokens={embedding_metrics.format_usage_tokens(cum.input_tokens)}, "
                f"total_tokens={embedding_metrics.format_usage_tokens(cum.total_tokens)}, "
                f"cost={embedding_metrics.format_cost(*cum.primary_cost())}, "
                f"elapsed={elapsed:.1f}s"
            )

    def fetch_batch(self, last_node_id: int) -> List[Dict[str, Any]]:
        """
        Fetch next batch of metadata objects without embeddings.

        Uses hash-based distribution to partition work across workers.

        Note on extensions:
        - Indexes ALL MetadataObjects (base + extensions) without filtering by config_name
        - This includes "Заимствованный" (borrowed) objects from extensions
        - May result in duplicate embeddings for objects with identical descriptions
        - Rationale: Borrowed objects may have own attributes with descriptions worth indexing

        Args:
            last_node_id: Last processed node ID (for pagination)

        Returns:
            List of metadata object dictionaries with node_id and description fields
        """
        query = """
        MATCH (m:MetadataObject)
        WHERE m.description_embedding IS NULL
          AND (m.name IS NOT NULL OR m.`Синоним` IS NOT NULL
               OR m.`Комментарий` IS NOT NULL OR m.`Справка` IS NOT NULL
               OR m.`Пояснение` IS NOT NULL)
          AND toLower(m.project_name) = toLower($project_name)
          AND id(m) % $total_workers = $worker_id
          AND id(m) > $last_node_id
        RETURN id(m) as node_id,
               m.name as name,
               m.`Синоним` as synonym,
               m.`Комментарий` as comment,
               m.`Справка` as help,
               m.`Пояснение` as explanation
        ORDER BY id(m)
        LIMIT $batch_size
        """

        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(
                    query,
                    project_name=settings.project_name,
                    total_workers=self.total_workers,
                    worker_id=self.worker_id,
                    last_node_id=last_node_id,
                    batch_size=self.batch_size
                )
                metadata_objects = [dict(record) for record in result]
                return metadata_objects

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Failed to fetch batch: {e}")
            return []

    def _combine_description_fields(self, obj: Dict[str, Any]) -> str:
        """
        Combine description fields into a single text with double newlines.

        Args:
            obj: Metadata object dictionary with description fields

        Returns:
            Combined text string
        """
        comment = obj.get('comment')
        # Drop comments that are only a static-analysis marker (e.g. "АПК:93 ...")
        # — these are noise for semantic search.
        if comment and str(comment).strip().upper().startswith('АПК:'):
            comment = None

        fields = [
            obj.get('name'),
            obj.get('synonym'),
            comment,
            obj.get('help'),
            obj.get('explanation')
        ]

        # Filter out None and empty strings, then join with double newlines
        non_empty_fields = [str(f).strip() for f in fields if f and str(f).strip()]
        return '\n\n'.join(non_empty_fields)

    def _chunk_text(self, text: str) -> Tuple[List[str], List[int]]:
        """
        Split long text into overlapping chunks suitable for embedding,
        using boundary-aware splitter from `embedding_chunks`.
        """
        chunk_chars = int(getattr(self.embedding_service, "effective_chunk_chars", 0) or 0)
        if chunk_chars <= 0:
            max_tokens_fallback = int(getattr(settings, "embedding_max_input_tokens_fallback", 8192) or 8192)
            safety_ratio = float(getattr(settings, "embedding_chunk_safety_ratio", 0.9) or 0.9)
            cpt = float(getattr(settings, "embedding_chars_per_token_fallback", 2.0) or 2.0)
            chunk_chars = max(1000, int(max_tokens_fallback * safety_ratio * cpt))

        overlap = int(getattr(settings, "embedding_chunk_overlap_chars", 200) or 0)
        max_chunks = int(getattr(settings, "embedding_max_chunks_per_object", 12) or 12)

        return split_text_for_embedding(text, chunk_chars, overlap, max_chunks)

    def _pool(self, vectors: List[List[float]], weights: List[int]) -> List[float]:
        """Weighted mean pool of chunk embeddings, with per-chunk and final L2 norm controlled by settings."""
        l2_chunks = bool(getattr(settings, "embedding_l2_norm_chunks", True))
        l2_final = bool(getattr(settings, "embedding_l2_norm_final", True))
        return weighted_mean_pool(vectors, weights, l2_chunks=l2_chunks, l2_final=l2_final)

    async def process_batch(self, metadata_objects: List[Dict[str, Any]]) -> bool:
        """
        Process a batch of metadata objects: chunk long texts, generate embeddings for chunks,
        mean-pool per object, and save to database.
        
        Args:
            metadata_objects: List of metadata object dictionaries
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Reset per-batch usage so a prior batch never leaks into this
            # batch's progress log (e.g. on the no-chunks early return below).
            self._last_batch_usage = embedding_metrics.EmbeddingUsageStats()

            # Combine description fields for each object
            texts = [self._combine_description_fields(obj) for obj in metadata_objects]

            # Build flat chunk list and mapping per object
            all_chunks: List[str] = []
            idx_map: List[Tuple[int, int, List[int]]] = []  # (start_index, count, chunk_lengths)
            for t in texts:
                chunks, lens = self._chunk_text(t)
                idx_map.append((len(all_chunks), len(chunks), lens))
                all_chunks.extend(chunks)

            # Generate embeddings for all chunks in batches (async to not block)
            if not all_chunks:
                logger.warning(f"Worker {self.worker_id}: No chunks to embed in this batch")
                return True

            format_spec = build_embedding_format_spec(
                profile=self.embedding_service.text_format_profile,
                transport=self.embedding_service.transport,
                side="document",
                purpose="description",
                description_instruction=settings.embedding_description_query_instruction,
            )
            metric_started = embedding_metrics.started()
            try:
                batch_result = await asyncio.to_thread(
                    lambda: embedding_metrics.call_batched_with_usage(
                        self.embedding_service, all_chunks, format_spec=format_spec
                    )
                )
            except Exception:
                embedding_metrics.record_failure(
                    event_type="metadata_description.embedding.index",
                    embedding_service=self.embedding_service,
                    duration_ms=embedding_metrics.elapsed_ms(metric_started),
                )
                raise
            embedding_metrics.record_result(
                event_type="metadata_description.embedding.index",
                embedding_service=self.embedding_service,
                result=batch_result,
                duration_ms=embedding_metrics.elapsed_ms(metric_started),
            )
            # The embedding API call already happened (and is billed), so fold
            # its usage into the cumulative here, before any mismatch/save check
            # below can return False — usage must not be lost on a later failure.
            batch_usage = embedding_metrics.EmbeddingUsageStats.from_result(batch_result)
            self._last_batch_usage = batch_usage
            self.embedding_usage.add(batch_usage)
            chunk_vectors: List[List[float]] = batch_result.embeddings

            if len(chunk_vectors) != len(all_chunks):
                logger.error(
                    f"Worker {self.worker_id}: Embedding count mismatch for chunks: "
                    f"got {len(chunk_vectors)}, expected {len(all_chunks)}"
                )
                return False

            # Mean-pool per object (weighted by chunk length)
            pooled_vectors: List[List[float]] = []
            for start, count, lens in idx_map:
                if count <= 0:
                    pooled_vectors.append([])
                    continue
                vecs = chunk_vectors[start:start + count]
                pooled = self._pool(vecs, lens)
                pooled_vectors.append(pooled)

            if len(pooled_vectors) != len(metadata_objects):
                logger.error(
                    f"Worker {self.worker_id}: Pooled vector count mismatch: "
                    f"got {len(pooled_vectors)}, expected {len(metadata_objects)}"
                )
                return False

            # Save embeddings to database
            self.save_embeddings(metadata_objects, pooled_vectors)

            return True

        except Exception as e:
            if self._outage is not None and is_embedding_unavailable_error(e):
                # Known external outage after preflight: signal the coordinator,
                # log once (not per batch), no traceback. The run loop stops.
                if self._outage.signal(format_embedding_error(e)):
                    logger.warning(
                        "Worker %s: embedding endpoint unavailable (%s); "
                        "stopping metadata description embedding this pass",
                        self.worker_id, self._outage.reason,
                    )
                return False
            logger.error(f"Worker {self.worker_id}: Failed to process batch: {e}", exc_info=True)
            return False

    def save_embeddings(self, metadata_objects: List[Dict[str, Any]], embeddings: List[Optional[List[float]]]) -> None:
        """
        Save embeddings to Neo4j database in batches.

        Args:
            metadata_objects: List of metadata object dictionaries
            embeddings: List of embedding vectors (same order as metadata_objects); None/empty entries are skipped.
        """
        updates = [
            {
                'node_id': obj['node_id'],
                'embedding': emb
            }
            for obj, emb in zip(metadata_objects, embeddings)
            if emb
        ]

        if not updates:
            logger.info(f"Worker {self.worker_id}: No non-empty embeddings to save in this batch, skipping")
            return

        # Process in save batches
        for i in range(0, len(updates), self.save_batch_size):
            batch = updates[i:i + self.save_batch_size]
            self._save_batch(batch)

    def _save_batch(self, batch: List[Dict[str, Any]]) -> None:
        """
        Save a single batch of embeddings to Neo4j.

        Args:
            batch: List of updates with node_id and embedding
        """
        query = """
        UNWIND $batch AS item
        MATCH (m:MetadataObject)
        WHERE id(m) = item.node_id
        SET m.description_embedding = item.embedding
        """

        try:
            with self.driver.session(database=self.database) as session:
                session.run(query, batch=batch)
                logger.debug(f"Worker {self.worker_id}: Saved {len(batch)} embeddings to database")

        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Failed to save batch to database: {e}")
            raise

    def stop(self) -> None:
        """Stop the indexing process gracefully"""
        logger.info(f"Worker {self.worker_id}: Stop requested")
        self.is_running = False
