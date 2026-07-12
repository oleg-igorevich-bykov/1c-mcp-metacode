"""
BSL (1C:Enterprise Business Solution Language) processor.

Handles multi-process scanning and parsing of .bsl files and Form.bin files.
Uses streaming architecture with producer-consumer pattern for optimal performance.

This module contains ALL BSL processing logic including:
- Multi-process workers for .bsl files
- Form.bin processing (ThreadPool)
- Post-phase linking (form events, commands, URL methods)
- SSL API routine marking
- Callsites resolution integration
"""

from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
import logging
import multiprocessing as mp
import threading
import time
from queue import Empty

from .data_structures import BSLData

logger = logging.getLogger(__name__)


class BSLProcessor:
    """
    Processes BSL files using multiprocessing for optimal performance.

    Architecture:
    - Main process: discovers files during directory scan
    - Worker processes: parse BSL files in parallel (.bsl files)
    - ThreadPool: processes Form.bin files
    - Result collector thread: collects results to prevent queue blocking
    """

    def __init__(self, num_processes: Optional[int] = None, threads_per_process: Optional[int] = None):
        """
        Initialize BSL processor.

        Args:
            num_processes: Number of worker processes (None = auto from settings)
            threads_per_process: Threads per process (None = auto from settings)
        """
        self.num_processes = num_processes
        self.threads_per_process = threads_per_process

        # Worker infrastructure
        self.bsl_file_queue: Optional[mp.Queue] = None
        self.bsl_result_queue: Optional[mp.Queue] = None
        self.bsl_workers: List[mp.Process] = []

        # Result collection
        self.result_collector_thread: Optional[threading.Thread] = None
        self.result_collector_stop: Optional[threading.Event] = None

        # Collected data from multiprocessing workers (.bsl files)
        self.all_bsl_routines_index: List[Dict[str, Any]] = []
        self.all_bsl_callsites: List[Dict[str, Any]] = []
        self.all_form_links: List[Dict[str, Any]] = []
        self.bsl_workers_done = 0
        self.bsl_files_processed_total = 0
        self.bsl_parsed_count = 0

        # Form.bin data (processed in ThreadPool in main scan loop)
        # These are kept separate and merged later
        self.formbin_modules_by_id: Dict[str, Dict[str, Any]] = {}
        self.formbin_routines: List[Dict[str, Any]] = []
        self.formbin_declares: List[Dict[str, Any]] = []
        self.formbin_common_declares: List[Dict[str, Any]] = []
        self.formbin_callsites: List[Dict[str, Any]] = []
        self.formbin_form_routines: Dict[str, List[Dict[str, Any]]] = {}  # form_qn -> routines

    def start_workers(
        self,
        code_root: Path,
        project_name: str,
        cfg_name: str,
        settings
    ):
        """
        Start BSL worker processes BEFORE directory scanning.

        This is called BEFORE the main directory scan starts.
        Workers will be ready to process files as they are discovered.

        Args:
            code_root: Root directory containing code
            project_name: Project name
            cfg_name: Configuration name
            settings: Settings object
        """
        from bsl_worker import worker_bsl_streaming

        # Get configuration
        num_processes = self.num_processes or settings.BSL_PROCESS_WORKERS or settings.PROCESS_WORKERS
        threads_per_process = self.threads_per_process or settings.BSL_THREADS_PER_PROCESS or settings.THREADS_PER_PROCESS

        logger.info(f"Starting BSL streaming workers: {num_processes} processes × {threads_per_process} threads")

        # Create queues
        self.bsl_file_queue = mp.Queue(maxsize=num_processes * 10)
        self.bsl_result_queue = mp.Queue()

        # Start worker processes
        for worker_id in range(num_processes):
            p = mp.Process(
                target=worker_bsl_streaming,
                args=(
                    self.bsl_file_queue,
                    self.bsl_result_queue,
                    code_root,
                    project_name,
                    cfg_name,
                    threads_per_process,
                    worker_id
                )
            )
            p.start()
            self.bsl_workers.append(p)

        logger.info(f"BSL workers started, ready to process files during scan")

        # Start result collector thread to prevent queue blocking
        self.result_collector_stop = threading.Event()
        self.result_collector_thread = threading.Thread(
            target=self._collect_results,
            daemon=True
        )
        self.result_collector_thread.start()
        logger.info("BSL result collector thread started")

    def queue_file(self, file_path: Path):
        """
        Queue a .bsl or Form.bin file for processing.

        Called during directory scanning for each .bsl file found.

        Args:
            file_path: Path to .bsl file to process
        """
        if self.bsl_file_queue is not None:
            self.bsl_file_queue.put(file_path)

    def process_formbin_result(self, result: Dict[str, Any]):
        """
        Process a Form.bin parsing result from ThreadPool worker.

        Form.bin files are processed in ThreadPool (not multiprocessing)
        because they need to be integrated into the main scan loop.

        Args:
            result: Result dictionary from worker_form_bin
        """
        if not result or result.get("kind") != "bsl":
            return

        mod = result.get("module")
        if not mod or not mod.get("path", "").endswith("Form.bin"):
            return

        # Store module
        if mod:
            self.formbin_modules_by_id[mod.get("id")] = mod

        # Store routines
        rs = result.get("routines") or []
        if rs:
            self.formbin_routines.extend(rs)

            # Add to form_routines if FormModule
            if mod.get("owner_label") == "Form":
                form_qn = mod.get("owner_qn")
                if form_qn:
                    if form_qn not in self.formbin_form_routines:
                        self.formbin_form_routines[form_qn] = []
                    self.formbin_form_routines[form_qn].extend(rs)

        # Store declares
        ds = result.get("declares") or []
        if ds:
            self.formbin_declares.extend(ds)

        cds = result.get("common_declares") or []
        if cds:
            self.formbin_common_declares.extend(cds)

        # Store callsites
        cs = result.get("callsites") or []
        if cs:
            self.formbin_callsites.extend(cs)

    def finalize(self, settings, lease=None) -> BSLData:
        """
        Finalize BSL processing: stop workers and collect final results.

        This is called AFTER the main directory scan completes.

        Args:
            settings: Settings object for configuration
            lease: Optional LockLease — main-thread caller передаёт сюда scheduler
                lease, и `finalize` вызывает `lease.heartbeat()` между poll-итерациями
                wait-loop. Heartbeat выполняется в этом же main thread-е (caller).
                Никаких callback-ов внутрь worker/collector thread не пробрасывается.

        Returns:
            BSLData with all collected BSL data (both .bsl and Form.bin).
            One-shot: ownership of the collected containers is transferred to
            the returned BSLData; the processor's internal accumulators are
            empty after this call.
        """
        if not self.bsl_workers:
            logger.info("No BSL workers to finalize")
            return self._build_bsl_data()

        logger.info(f"Finalizing BSL processing. Waiting for {len(self.bsl_workers)} workers to finish...")

        # Send poison pills to stop workers
        try:
            for _ in range(len(self.bsl_workers)):
                self.bsl_file_queue.put(None)
        except Exception as e:
            logger.error(f"Error sending stop signals to BSL workers: {e}")

        try:
            # Wait until all workers report completion
            total_workers = len(self.bsl_workers)
            start_ts = time.time()
            last_heartbeat = 0.0

            while self.bsl_workers_done < total_workers:
                time.sleep(0.5)

                # Lock lease heartbeat (main-thread). Раз в секунду достаточно — stale
                # window не короче 300s, а next iteration через 0.5s.
                if lease is not None:
                    now_ts = time.time()
                    if now_ts - last_heartbeat >= 1.0:
                        lease.heartbeat()
                        last_heartbeat = now_ts

                # Optional progress logging
                if getattr(settings, "enable_parallel_logging", False):
                    waited = int(time.time() - start_ts)
                    if waited % 30 == 0 and waited != 0:
                        logger.info(
                            "Waiting for BSL workers to finish: %d/%d",
                            self.bsl_workers_done, total_workers
                        )

            # Join all worker processes
            for p in self.bsl_workers:
                p.join()

            # Stop result collector thread
            self.result_collector_stop.set()
            self.result_collector_thread.join(timeout=10)

            logger.info(f"BSL streaming complete: {self.bsl_files_processed_total} files parsed")
            logger.info(
                f"Collected {len(self.all_bsl_routines_index)} routine indexes, "
                f"{len(self.all_bsl_callsites)} callsites, "
                f"{len(self.all_form_links)} form handler links"
            )

        except Exception as e:
            logger.error(f"BSL finalization failed: {e}", exc_info=True)

            # Cleanup: terminate any remaining workers
            for p in self.bsl_workers:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)

        # Build and return complete BSLData
        return self._build_bsl_data()

    def terminate_workers(self):
        """Emergency cleanup for the abort path: terminate + join any live worker
        processes and stop the result collector thread. Idempotent — safe to call
        even if workers were never started or already finalized.

        Order matters: terminate workers and clear the worker list FIRST, then
        stop + join the collector. The collector loop condition keys off
        `result_collector_stop` AND `bsl_workers_done < len(self.bsl_workers)`,
        so it only exits once the worker list is empty — joining it before the
        workers are gone would just block on the timeout and leave the thread
        running.
        """
        # 1. Terminate + join worker processes, then clear the list so the
        #    collector's loop condition becomes terminal.
        for p in self.bsl_workers:
            try:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=5)
            except Exception as e:
                logger.error("BSL terminate: worker terminate failed: %s", e)
        self.bsl_workers = []

        # 2. Now stop the collector and join it.
        try:
            if self.result_collector_stop is not None:
                self.result_collector_stop.set()
            if self.result_collector_thread is not None and self.result_collector_thread.is_alive():
                self.result_collector_thread.join(timeout=5)
        except Exception as e:
            logger.error("BSL terminate: result collector stop failed: %s", e)

        logger.info("BSL workers terminated (abort cleanup)")

    def _build_bsl_data(self) -> BSLData:
        """
        Build BSLData from all collected results.

        Merges data from:
        - Multiprocessing workers (.bsl files)
        - ThreadPool workers (Form.bin files)

        One-shot ownership transfer: containers are MOVED into the returned
        BSLData (no backbone copies), and the processor's accumulators are
        rebound to fresh empty containers.

        Returns:
            BSLData with complete data
        """
        bsl_data = BSLData()

        # Move multiprocessing results (.bsl files)
        bsl_data.routines_indexes = self.all_bsl_routines_index
        bsl_data.form_links = self.all_form_links
        # callsites has two sources: move the large one, append the Form.bin tail
        bsl_data.callsites = self.all_bsl_callsites
        bsl_data.callsites.extend(self.formbin_callsites)

        # Build form_routines map from compact links
        for link in bsl_data.form_links:
            fq = link.get("form_qn")
            rn = link.get("routine_name")
            if fq and rn:
                if fq not in bsl_data.form_routines:
                    bsl_data.form_routines[fq] = []
                bsl_data.form_routines[fq].append({"name": rn})

        # Move Form.bin results (from ThreadPool)
        bsl_data.modules_by_id = self.formbin_modules_by_id
        bsl_data.routines_formbin = self.formbin_routines
        bsl_data.declares = self.formbin_declares
        bsl_data.common_declares = self.formbin_common_declares

        # Merge Form.bin form_routines into main map
        for form_qn, routines in self.formbin_form_routines.items():
            if form_qn not in bsl_data.form_routines:
                bsl_data.form_routines[form_qn] = []
            bsl_data.form_routines[form_qn].extend(routines)

        # Release accumulators: BSLData owns the containers from here on
        self.all_bsl_routines_index = []
        self.all_bsl_callsites = []
        self.all_form_links = []
        self.formbin_modules_by_id = {}
        self.formbin_routines = []
        self.formbin_declares = []
        self.formbin_common_declares = []
        self.formbin_callsites = []
        self.formbin_form_routines = {}

        return bsl_data

    def _collect_results(self):
        """
        Background thread that collects results from worker processes.

        This runs DURING the directory scan to prevent the result queue from blocking.
        Results are accumulated in instance variables.
        """
        while not self.result_collector_stop.is_set() or self.bsl_workers_done < len(self.bsl_workers):
            try:
                result = self.bsl_result_queue.get(timeout=1)

                if result.get("worker_done"):
                    # Worker finished
                    self.bsl_workers_done += 1
                    worker_id = result.get("worker_id", "?")
                    files_ok = result.get("files_processed", 0)
                    files_err = result.get("files_failed", 0)
                    self.bsl_files_processed_total += files_ok

                    logger.info(
                        f"BSL Worker {worker_id} done: {files_ok} OK, {files_err} errors "
                        f"({self.bsl_workers_done}/{len(self.bsl_workers)} workers finished)"
                    )
                else:
                    # File processing result
                    if result.get("success"):
                        self.all_bsl_routines_index.extend(result.get("routines_index", []))
                        self.all_bsl_callsites.extend(result.get("callsites", []))
                        self.all_form_links.extend(result.get("form_links", []))
                        self.bsl_parsed_count += 1

                        if (self.bsl_parsed_count % 200) == 0:
                            logger.info("Parsed BSL files so far: %d", self.bsl_parsed_count)

            except Empty:
                # Timeout - check if we should stop
                if self.result_collector_stop.is_set() and self.bsl_workers_done >= len(self.bsl_workers):
                    break
                continue

            except Exception as e:
                logger.error(f"Result collector error: {e}")
                continue

    def post_phase_linking(
        self,
        loader,  # Neo4jLoader instance
        project_name: str,
        cfg_name: str,
        form_routines: Dict[str, List[Dict[str, Any]]]
    ):
        """
        Post-phase linking after Forms and Predefined data are loaded.

        Links:
        - Form events to their handlers
        - Form controls to their handlers
        - Form commands to their handlers
        - URL methods

        Args:
            loader: Neo4jLoader instance
            project_name: Project name
            cfg_name: Configuration name
            form_routines: Map of form_qn -> routines
        """
        logger.info("Running post-phase BSL linking (form events, commands, URL methods)...")

        try:
            loader.link_form_events_and_commands(project_name, cfg_name, form_routines)
            logger.info("Post-phase linking finished")
        except Exception as e:
            logger.error("Post-phase linking failed: %s", e, exc_info=True)

    def mark_ssl_api_routines(
        self,
        loader,  # Neo4jLoader instance
        settings,
        session
    ) -> int:
        """Thin wrapper для full-load: делегирует в SslApiMarkerMixin.

        Direction `indexer -> graphdb` сохранён: full-load orchestrator (BSLProcessor)
        зовёт graphdb mixin, а не наоборот. Incremental layer зовёт mixin напрямую через
        loader.refresh_ssl_api_for_project / refresh_ssl_api_for_routines.

        Full-load logging contract (три классических log-line) сохранён внутри mixin'а.
        """
        try:
            return loader.refresh_ssl_api_for_project(session, settings.project_name)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to mark SSL API routines: %s", e, exc_info=True)
            return 0

