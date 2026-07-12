"""
Extension directory scanner - single-pass scanning for extension files.

Handles:
- XML files of metadata objects for property analysis
- Ext/Predefined.xml files for predefined values
- (Future) Forms, BSL, Help files
"""

import os
import logging
from pathlib import Path
from typing import Set
from concurrent.futures import ThreadPoolExecutor, as_completed

from .data_structures import ExtensionScanResults, PredefinedData
from .code_file_index import CodeFileScanConsumers
from .workers import worker_predefined, worker_help


def _read_form_belonging(descriptor_xml: Path) -> bool:
    """Return True if the form descriptor has <ObjectBelonging>Adopted</ObjectBelonging>."""
    import xml.etree.ElementTree as ET  # local import: ET not used elsewhere in this module
    try:
        for _, elem in ET.iterparse(descriptor_xml, events=("end",)):
            if elem.tag.split("}")[-1] == "ObjectBelonging":
                return elem.text == "Adopted"
    except Exception:
        return False
    return False

logger = logging.getLogger(__name__)


# Metadata categories to look for XML files
METADATA_CATEGORIES = {
    "Documents", "Catalogs", "Enums", "ChartsOfAccounts",
    "ChartsOfCharacteristicTypes", "ChartsOfCalculationTypes",
    "InformationRegisters", "AccumulationRegisters",
    "AccountingRegisters", "CalculationRegisters",
    "BusinessProcesses", "Tasks", "ExchangePlans",
    "DataProcessors", "Reports", "CommonModules",
    "Subsystems", "Roles",
    "CommonCommands", "Constants", "DocumentJournals",
    "HTTPServices", "WebServices", "XDTOPackages",
    "CommonTemplates", "CommonPictures", "StyleItems",
    "FunctionalOptions", "FunctionalOptionsParameters",
    "DefinedTypes", "Languages", "CommandGroups",
    "CommonAttributes", "FilterCriteria",
    "ScheduledJobs", "SessionParameters", "SettingsStorages",
}


