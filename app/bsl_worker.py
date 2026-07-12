"""
Worker functions for parallel BSL parsing with multiprocessing.

This module provides worker functions that:
1. Parse BSL source files (.bsl) and Form.bin-extracted modules
2. Write full data to Neo4j (modules, routines, declares) using a process-scoped driver
3. Return lightweight indexes for callsites resolution and compact form handler links

Architecture:
- Streaming mode: worker processes read file paths from a multiprocessing Queue
- Each worker process can use multiple threads (controlled by threads_per_process)
- A single Neo4j driver is created per process; loader methods open short-lived sessions per operation
- Full routine data is written to Neo4j in micro-batches (do_linking=False; linking is deferred)
- Only lightweight indexes and form_links are returned to avoid IPC overhead; the main process runs a single post-phase linking
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from multiprocessing import Queue
from queue import Empty
import logging
import os
import threading

from bsl_signature_scanner import scan_bsl_file, scan_bsl_from_form_bin
from parsers.form_bin_parser import FormBinParser
from neo4j_loader import Neo4jLoader
from config import settings

logger = logging.getLogger(__name__)


def worker_bsl_streaming(
    file_queue: Queue,
    result_queue: Queue,
    code_root: Path,
    project_name: str,
    cfg_name: str,
    threads_per_process: int,
    worker_id: int
) -> None:
    """
    Worker process for streaming BSL parsing.
    Reads file paths from file_queue, processes them, and sends results to result_queue.

    Args:
        file_queue: Queue with BSL file paths to process (multiprocessing.Queue)
        result_queue: Queue for sending results back (multiprocessing.Queue)
        code_root: Root directory of code dump
        project_name: 1C project name
        cfg_name: Configuration name
        threads_per_process: Number of threads to use in this process
        worker_id: ID of this worker for logging
    """
    # Configure logging for this worker process
    import sys
    logging.basicConfig(
        level=logging.DEBUG if settings.enable_debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True
    )

    files_processed = 0
    files_failed = 0

    # Create a single Neo4jLoader per process and reuse across threads
    loader: Optional[Neo4jLoader] = None
    try:
        loader = Neo4jLoader()
    except Exception as e:
        logger.error(f"Worker {worker_id} failed to create Neo4jLoader: {e}", exc_info=True)
        # Signal completion with zero work and exit this process early
        result_queue.put({
            "worker_done": True,
            "worker_id": worker_id,
            "files_processed": files_processed,
            "files_failed": files_failed,
        })
        return

    # ------------- Micro-batching by total body size (settings.neo4j_bsl_batch_max_mb) -------------
    max_batch_mb = settings.neo4j_bsl_batch_max_mb
    max_size_bytes = int(max_batch_mb * 1024 * 1024)
    if settings.enable_parallel_logging:
        logger.info("Worker start: id=%s pid=%s threads=%s max_batch_mb=%.2f", worker_id, os.getpid(), threads_per_process, max_batch_mb)

    pending_modules: List[Dict[str, Any]] = []
    pending_routines: List[Dict[str, Any]] = []
    pending_declares: List[Dict[str, Any]] = []
    pending_common_declares: List[Dict[str, Any]] = []
    # For emitting per-file success messages after a batch commit
    pending_items: List[Dict[str, Any]] = []  # each: {"routines_index":[], "callsites":[], "form_links":[]}
    batch_size_bytes: int = 0

    def _calc_body_bytes(routines: List[Dict[str, Any]]) -> int:
        try:
            return sum(len((r.get("body") or "").encode("utf-8")) for r in (routines or []))
        except Exception:
            return 0

    def _flush_batch():
        nonlocal pending_modules, pending_routines, pending_declares, pending_common_declares, pending_items, batch_size_bytes, files_processed, files_failed
        if not pending_items:
            return
        try:
            if settings.enable_parallel_logging:
                logger.info(
                    "Batch commit: pid=%s files=%d modules=%d routines=%d size=%.2fMB",
                    os.getpid(), len(pending_items), len(pending_modules), len(pending_routines),
                    batch_size_bytes / (1024 * 1024)
                )
            # Single call to persist accumulated modules/routines/declares for the batch
            loader.load_bsl_signatures(
                project_name,
                cfg_name,
                pending_modules,
                pending_routines,
                pending_declares,
                pending_common_declares,
                None,  # form_routines not used here
                do_linking=False
            )
            # Emit per-file success messages (indexes/callsites/form_links were carried along)
            for it in pending_items:
                result_queue.put({
                    "success": True,
                    "routines_index": it.get("routines_index", []),
                    "callsites": it.get("callsites", []),
                    "form_links": it.get("form_links", []),
                })
            files_processed += len(pending_items)
        except Exception as e:
            logger.error(f"Worker {worker_id} batch Neo4j write failed: {e}", exc_info=True)
            files_failed += len(pending_items)
        finally:
            # Reset batch
            pending_modules = []
            pending_routines = []
            pending_declares = []
            pending_common_declares = []
            pending_items = []
            batch_size_bytes = 0

    def _enqueue_result_for_batch(res: Dict[str, Any]):
        nonlocal batch_size_bytes
        if not res:
            return
        # Compute bytes for this file
        file_bytes = _calc_body_bytes(res.get("routines") or [])
        # If adding this file would overflow the batch, flush first (when batch already has items)
        if pending_items and (batch_size_bytes + file_bytes > max_size_bytes):
            _flush_batch()
        # Accumulate this file's payload into batch lists
        mod = res.get("module")
        if mod:
            pending_modules.append(mod)
        rs = res.get("routines") or []
        if rs:
            pending_routines.extend(rs)
        ds = res.get("declares") or []
        if ds:
            pending_declares.extend(ds)
        cds = res.get("common_declares") or []
        if cds:
            pending_common_declares.extend(cds)
        batch_size_bytes += file_bytes
        # Save per-file lightweight artifacts for result_queue after flush
        pending_items.append({
            "routines_index": res.get("routines_index", []),
            "callsites": res.get("callsites", []),
            "form_links": res.get("form_links", []),
        })

    def thread_worker(bsl_path: Path) -> Optional[Dict[str, Any]]:
        """Thread worker that processes a single BSL file"""
        try:
            if settings.enable_parallel_logging:
                logger.debug("Parse start: pid=%s thread=%s path=%s", os.getpid(), threading.current_thread().name, bsl_path)
            # 1. Parse BSL file (.bsl or Form.bin)
            if bsl_path.name.lower() == "form.bin":
                try:
                    parser = FormBinParser(code_root)
                    code_chunks, module_path_line = parser.parse(bsl_path)
                except Exception as pe:
                    logger.error(f"Worker {worker_id} failed to extract code from Form.bin {bsl_path}: {pe}", exc_info=True)
                    return None
                if not code_chunks or not code_chunks[0]:
                    logger.info("Form.bin with zero code extracted: %s", bsl_path)
                    # Return empty result (same as empty BSL file - not an error)
                    result = {
                        "kind": "bsl",
                        "module": None,
                        "routines": [],
                        "declares": [],
                        "common_declares": [],
                        "callsites": [],
                    }
                else:
                    result = scan_bsl_from_form_bin(
                    code_chunks[0],
                    bsl_path,
                    code_root,
                    project_name,
                    cfg_name
                )
            else:
                result = scan_bsl_file(bsl_path, code_root, project_name, cfg_name)
            if not result:
                return None
            if settings.enable_parallel_logging:
                logger.debug(
                    "Parse done: pid=%s thread=%s path=%s routines=%d",
                    os.getpid(), threading.current_thread().name, bsl_path, len(result.get("routines", []))
                )

            # 2. Do NOT write to Neo4j here (micro-batching handles persistence in the main loop)
            #    Just return parsed payload and lightweight artifacts

            # 3. Build LIGHTWEIGHT index for callsites resolution
            routines_index = []
            for r in result.get("routines", []):
                params = r.get("params_json") or []

                # Compute min/max arity
                min_arity = 0
                for p in params:
                    if isinstance(p, dict):
                        default_present = p.get("default_present", False)
                        markers = p.get("markers_raw", [])
                        is_optional = default_present or any("необязатель" in str(m).casefold() for m in markers)
                        if not is_optional:
                            min_arity += 1
                    else:
                        min_arity += 1

                routines_index.append({
                    "id": r["id"],
                    "name": r["name"],
                    "module_id": r.get("module_id"),
                    "owner_qn": r.get("owner_qn"),
                    "module_type": r.get("module_type"),
                    "min_arity": min_arity,
                    "max_arity": len(params),
                    "directives": r.get("directives", []),
                    "export": r.get("export", False),
                    "params_json": params,  # Keep for callsites resolution
                    # Required by incremental BSLFileArtifact builder: per-file grouping.
                    "file_path": r.get("file_path"),
                    "config_name": r.get("config_name"),
                    # Per-routine hashes для routine-level incremental diff
                    # (см. bsl_routine_delta.build_delta). Эти поля безопасны на full-load —
                    # они просто проходят через aggregator, никаких extra graph writes.
                    "body_hash": r.get("body_hash", ""),
                    "doc_hash": r.get("doc_hash", ""),
                    "doc_description_embedding_hash": r.get("doc_description_embedding_hash", ""),
                    "signature_hash": r.get("signature_hash", ""),
                    "routine_state_hash": r.get("routine_state_hash", ""),
                    "line": r.get("line", 0),
                    "signature": r.get("signature", ""),
                    "params_text": r.get("params_text", ""),
                    "routine_type": r.get("routine_type", ""),
                    "decorator_type": r.get("decorator_type", ""),
                    "decorator_target": r.get("decorator_target", ""),
                })

            # Build compact links for form handlers to be used in post-phase linking
            form_links = []
            mod = result.get("module") or {}
            if mod.get("owner_label") == "Form" and mod.get("owner_qn"):
                fq = mod.get("owner_qn")
                for rr in result.get("routines", []):
                    rn = rr.get("name")
                    if rn:
                        form_links.append({"form_qn": fq, "routine_name": rn})

            return {
                "module": result.get("module"),
                "routines": result.get("routines", []),
                "declares": result.get("declares", []),
                "common_declares": result.get("common_declares", []),
                "routines_index": routines_index,
                "callsites": result.get("callsites", []),
                "form_links": form_links,
            }

        except Exception as e:
            logger.error(f"Worker {worker_id} thread failed for {bsl_path}: {e}")
            return None

    # Process files from queue using threads (if threads_per_process > 1)
    if threads_per_process > 1:
        # Thread pool mode
        with ThreadPoolExecutor(max_workers=threads_per_process) as thread_executor:
            futures = []

            while True:
                # Get file from queue
                try:
                    bsl_file = file_queue.get(timeout=1)
                except Empty:
                    # Timeout - drain any completed futures to keep memory flat and feed micro-batch
                    if futures:
                        done_futures = [f for f in futures if f.done()]
                        for fut in done_futures:
                            result = fut.result()
                            if result:
                                _enqueue_result_for_batch(result)
                            else:
                                files_failed += 1
                            futures.remove(fut)
                    # Continue waiting for new files or more completions
                    continue

                if bsl_file is None:
                    # Poison pill - stop signal
                    break

                # Submit to thread pool
                fut = thread_executor.submit(thread_worker, bsl_file)
                futures.append(fut)

                # Check completed futures periodically
                if len(futures) >= threads_per_process * 2:
                    done_futures = [f for f in futures if f.done()]
                    for fut in done_futures:
                        result = fut.result()
                        if result:
                            _enqueue_result_for_batch(result)
                        else:
                            files_failed += 1
                        futures.remove(fut)

            # Wait for remaining futures
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    _enqueue_result_for_batch(result)
                else:
                    files_failed += 1
            # Final flush after all futures done
            _flush_batch()
    else:
        # Sequential processing (threads_per_process == 1)
        while True:
            try:
                bsl_file = file_queue.get(timeout=1)
            except Empty:
                # Timeout - continue waiting
                continue

            if bsl_file is None:
                # Poison pill - stop signal
                break

            result = thread_worker(bsl_file)
            if result:
                _enqueue_result_for_batch(result)
            else:
                files_failed += 1

        # Final flush for sequential mode
        _flush_batch()

    # Close process-scoped Neo4jLoader before sending final statistics
    try:
        if loader:
            loader.close()
    except Exception:
        pass

    # Send final statistics
    result_queue.put({
        "worker_done": True,
        "worker_id": worker_id,
        "files_processed": files_processed,
        "files_failed": files_failed,
    })

    logger.info(f"Worker {worker_id} finished: {files_processed} OK, {files_failed} errors")


def worker_bsl_hybrid(
    file_batch: List[Path],
    code_root: Path,
    project_name: str,
    cfg_name: str,
    threads_per_process: int
) -> Dict[str, Any]:
    """
    Worker process for parsing BSL files.
    Uses threads internally for parallel processing if threads_per_process > 1.

    Args:
        file_batch: List of BSL file paths to process
        code_root: Root directory of code dump
        project_name: 1C project name
        cfg_name: Configuration name
        threads_per_process: Number of threads to use in this process

    Returns:
        Dictionary with:
        - success: bool
        - routines_index: List of lightweight routine metadata for callsites resolution
        - callsites: List of raw callsites
        - parsed_ok: Number of successfully parsed files
        - parsed_err: Number of failed files
    """
    # Configure logging for this worker process
    import sys
    logging.basicConfig(
        level=logging.DEBUG if settings.enable_debug else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True
    )

    all_routines_index = []
    all_callsites = []
    parsed_ok = 0
    parsed_err = 0

    # Create a single Neo4jLoader per process and reuse across threads
    loader: Optional[Neo4jLoader] = None
    try:
        loader = Neo4jLoader()
    except Exception as e:
        logger.error(f"Hybrid worker failed to create Neo4jLoader: {e}", exc_info=True)
        return {
            "success": False,
            "routines_index": [],
            "callsites": [],
            "parsed_ok": 0,
            "parsed_err": len(file_batch),
        }

    def thread_worker(bsl_path: Path) -> Optional[Dict[str, Any]]:
        """Thread worker that processes a single BSL file"""
        try:
            # 1. Parse BSL file (.bsl or Form.bin)
            if bsl_path.name.lower() == "form.bin":
                try:
                    parser = FormBinParser(code_root)
                    code_chunks, module_path_line = parser.parse(bsl_path)
                except Exception as pe:
                    logger.error(f"Thread worker failed to extract code from Form.bin {bsl_path}: {pe}", exc_info=True)
                    return None
                if not code_chunks or not code_chunks[0]:
                    logger.info("Form.bin with zero code extracted: %s", bsl_path)
                    # Return empty result (same as empty BSL file - not an error)
                    result = {
                        "kind": "bsl",
                        "module": None,
                        "routines": [],
                        "declares": [],
                        "common_declares": [],
                        "callsites": [],
                    }
                else:
                    result = scan_bsl_from_form_bin(
                    code_chunks[0],
                    bsl_path,
                    code_root,
                    project_name,
                    cfg_name
                )
            else:
                result = scan_bsl_file(bsl_path, code_root, project_name, cfg_name)
            if not result:
                return None

            # 2. Write FULL data to Neo4j (each thread creates its own connection)
            # 2. Write FULL data to Neo4j (reuse process-scoped driver)
            try:
                # Use existing load_bsl_signatures method
                # Pass only data from this single file
                loader.load_bsl_signatures(
                    project_name,
                    cfg_name,
                    [result["module"]] if result.get("module") else [],
                    result.get("routines", []),
                    result.get("declares", []),
                    result.get("common_declares", []),
                    None,  # form_routines not needed here
                    do_linking=False
                )
            except Exception as e:
                logger.error(f"Thread worker Neo4j write failed for {bsl_path}: {e}", exc_info=True)
                return None

            # 3. Build LIGHTWEIGHT index for callsites resolution
            routines_index = []
            for r in result.get("routines", []):
                params = r.get("params_json") or []

                # Compute min/max arity
                min_arity = 0
                for p in params:
                    if isinstance(p, dict):
                        default_present = p.get("default_present", False)
                        markers = p.get("markers_raw", [])
                        is_optional = default_present or any("необязатель" in str(m).casefold() for m in markers)
                        if not is_optional:
                            min_arity += 1
                    else:
                        min_arity += 1

                routines_index.append({
                    "id": r["id"],
                    "name": r["name"],
                    "module_id": r.get("module_id"),
                    "owner_qn": r.get("owner_qn"),
                    "module_type": r.get("module_type"),
                    "min_arity": min_arity,
                    "max_arity": len(params),
                    "directives": r.get("directives", []),
                    "export": r.get("export", False),
                    "params_json": params,  # Keep for callsites resolution
                    # Required by incremental BSLFileArtifact builder: per-file grouping.
                    "file_path": r.get("file_path"),
                    "config_name": r.get("config_name"),
                    # Per-routine hashes для routine-level incremental diff
                    # (см. bsl_routine_delta.build_delta). Эти поля безопасны на full-load —
                    # они просто проходят через aggregator, никаких extra graph writes.
                    "body_hash": r.get("body_hash", ""),
                    "doc_hash": r.get("doc_hash", ""),
                    "doc_description_embedding_hash": r.get("doc_description_embedding_hash", ""),
                    "signature_hash": r.get("signature_hash", ""),
                    "routine_state_hash": r.get("routine_state_hash", ""),
                    "line": r.get("line", 0),
                    "signature": r.get("signature", ""),
                    "params_text": r.get("params_text", ""),
                    "routine_type": r.get("routine_type", ""),
                    "decorator_type": r.get("decorator_type", ""),
                    "decorator_target": r.get("decorator_target", ""),
                })

            # Build compact links for form handlers to be used in post-phase linking
            form_links = []
            mod = result.get("module") or {}
            if mod.get("owner_label") == "Form" and mod.get("owner_qn"):
                fq = mod.get("owner_qn")
                for rr in result.get("routines", []):
                    rn = rr.get("name")
                    if rn:
                        form_links.append({"form_qn": fq, "routine_name": rn})

            return {
                "routines_index": routines_index,
                "callsites": result.get("callsites", []),
                "form_links": form_links,
            }

        except Exception as e:
            logger.error(f"Thread worker failed for {bsl_path}: {e}")
            return None

    # Process files in this batch using threads (if threads_per_process > 1)
    if threads_per_process > 1:
        with ThreadPoolExecutor(max_workers=threads_per_process) as thread_executor:
            futures = {
                thread_executor.submit(thread_worker, bsl_file): bsl_file
                for bsl_file in file_batch
            }

            for future in as_completed(futures):
                result = future.result()
                if result:
                    all_routines_index.extend(result.get("routines_index", []))
                    all_callsites.extend(result.get("callsites", []))
                    parsed_ok += 1
                else:
                    parsed_err += 1
    else:
        # Sequential processing (threads_per_process == 1)
        for bsl_file in file_batch:
            result = thread_worker(bsl_file)
            if result:
                all_routines_index.extend(result.get("routines_index", []))
                all_callsites.extend(result.get("callsites", []))
                parsed_ok += 1
            else:
                parsed_err += 1

    # Return collected indexes from this process
    try:
        if loader:
            loader.close()
    except Exception:
        pass
    return {
        "success": True,
        "routines_index": all_routines_index,
        "callsites": all_callsites,
        "parsed_ok": parsed_ok,
        "parsed_err": parsed_err,
    }
