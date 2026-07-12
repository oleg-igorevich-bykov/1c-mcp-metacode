"""
Directory scanner — parses files discovered by CodeFileIndexer.

The scanner no longer walks the filesystem. CodeFileIndexer.scan() already
visited code_root once and built typed lists of paths. This module only
parses the listed files in parallel (ThreadPoolExecutor) and enqueues BSL
files for the multiprocessing BSL workers.

Two entry points:
  * DirectoryScanSession — streaming: callbacks fire during a single os.walk
    (TXT callback-mode), or files are replayed from a ready CodeFileIndex
    (XML list-replay). Holds the ThreadPool open across submissions.
  * DirectoryScanner — thin backward-compatible wrapper around the session
    that replays a CodeFileIndex in one call. Not used by the orchestrator
    (which drives the session directly), kept for compatibility.
"""

from pathlib import Path
import logging
from typing import Dict, List, Any, Set, Callable, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .data_structures import FormsData, PredefinedData, HelpData, ProcessingConfig, ProcessingStatistics
from .code_file_index import CodeFileScanConsumers
from .workers import worker_form_xml, worker_predefined, worker_help, worker_form_bin, worker_event_subscription

logger = logging.getLogger(__name__)


class DirectoryScanSession:
    """Streaming parse session for secondary files (forms / predefined / help /
    event subscriptions) plus BSL queueing.

    Callbacks (``on_*``) submit work to the ThreadPool immediately, applying
    ``max_in_flight`` backpressure — when used as ``CodeFileScanConsumers`` they
    overlap parsing with the os.walk. The same methods back ``submit_index_*``
    for XML list-replay from a ready CodeFileIndex. ``finish()`` drains the pool
    and returns the same dict the old DirectoryScanner.scan returned.

    BSL is NOT parsed here — .bsl / Form.bin paths are forwarded to the
    multiprocessing BSL pipeline via ``bsl_queue_callback``.
    """

    def __init__(
        self,
        config: ProcessingConfig,
        configurations: List[Any],
        project_name: str,
        code_root: Path,
        *,
        bsl_queue_callback: Optional[Callable[[Path], None]] = None,
    ):
        self.config = config
        self.configurations = configurations
        self.project_name = project_name
        self.code_root = code_root
        self.bsl_queue_callback = bsl_queue_callback
        self.cfg_by_name: Dict[str, Any] = {cfg.name: cfg for cfg in configurations}
        self.stats = ProcessingStatistics()

        from parsers.form_xml_parser import FormXmlParser
        from parsers.predefined_parser import PredefinedParser

        self._fparser = FormXmlParser()
        self._pre_parser = PredefinedParser()

        # Accumulators
        self.forms_data = FormsData()
        self.predef_data = PredefinedData()
        self.help_data = HelpData()
        self.formbin_results: List[Dict[str, Any]] = []
        self.event_subscriptions: List = []

        self._executor = ThreadPoolExecutor(max_workers=config.max_workers)
        self._in_flight: Set = set()
        self._max_in_flight = config.max_in_flight
        self._closed = False

    # ------------------------------------------------------------------ #
    # Backpressure
    # ------------------------------------------------------------------ #
    def _maybe_backpressure(self):
        if len(self._in_flight) >= self._max_in_flight:
            try:
                done = next(as_completed(self._in_flight))
                self._in_flight.remove(done)
                self._process_completed_future(done)
            except StopIteration:
                pass

    # ------------------------------------------------------------------ #
    # Streaming callbacks (TXT callback-mode) — also reused by list-replay
    # ------------------------------------------------------------------ #
    def on_event_subscription_xml(self, path: Path):
        if not self.config.enable_event_subscriptions:
            return
        fut = self._executor.submit(worker_event_subscription, path)
        self._in_flight.add(fut)
        self.stats.discovered_event_subs += 1
        self._maybe_backpressure()

    def on_form_xml(self, entry):
        if not self.config.enable_forms:
            return
        for cfg in self.configurations:
            fut = self._executor.submit(
                worker_form_xml,
                entry.form_xml_path, cfg.name, self.code_root,
                self.project_name, self._fparser, self.cfg_by_name,
            )
            self._in_flight.add(fut)
        self.stats.discovered_forms += 1
        self._maybe_backpressure()

    def on_predefined_xml(self, path: Path):
        if not self.config.enable_predefined:
            return
        fut = self._executor.submit(worker_predefined, path, self._pre_parser)
        self._in_flight.add(fut)
        self.stats.discovered_predef += 1
        self._maybe_backpressure()

    def on_help_html(self, path: Path):
        if not self.config.enable_help:
            return
        fut = self._executor.submit(worker_help, path)
        self._in_flight.add(fut)
        self.stats.discovered_help += 1
        self._maybe_backpressure()

    def on_bsl_file(self, path: Path):
        if self.config.enable_bsl and self.bsl_queue_callback is not None:
            self.bsl_queue_callback(path)
            self.stats.discovered_bsl += 1

    def on_form_bin(self, path: Path):
        if self.config.enable_bsl and self.bsl_queue_callback is not None:
            self.bsl_queue_callback(path)
            self.stats.discovered_form_bin += 1

    def consumers(self, *, include_bsl: bool) -> CodeFileScanConsumers:
        """Build CodeFileScanConsumers wired to this session's callbacks.

        include_bsl=True (base TXT): .bsl / Form.bin are queued during the walk.
        include_bsl=False (extensions): BSL is collected in the index and queued
        later, after forms, to preserve the existing write order.
        """
        return CodeFileScanConsumers(
            on_form_xml=self.on_form_xml,
            on_predefined_xml=self.on_predefined_xml,
            on_help_html=self.on_help_html,
            on_event_subscription_xml=self.on_event_subscription_xml,
            on_bsl_file=self.on_bsl_file if include_bsl else None,
            on_form_bin=self.on_form_bin if include_bsl else None,
        )

    # ------------------------------------------------------------------ #
    # List-replay from a ready CodeFileIndex (XML mode / compat)
    # ------------------------------------------------------------------ #
    def submit_index_non_bsl(self, code_index):
        for p in code_index.event_subscription_xml_files:
            self.on_event_subscription_xml(p)
        for entry in code_index.form_xml_files:
            self.on_form_xml(entry)
        for p in code_index.predefined_xml_files:
            self.on_predefined_xml(p)
        for p in code_index.help_html_files:
            self.on_help_html(p)

    def queue_bsl_from_index(self, code_index):
        if not (self.config.enable_bsl and self.bsl_queue_callback is not None):
            return
        for p in code_index.bsl_files:
            self.bsl_queue_callback(p)
            self.stats.discovered_bsl += 1
        for p in getattr(code_index, "form_bin_files", []):
            self.bsl_queue_callback(p)
            self.stats.discovered_form_bin += 1

    # ------------------------------------------------------------------ #
    # Finish / cleanup
    # ------------------------------------------------------------------ #
    def finish(self) -> Dict[str, Any]:
        try:
            for done in as_completed(self._in_flight):
                self._process_completed_future(done)
            self._in_flight.clear()

            logger.info(
                "Streaming scan finished: discovered forms=%d, predef=%d, bsl=%d, form_bin=%d, "
                "help=%d, event_subs=%d; parsed form tasks=%d, predef tasks=%d, help files=%d, "
                "event_subs=%d",
                self.stats.discovered_forms, self.stats.discovered_predef, self.stats.discovered_bsl,
                self.stats.discovered_form_bin, self.stats.discovered_help, self.stats.discovered_event_subs,
                self.stats.parsed_forms, self.stats.parsed_predef, self.stats.parsed_help,
                self.stats.parsed_event_subs,
            )
            logger.info("-" * 60)

            return {
                "forms_data": self.forms_data,
                "predef_data": self.predef_data,
                "help_data": self.help_data,
                "formbin_results": self.formbin_results,
                "event_subscriptions": self.event_subscriptions,
                "statistics": self.stats,
            }
        finally:
            self.shutdown()

    def shutdown(self):
        if not self._closed:
            self._executor.shutdown(cancel_futures=True)
            self._closed = True

    # ------------------------------------------------------------------ #
    # Result merging
    # ------------------------------------------------------------------ #
    def _process_completed_future(self, fut):
        try:
            res = fut.result()
        except Exception as e:
            logger.error("Worker exception: %s", e)
            return

        if not isinstance(res, dict):
            return

        kind = res.get("kind")

        if kind == "form":
            self._merge_form_result(res)
            self.stats.parsed_forms += 1
            if (self.stats.parsed_forms % 100) == 0:
                logger.info("Parsed Form.xml tasks so far: %d", self.stats.parsed_forms)

        elif kind == "predef":
            self._merge_predef_result(res)
            self.stats.parsed_predef += 1
            if (self.stats.parsed_predef % 100) == 0:
                logger.info("Parsed Predefined.xml tasks so far: %d", self.stats.parsed_predef)

        elif kind == "bsl":
            # Form.bin results (from ThreadPool worker)
            self.formbin_results.append(res)
            self.stats.parsed_form_bin += 1
            if (self.stats.parsed_form_bin % 100) == 0:
                logger.info("Parsed Form.bin files so far: %d", self.stats.parsed_form_bin)

        elif kind == "help":
            self._merge_help_result(res)
            self.stats.parsed_help += 1
            if (self.stats.parsed_help % 100) == 0:
                logger.info("Parsed Help/ru.html files so far: %d", self.stats.parsed_help)

        elif kind == "event_sub":
            sub = res.get("data")
            if sub is not None:
                self.event_subscriptions.append(sub)
                self.stats.parsed_event_subs += 1

    def _merge_form_result(self, result: Dict[str, Any]):
        rows = result.get("rows") or {}
        for key in [
            "form_updates", "controls", "root_rel", "child_rel",
            "events", "event_rel", "event_actions", "form_attributes", "form_commands",
            "form_command_usages", "data_bindings"
        ]:
            data = rows.get(key, [])
            if data:
                getattr(self.forms_data, key).extend(data)

        form_content_hash = result.get("form_content_hash")
        form_qn = result.get("form_qn")
        if form_content_hash and form_qn:
            self.forms_data.form_content_hashes.append({
                "form_qn": form_qn,
                "form_content_hash": form_content_hash,
                "base_form_hash": None,
            })

    def _merge_predef_result(self, result: Dict[str, Any]):
        items = result.get("items") or []
        rels = result.get("relations") or []
        if items:
            self.predef_data.items.extend(items)
        if rels:
            self.predef_data.relations.extend(rels)

    def _merge_help_result(self, result: Dict[str, Any]):
        cat_folder = result.get("category_folder")
        obj_name = result.get("object_name")
        content = result.get("help_content")
        if cat_folder and obj_name and content:
            self.help_data.help_by_object[(cat_folder, obj_name)] = content


class DirectoryScanner:
    """Backward-compatible wrapper: replays a CodeFileIndex through a
    DirectoryScanSession in one call. The orchestrator drives the session
    directly instead; this is kept for any other callers.
    """

    def __init__(
        self,
        config: ProcessingConfig,
        code_index,
        configurations: List[Any],
        project_name: str,
    ):
        self.config = config
        self.code_index = code_index
        self.code_root = code_index.root
        self.configurations = configurations
        self.project_name = project_name

    def scan(
        self,
        bsl_queue_callback: Optional[Callable[[Path], None]] = None,
        *,
        queue_bsl_first: bool = True,
    ) -> Dict[str, Any]:
        logger.info("Starting file processing from CodeFileIndex (root=%s) ...", self.code_root)
        session = DirectoryScanSession(
            self.config,
            self.configurations,
            self.project_name,
            self.code_root,
            bsl_queue_callback=bsl_queue_callback,
        )
        if queue_bsl_first:
            session.queue_bsl_from_index(self.code_index)
            session.submit_index_non_bsl(self.code_index)
        else:
            session.submit_index_non_bsl(self.code_index)
            session.queue_bsl_from_index(self.code_index)
        return session.finish()