class ExtensionScanSession:
    """Streaming parse session for extension secondary files.

    Distinct from the base DirectoryScanSession: extension forms are NOT parsed
    here — they are COLLECTED as (form_xml_path, descriptor, is_adopted) tuples
    and parsed later via worker_extension_form (which needs is_adopted and a
    different data shape). Predefined.xml and Help/ru.html ARE parsed here in a
    ThreadPool so they overlap with the os.walk.

    finish(code_index) returns the same ExtensionScanResults the legacy
    ExtensionScanner.scan returned, including the pass-through lists
    (bsl_files, event_subscription_files, xml_files_for_analysis) taken from the
    CodeFileIndex — these feed property analysis and event subscription loading.
    """

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self.results = ExtensionScanResults()
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._in_flight: Set = set()
        self._closed = False

    # ---- streaming callbacks ---- #
    def on_form_xml(self, entry):
        descriptor = entry.descriptor_xml_path
        is_adopted = (
            _read_form_belonging(descriptor) if descriptor and descriptor.exists() else False
        )
        self.results.form_files.append((entry.form_xml_path, descriptor, is_adopted))

    def on_predefined_xml(self, path: Path):
        fut = self._executor.submit(worker_predefined, path, self._pre_parser)
        self._in_flight.add(fut)
        self._maybe_drain()

    def on_help_html(self, path: Path):
        fut = self._executor.submit(worker_help, path)
        self._in_flight.add(fut)
        self._maybe_drain()

    def on_event_subscription_xml(self, path: Path):
        self.results.event_subscription_files.append(path)

    @property
    def _pre_parser(self):
        from parsers.predefined_parser import PredefinedParser
        if not hasattr(self, "_pre_parser_obj"):
            self._pre_parser_obj = PredefinedParser()
        return self._pre_parser_obj

    def _maybe_drain(self):
        if len(self._in_flight) >= self.max_workers * 2:
            for fut in list(self._in_flight):
                if fut.done():
                    self._in_flight.remove(fut)
                    self._merge(fut)

    def consumers(self, *, include_bsl: bool = False) -> CodeFileScanConsumers:
        # include_bsl is always False for extensions: BSL is collected in the
        # index and queued later (after forms) to preserve write order.
        return CodeFileScanConsumers(
            on_form_xml=self.on_form_xml,
            on_predefined_xml=self.on_predefined_xml,
            on_help_html=self.on_help_html,
            on_event_subscription_xml=self.on_event_subscription_xml,
        )

    def finish(self, code_index) -> ExtensionScanResults:
        try:
            for fut in as_completed(self._in_flight):
                self._merge(fut)
            self._in_flight.clear()

            # Pass-through lists from the index (Finding 1): these feed property
            # analysis and event subscription loading in extensions_loader.
            self.results.bsl_files = list(code_index.bsl_files)
            self.results.xml_files_for_analysis = list(code_index.extension_property_analysis_xml_files)
            # event_subscription_files were collected via callback; if the session
            # was driven by list-replay instead, fall back to the index.
            if not self.results.event_subscription_files:
                self.results.event_subscription_files = list(code_index.event_subscription_xml_files)

            logger.info(
                "  [SCANNER] ExtensionScanSession done: xml_for_analysis=%d, predefined=%d, "
                "form_files=%d, event_subs=%d, bsl_files=%d, help_files=%d",
                len(self.results.xml_files_for_analysis), len(self.results.predef_data.items),
                len(self.results.form_files), len(self.results.event_subscription_files),
                len(self.results.bsl_files), len(self.results.help_data.help_by_object),
            )
            return self.results
        finally:
            self.shutdown()

    def shutdown(self):
        if not self._closed:
            self._executor.shutdown(cancel_futures=True)
            self._closed = True

    def _merge(self, fut):
        try:
            result = fut.result()
            if not result:
                return
            kind = result.get("kind")
            if kind == "predef":
                items = result.get("items") or []
                relations = result.get("relations") or []
                if items:
                    self.results.predef_data.items.extend(items)
                if relations:
                    self.results.predef_data.relations.extend(relations)
            elif kind == "help":
                cat_folder = result.get("category_folder")
                obj_name = result.get("object_name")
                content = result.get("help_content")
                if cat_folder and obj_name and content:
                    self.results.help_data.help_by_object[(cat_folder, obj_name)] = content
        except Exception as e:
            logger.error("  [SCANNER] Error processing result: %s", str(e))


class ExtensionScanner:
    """
    Single-pass scanner for extension directory.

    Scans extension's code directory and collects:
    - XML files for property analysis
    - Predefined.xml files
    """

    def __init__(self, max_workers: int = 4):
        """
        Initialize extension scanner.

        Args:
            max_workers: Maximum number of worker threads
        """
        self.max_workers = max_workers

    def scan(self, code_index, ext_name: str) -> ExtensionScanResults:
        """
        Process files for an extension from a ready CodeFileIndex.

        Args:
            code_index: CodeFileIndex produced by CodeFileIndexer.scan(ext_code_dir)
            ext_name: Extension name for logging

        Returns:
            ExtensionScanResults with collected data
        """
        if code_index is None:
            logger.warning("  [SCANNER] %s: empty code_index passed", ext_name)
            return ExtensionScanResults()

        logger.info("  [SCANNER] Processing extension files from index (root=%s)", code_index.root)

        # Delegate to the streaming session via list-replay (single implementation).
        session = ExtensionScanSession(max_workers=self.max_workers)
        for entry in code_index.form_xml_files:
            session.on_form_xml(entry)
        for p in code_index.predefined_xml_files:
            session.on_predefined_xml(p)
        for p in code_index.help_html_files:
            session.on_help_html(p)
        return session.finish(code_index)
