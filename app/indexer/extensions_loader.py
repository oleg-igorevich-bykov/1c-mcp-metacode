"""
Extensions loader for 1C configuration extensions.

Handles:
- Loading extension metadata
- Building ADOPTED_FROM relationships
- Building EXTENDS relationships
- Analyzing extension properties (classification and extraction)
- Saving properties to Neo4j
"""

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import List, Dict, Any, Tuple
import logging

from parsers.metadata_parser import MetadataParser
from dumpinfo_loader import load_dumpinfo_map
from .code_file_index import CodeFileIndexer, CodeFileScanConsumers
from .extension_scanner import ExtensionScanSession
from .indexing_result import LoadedExtensionSnapshot
from .metadata_loader import MetadataLoader

logger = logging.getLogger(__name__)

_ELEMENT_LABEL_WHITELIST: dict[str, str] = {
    "MetadataObject":    "MetadataObject",
    "TabularSection":    "TabularPart",
    "Attribute":         "Attribute",
    "Resource":          "Resource",
    "Dimension":         "Dimension",
    "EnumValue":         "EnumValue",
    "Column":            "JournalGraph",
    "URLTemplate":       "UrlTemplate",
    "Method":            "UrlMethod",
    "AddressingAttribute": "Attribute",  # в графе хранится как Attribute
}

# Metadata categories for extension analysis
METADATA_CATEGORIES = {
    "Documents", "Catalogs", "Enums", "ChartsOfAccounts",
    "ChartsOfCharacteristicTypes", "ChartsOfCalculationTypes",
    "InformationRegisters", "AccumulationRegisters",
    "AccountingRegisters", "CalculationRegisters",
    "BusinessProcesses", "Tasks", "ExchangePlans",
    "DataProcessors", "Reports", "CommonModules",
    "Subsystems", "Roles"
}

# Properties that have default values in TXT files but should be cleared
# for adopted extension elements if not explicitly modified in XML
EXTENSION_DEFAULT_PROPERTIES = {
    # Document numbering properties (default values)
    "ТипНомера",
    "ДлинаНомера",
    "ДопустимаяДлинаНомера",
    "ПериодичностьНомера",
    "КонтрольУникальности",

    # Catalog/ChartOf* hierarchy properties (default values)
    "Иерархический",
    "ВидИерархии",
    "ДлинаКода",
    "ДлинаНаименования",
    "ТипКода",
    "ДопустимаяДлинаКода",
}


class ExtensionsLoader:
    """Loads 1C extensions and analyzes their properties"""

    def __init__(self, loader, settings):
        """
        Initialize extensions loader.

        Args:
            loader: Neo4jLoader instance
            settings: Settings object
        """
        self.loader = loader
        self.settings = settings
        self.parser = MetadataParser()

    _EMPTY_RESULT = {"count": 0, "ext_pairs": [], "extensions": []}

    def load_extensions(
        self,
        base_config_name: str,
        code_directory: Path,
        callsites_resolver=None,
        base_routines_indexes: list = None,
        base_configuration=None,
    ) -> dict:
        """
        Load all extensions from extensions directory.

        Args:
            base_config_name: Base configuration name
            code_directory: Code directory (not used, extensions_dir from settings)

        Returns:
            Dict {"count": int, "ext_pairs": list[tuple[str, str]]} —
            count = число успешно загруженных расширений,
            ext_pairs = [(ext_graph_config_name, base_config_name), ...]
            для последующего создания EXTENDS_ROUTINE рёбер.
        """
        extensions_dir = self.settings.extensions_directory

        if not extensions_dir.exists():
            logger.info("Extensions directory does not exist: %s", extensions_dir)
            return self._EMPTY_RESULT

        # Find all subdirectories in extensions directory
        ext_dirs = [d for d in extensions_dir.iterdir() if d.is_dir()]

        if not ext_dirs:
            logger.info("No extension directories found in: %s", extensions_dir)
            return self._EMPTY_RESULT

        logger.info("=" * 80)
        logger.info("Loading %d extension(s) from: %s", len(ext_dirs), extensions_dir)
        logger.info("=" * 80)

        extensions_loaded = 0
        ext_pairs: list = []
        ext_snapshots: list = []
        project_name = self.settings.project_name
        base_config_qn = f"{project_name}/{base_config_name}"
        deferred_ext_linking: list[tuple[str, dict]] = []

        metadata_source = getattr(self.settings, "metadata_source", "txt")
        metadata_loader = MetadataLoader()

        for ext_idx, ext_dir in enumerate(ext_dirs, 1):
            ext_dir_name = ext_dir.name
            logger.info("[INDEXER] Loading extension %d/%d: %s", ext_idx, len(ext_dirs), ext_dir_name)

            # Validate structure
            ext_metadata_dir = ext_dir / "metadata"
            ext_code_dir = ext_dir / "code"

            # Guards differ by metadata_source. In TXT mode the legacy checks stay
            # (metadata/ dir + exactly one .txt). In XML mode we only require
            # code/Configuration.xml.
            if metadata_source == "txt":
                if not ext_metadata_dir.exists():
                    logger.warning("  ⊘ Skipping (no metadata directory): %s", ext_dir_name)
                    continue
                txt_files = list(ext_metadata_dir.glob("*.txt"))
                if not txt_files:
                    logger.warning("  ⊘ Skipping (no .txt file in metadata): %s", ext_dir_name)
                    continue
                if len(txt_files) > 1:
                    logger.warning("  ⊘ Skipping (multiple .txt files in metadata): %s", ext_dir_name)
                    continue
            else:  # xml
                if not (ext_code_dir / "Configuration.xml").exists():
                    logger.warning(
                        "  ⊘ Skipping (no code/Configuration.xml in XML mode): %s",
                        ext_dir_name,
                    )
                    continue

            _original_guid_map = getattr(self.loader, '_guid_map', {}).copy()
            ext_scan_session = None
            ext_xml_session = None
            ext_bsl = None
            ext_bsl_finalized = False
            # Per-iteration reset: without this, an extension with no BSL files
            # would inherit the previous extension's bsl_data into its snapshot.
            bsl_data = None
            try:
                # Single os.walk for this extension. Streaming consumers overlap
                # the walk with secondary parse (predef/help) and, in XML mode,
                # with XML metadata descriptor parsing. BSL is collected in the
                # index (include_bsl=False) and queued later, after forms.
                ext_code_index = None
                if ext_code_dir.exists():
                    ext_scan_session = ExtensionScanSession(max_workers=4)
                    if metadata_source == "xml":
                        from xml_metadata import XmlMetadataParseSession
                        from config import resolve_xml_standard_attributes_mode

                        ext_materialize, ext_preserve = resolve_xml_standard_attributes_mode(
                            self.settings.xml_standard_attributes_mode
                        )
                        xml_workers = (
                            getattr(self.settings, "XML_PROCESS_WORKERS", None)
                            or getattr(self.settings, "PROCESS_WORKERS", None)
                            or 4
                        )
                        ext_xml_session = XmlMetadataParseSession(
                            workers=int(xml_workers),
                            materialize_standard_attrs=ext_materialize,
                            preserve_listed_standard_attrs=ext_preserve,
                            root=ext_code_dir,
                            project_name="",
                        )
                        cons = CodeFileScanConsumers(
                            on_metadata_xml=ext_xml_session.submit,
                            on_form_xml=ext_scan_session.on_form_xml,
                            on_predefined_xml=ext_scan_session.on_predefined_xml,
                            on_help_html=ext_scan_session.on_help_html,
                            on_event_subscription_xml=ext_scan_session.on_event_subscription_xml,
                        )
                    else:
                        cons = ext_scan_session.consumers(include_bsl=False)
                    ext_code_index = CodeFileIndexer.scan(ext_code_dir, consumers=cons)

                # Parse extension configuration.
                if metadata_source == "xml":
                    if ext_xml_session is None:
                        logger.warning("  ⊘ Skipping (no code dir for XML extension): %s", ext_dir_name)
                        continue
                    ext_configs = ext_xml_session.finish(is_extension=True)
                    ext_xml_session = None  # finish() shut the pool down
                else:
                    ext_configs = metadata_loader.load_configurations(
                        ext_metadata_dir,
                        ext_code_index,
                        source="txt",
                        is_extension=True,
                    )

                if not ext_configs:
                    logger.warning("  ⊘ Skipping (failed to parse): %s", ext_dir_name)
                    continue

                ext_config = ext_configs[0]
                ext_config_name = ext_config.name

                # XML-only: fill missing properties on adopted objects from the base
                # configuration before loading into Neo4j. Runs after finish() (ownership
                # already stamped) and only when a base Configuration is available.
                if metadata_source == "xml" and base_configuration is not None:
                    from xml_metadata import apply_extension_base_overlay

                    overlay_stats = apply_extension_base_overlay(ext_config, base_configuration)
                    logger.info(
                        "  Extension XML base overlay: objects=%s, object_attrs_props=%s, owner_attrs_added=%s, missing_base_object=%s",
                        overlay_stats.get("objects", 0),
                        overlay_stats.get("object_attrs_props", 0),
                        overlay_stats.get("owner_attrs_added", 0),
                        overlay_stats.get("missing_base_object", 0),
                    )

                total_objects = sum(len(cat.metadata_objects) for cat in ext_config.categories)
                logger.info("  [PARSER] Configuration name: '%s'", ext_config_name)
                logger.info("  [PARSER] Objects: %d in %d categories", total_objects, len(ext_config.categories))

                # Always remember the original name so the restore-block at the
                # end works in both branches without UnboundLocalError.
                original_config_name = ext_config.name

                if metadata_source == "txt":
                    # TXT parser returned the raw name without the marker — add it here.
                    ext_config.name = f"{ext_config_name}$ext$"
                # else: XML parser already returned "<Name>$ext$" thanks to
                # is_extension=True. Avoid double-suffixing.

                ext_graph_config_name = ext_config.name
                ext_config_qn = f"{project_name}/{ext_graph_config_name}"

                logger.info("  [LOADER] Loading as extension (QN: %s)", ext_config_qn)

                # Use extension-only GUID map so adopted nodes never inherit base GUIDs
                if getattr(self.settings, 'load_metadata_guids', True):
                    try:
                        _ext_guid_map = load_dumpinfo_map(ext_code_dir) if ext_code_dir.exists() else {}
                        if _ext_guid_map:
                            self.loader.set_guid_map(_ext_guid_map)
                            logger.info("  [GUID] Loaded %d GUID entries for extension", len(_ext_guid_map))
                        else:
                            self.loader.set_guid_map({})
                            logger.debug("  [GUID] No ConfigDumpInfo.xml for extension, GUID map disabled")
                    except Exception as _guid_e:
                        logger.debug("  [GUID] Extension GUID map not loaded: %s", _guid_e)

                with self.loader.driver.session(database=self.settings.neo4j_database) as session:
                    try:
                        self.loader._load_configuration(
                            session,
                            project_name,
                            ext_config,
                            is_extension=True
                        )
                    finally:
                        self.loader.set_guid_map(_original_guid_map)

                    # Build ADOPTED_FROM relationships (inside session)
                    logger.info("  [LOADER] Building ADOPTED_FROM relationships...")
                    try:
                        from graphdb.extension_relationships_builder import ExtensionRelationshipsBuilder

                        rel_builder = ExtensionRelationshipsBuilder(self.loader)
                        adopted_from_stats = rel_builder.build_adopted_from_for_extension(
                            ext_config_qn,
                            base_config_qn
                        )

                        total_adopted = sum(adopted_from_stats.values())
                        logger.info("  [LOADER] ✓ ADOPTED_FROM created: %d", total_adopted)

                    except Exception as oe:
                        logger.error("  [LOADER] ✗ Failed to build ADOPTED_FROM: %s", str(oe))

                    # Process extension files (using the CodeFileIndex built above).
                    # Runs INSIDE the Neo4j session because subsequent steps need to update nodes.
                    if ext_code_index is not None and ext_scan_session is not None:
                        logger.info("  [SCANNER] Processing extension files from CodeFileIndex...")
                        try:
                            # Drain the streaming session started during the walk
                            # (predef/help parsed there; forms/events/bsl collected).
                            scan_results = ext_scan_session.finish(ext_code_index)
                            ext_scan_session = None

                            # 1. Analyze and save property values from XML files
                            if scan_results.xml_files_for_analysis:
                                logger.info("  [ANALYZER] Analyzing %d XML files...", len(scan_results.xml_files_for_analysis))
                                classification_results, extraction_results = self._analyze_xml_files(
                                    scan_results.xml_files_for_analysis
                                )

                                # Сохраняем результаты в Neo4j
                                if classification_results or extraction_results:
                                    self._save_extension_analysis_results(
                                        session,
                                        project_name,
                                        ext_config_qn,
                                        classification_results,
                                        extraction_results
                                    )
                                    logger.info("  [ANALYZER] ✓ Extension analysis saved to Neo4j")
                            else:
                                logger.info("  [ANALYZER] No metadata XML files found for analysis")

                            # 2. Load predefined values
                            if scan_results.predef_data.items:
                                logger.info("  [LOADER] Loading predefined values...")
                                self.loader.load_predefined(
                                    scan_results.predef_data.items,
                                    scan_results.predef_data.relations,
                                    project_name,
                                    ext_config.name  # Contains $ext$ marker (was set on line 143)
                                )
                                logger.info("  [LOADER] ✓ Loaded %d predefined items", len(scan_results.predef_data.items))
                                try:
                                    predef_adopted = rel_builder._build_adopted_from_for_type(
                                        "PredefinedItem",
                                        ext_config.name,
                                        base_config_name,
                                    )
                                    logger.info("  [LOADER] ✓ PredefinedItem ADOPTED_FROM: %d", predef_adopted)
                                except Exception as predef_e:
                                    logger.error("  [LOADER] ✗ PredefinedItem ADOPTED_FROM failed: %s", predef_e)

                            # 3. Load extension forms
                            if scan_results.form_files:
                                logger.info("  [FORMS] Processing %d form files...", len(scan_results.form_files))
                                try:
                                    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
                                    from parsers.form_xml_parser import FormXmlParser
                                    from .workers import worker_extension_form
                                    from .forms_processor import FormsProcessor
                                    from .data_structures import FormsData

                                    fparser = FormXmlParser()
                                    proc = FormsProcessor()
                                    ext_forms_data = FormsData()
                                    hash_rows: List[Dict] = []

                                    with _TPE(max_workers=min(4, __import__('os').cpu_count() or 4)) as executor:
                                        futures = {
                                            executor.submit(
                                                worker_extension_form,
                                                form_xml_path, is_adopted,
                                                ext_config.name, ext_code_dir, project_name, fparser,
                                                ext_config,
                                            ): None
                                            for form_xml_path, _, is_adopted in scan_results.form_files
                                        }
                                        for fut in _ac(futures):
                                            result = fut.result()
                                            if result:
                                                proc.merge_form_result(ext_forms_data, result)
                                                if result.get("base_form_hash"):
                                                    hash_rows.append({
                                                        "form_qn": result["form_qn"],
                                                        "form_content_hash": None,
                                                        "base_form_hash": result["base_form_hash"],
                                                    })

                                    if proc.has_data(ext_forms_data):
                                        self.loader.load_form_definitions(ext_forms_data.to_dict())
                                        logger.info("  [FORMS] ✓ Extension forms loaded")

                                    if hash_rows:
                                        self.loader.update_form_hashes(hash_rows)

                                    from graphdb.extension_relationships_builder import ExtensionRelationshipsBuilder as _ERB
                                    _form_rel_builder = _ERB(self.loader)

                                    ctrl_count = _form_rel_builder.build_adopted_from_for_formcontrols(
                                        ext_config.name,
                                        base_config_name,
                                    )
                                    logger.info("  [FORMS] ✓ FormControl ADOPTED_FROM: %d", ctrl_count)

                                    attr_count = _form_rel_builder.build_adopted_from_for_formattributes(
                                        ext_config.name,
                                        base_config_name,
                                    )
                                    logger.info("  [FORMS] ✓ FormAttribute ADOPTED_FROM: %d", attr_count)

                                    cmd_count = _form_rel_builder.build_adopted_from_for_formcommands(
                                        ext_config.name,
                                        base_config_name,
                                    )
                                    logger.info("  [FORMS] ✓ FormCommand ADOPTED_FROM: %d", cmd_count)

                                    evt_count = _form_rel_builder.build_adopted_from_for_formevents(
                                        ext_config.name,
                                        base_config_name,
                                    )
                                    logger.info("  [FORMS] ✓ FormEvent ADOPTED_FROM: %d", evt_count)

                                    action_count = _form_rel_builder.build_extends_action_for_formevent_actions(
                                        ext_config.name,
                                        base_config_name,
                                    )
                                    logger.info("  [FORMS] ✓ FormEventAction EXTENDS_ACTION: %d", action_count)

                                except Exception as e:
                                    logger.error("  [FORMS] ✗ Failed: %s", str(e), exc_info=True)

                            # 4. Scan extension BSL modules via multiprocessing (mirrors main config flow)
                            # Workers start after forms are loaded — ensures Form nodes exist before
                            # HAS_MODULE links are written by worker micro-batch flushes.
                            form_routines: dict = {}
                            ext_routines_index: list = []
                            ext_callsites: list = []
                            try:
                                from .bsl_processor import BSLProcessor

                                # BSL inputs = .bsl files + Form.bin files (the latter
                                # was missing in the legacy ExtensionScanner; CodeFileIndex
                                # now closes that gap).
                                bsl_files = scan_results.bsl_files
                                form_bin_files = list(getattr(ext_code_index, "form_bin_files", []))
                                if bsl_files or form_bin_files:
                                    ext_bsl = BSLProcessor()
                                    ext_bsl.start_workers(
                                        ext_code_dir, project_name, ext_config.name, self.settings
                                    )
                                    for f in bsl_files:
                                        ext_bsl.queue_file(f)
                                    for f in form_bin_files:
                                        ext_bsl.queue_file(f)
                                    bsl_data = ext_bsl.finalize(self.settings)
                                    ext_bsl_finalized = True

                                    form_routines = bsl_data.form_routines
                                    ext_routines_index = bsl_data.routines_indexes
                                    ext_callsites = bsl_data.callsites
                                    logger.info("  [BSL] ✓ %s: BSL parsed via multiprocessing", ext_config.name)
                                else:
                                    logger.info("  [BSL] No BSL files found in extension code directory")

                            except Exception as bsl_stage_e:
                                logger.error("  [BSL] ✗ Failed BSL scan: %s", str(bsl_stage_e), exc_info=True)

                            # deferred linking always runs — form events must be linked even if CALLS fails
                            deferred_ext_linking.append((ext_config.name, form_routines))

                            # 4b. Resolve callsites and load CALLS edges for this extension
                            if callsites_resolver is not None and (ext_callsites or ext_routines_index):
                                try:
                                    combined_indexes = ext_routines_index + list(base_routines_indexes or [])
                                    call_rows, sorted_callers = callsites_resolver.resolve_calls(
                                        combined_indexes,
                                        ext_callsites,
                                        project_name,
                                    )
                                    if sorted_callers:
                                        self.loader.load_bsl_calls(project_name, call_rows, sorted_callers)
                                        logger.info(
                                            "  [BSL] ✓ CALLS loaded for %s: callers=%d, edges=%d",
                                            ext_config.name, len(sorted_callers), len(call_rows),
                                        )
                                    else:
                                        logger.info("  [BSL] No CALLS resolved for %s", ext_config.name)
                                except Exception as calls_e:
                                    logger.error(
                                        "  [BSL] ✗ CALLS resolution failed for %s: %s",
                                        ext_config.name, str(calls_e), exc_info=True,
                                    )

                            # 5. Load extension EventSubscriptions
                            if scan_results.event_subscription_files and getattr(self.settings, 'load_event_subscriptions', True):
                                logger.info("  [EVENTS] Processing %d EventSubscription files...", len(scan_results.event_subscription_files))
                                try:
                                    from parsers.event_subscription_parser import EventSubscriptionParser

                                    ev_parser = EventSubscriptionParser()
                                    subscriptions = []
                                    for xml_path in scan_results.event_subscription_files:
                                        parsed = ev_parser.parse_file(xml_path)
                                        if parsed:
                                            subscriptions.append(parsed)

                                    if subscriptions:
                                        self.loader.load_event_subscriptions(
                                            subscriptions, project_name, ext_config.name
                                        )
                                        self.loader.link_event_subscriptions_to_handlers(
                                            project_name, ext_config.name
                                        )
                                        logger.info("  [EVENTS] ✓ Loaded %d event subscriptions", len(subscriptions))

                                        ev_adopted = rel_builder.build_adopted_from_for_eventsubscriptions(
                                            ext_config_qn, base_config_qn
                                        )
                                        logger.info("  [EVENTS] ✓ EventSubscription ADOPTED_FROM: %d", ev_adopted)

                                except Exception as ev_e:
                                    logger.error("  [EVENTS] ✗ Failed: %s", str(ev_e), exc_info=True)

                            # 6. Load extension Role Rights
                            if getattr(self.settings, 'load_role_rights', False):
                                try:
                                    from parsers.role_rights_parser import RoleRightsParser

                                    rr_rows = RoleRightsParser().parse_files(
                                        ext_code_index.rights_xml_files,
                                        project_name,
                                        ext_config.name,
                                    )
                                    if rr_rows:
                                        self.loader.load_role_rights_targets_ext(
                                            rr_rows,
                                            ext_config.name,
                                            base_config_name,
                                        )
                                        logger.info("  [RIGHTS] ✓ Loaded role rights: %d rows", len(rr_rows))
                                    else:
                                        logger.info("  [RIGHTS] No Rights.xml found in extension")
                                except Exception as rr_e:
                                    logger.error("  [RIGHTS] ✗ Failed: %s", str(rr_e), exc_info=True)

                            # 7. Load Help content (Справка)
                            if getattr(self.settings, 'load_help_from_html', True) and scan_results.help_data.help_by_object:
                                logger.info("  [HELP] Loading help content for %d objects...", len(scan_results.help_data.help_by_object))
                                try:
                                    self.loader.load_help_content(
                                        scan_results.help_data.help_by_object,
                                        project_name,
                                        ext_config.name,
                                    )
                                    logger.info("  [HELP] ✓ Help content loaded")
                                except Exception as help_e:
                                    logger.error("  [HELP] ✗ Failed: %s", str(help_e), exc_info=True)

                        except Exception as e:
                            logger.error("  [SCANNER] ✗ Failed to scan/analyze extension: %s", str(e), exc_info=True)

                # Snapshot для incremental baseline (см. план §1).
                # Critical: snapshot.ext_graph_config_name содержит $ext$, а
                # snapshot.configuration.name мы deepcopy чтобы baseline init
                # не зависел от последующего восстановления raw имени в
                # `ext_config.name = original_config_name` ниже.
                # deepcopy объекта Configuration с категориями/объектами безопасен —
                # parsed model не содержит open file handles или DB сессий.
                try:
                    ext_config_for_snapshot = deepcopy(ext_config)
                except Exception as _copy_e:
                    logger.warning(
                        "Extension snapshot deepcopy failed for %s: %s — using by-reference",
                        ext_dir_name, _copy_e,
                    )
                    ext_config_for_snapshot = ext_config
                ext_snapshots.append(LoadedExtensionSnapshot(
                    ext_dir_name=ext_dir_name,
                    ext_graph_config_name=ext_graph_config_name,
                    base_config_name=base_config_name,
                    source=metadata_source,
                    ext_metadata_dir=ext_metadata_dir if metadata_source == "txt" else None,
                    ext_code_dir=ext_code_dir if ext_code_dir.exists() else None,
                    ext_code_index=ext_code_index if metadata_source == "xml" else None,
                    configuration=ext_config_for_snapshot,
                    bsl_data=bsl_data,
                ))

                # Restore original name
                ext_config.name = original_config_name

                # Create EXTENDS link
                self.loader.create_extends_link(ext_config_qn, base_config_qn)

                extensions_loaded += 1
                # Сохраняем стабильное имя ДО restore — после restore имя уже без $ext$
                ext_pairs.append((ext_graph_config_name, base_config_name))
                logger.info("  ✓ Extension loaded successfully: %s", ext_dir_name)
                logger.info("-" * 60)

            except Exception as e:
                self.loader.set_guid_map(_original_guid_map)
                logger.error("  ✗ Failed to load extension %s: %s", ext_dir_name, str(e))
                continue
            finally:
                # Cleanup long-lived pools for this extension (idempotent on success).
                if ext_xml_session is not None:
                    ext_xml_session.shutdown()
                if ext_scan_session is not None:
                    ext_scan_session.shutdown()
                if ext_bsl is not None and not ext_bsl_finalized:
                    ext_bsl.terminate_workers()

        logger.info("=" * 80)
        logger.info("Successfully loaded %d/%d extensions", extensions_loaded, len(ext_dirs))
        logger.info("=" * 80)

        if deferred_ext_linking:
            try:
                self.loader.run_deferred_extensions_linking(project_name, deferred_ext_linking)
            except Exception as e:
                logger.error("Deferred extension linking failed: %s", e, exc_info=True)

        return {
            "count": extensions_loaded,
            "ext_pairs": ext_pairs,
            "extensions": ext_snapshots,
        }

    def _analyze_xml_files(
        self,
        xml_files_to_analyze: List[Path]
    ) -> Tuple[List[Tuple[Path, Any]], List[Tuple[Path, Any]]]:
        """
        Analyze extension properties from a list of XML files.

        This method is used after ExtensionScanner collects XML files in a single pass.

        Args:
            xml_files_to_analyze: List of XML file paths to analyze

        Returns:
            Tuple of (classification_results, extraction_results)
        """
        from extension_properties_classifier import ExtensionPropertiesClassifier
        from extension_properties_extractor import ExtensionPropertiesExtractor
        from concurrent.futures import ThreadPoolExecutor as _TXML, as_completed as _xml_ac
        import os as _os

        def _analyze_one(xml_file: Path):
            cls = ExtensionPropertiesClassifier()
            ext = ExtensionPropertiesExtractor()
            return xml_file, cls.analyze_metadata_xml(xml_file), ext.extract_from_xml(xml_file)

        classification_results = []
        extraction_results = []

        _xml_workers = min(4, _os.cpu_count() or 4)
        with _TXML(max_workers=_xml_workers) as _xml_executor:
            _xml_futures = {_xml_executor.submit(_analyze_one, f): f for f in xml_files_to_analyze}
            for fut in _xml_ac(_xml_futures):
                try:
                    xml_file, classification, extraction = fut.result()
                except Exception as e:
                    logger.warning("  [ANALYZER] Error analyzing %s: %s", _xml_futures[fut].name, e)
                    continue

                if classification and classification.elements:
                    if any(e.is_adopted for e in classification.elements):
                        classification_results.append((xml_file, classification))

                if extraction and extraction.elements:
                    if any(e.is_adopted for e in extraction.elements):
                        extraction_results.append((xml_file, extraction))

        logger.info("  [ANALYZER] Analyzed %d files", len(xml_files_to_analyze))
        logger.info("  [CLASSIFIER] Found %d objects with adopted elements (classification)", len(classification_results))
        logger.info("  [EXTRACTOR] Found %d objects with property values", len(extraction_results))

        return classification_results, extraction_results

    def _save_extension_analysis_results(
        self,
        session,
        project_name: str,
        ext_config_qn: str,
        classification_results: List[Tuple[Path, Any]],
        extraction_results: List[Tuple[Path, Any]]
    ):
        """
        Сохраняет результаты анализа расширения в Neo4j.

        Args:
            session: Neo4j session
            project_name: Имя проекта
            ext_config_qn: Qualified name расширения
            classification_results: Результаты классификации свойств
            extraction_results: Результаты извлечения значений свойств
        """
        # 1. Сохранить классификацию (controlled_properties, modified_properties)
        if classification_results:
            self._save_properties_classification(session, project_name, ext_config_qn, classification_results)

        # 2. Сохранить значения свойств
        if extraction_results:
            self._save_property_values(session, project_name, ext_config_qn, extraction_results)

    def _save_properties_classification(
        self,
        session,
        project_name: str,
        ext_config_qn: str,
        analysis_results: List[Tuple[Path, Any]]
    ):
        """
        Сохраняет результаты классификации свойств в Neo4j.

        Args:
            session: Neo4j session
            project_name: Имя проекта
            ext_config_qn: Qualified name расширения (с маркером $ext$)
            analysis_results: Список кортежей (xml_file_path, ObjectAnalysisResult)
        """
        total_elements = 0
        rows_by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for xml_file, obj_result in analysis_results:
            for element in obj_result.elements:
                total_elements += 1

                if not element.is_adopted:
                    continue

                label = _ELEMENT_LABEL_WHITELIST.get(element.element_type)
                if not label:
                    logger.warning("Unknown element type: %s", element.element_type)
                    continue

                category_ru = self._get_category_ru(obj_result.object_type)

                # Особый случай: Subsystem — QN строится по пути файла для вложенных подсистем
                if obj_result.object_type == "Subsystem" and obj_result.xml_path:
                    parts = obj_result.xml_path.parts
                    code_idx = next((i for i, p in enumerate(parts) if p == "code"), None)
                    if code_idx is not None:
                        chain = [p for p in parts[code_idx + 1:-1] if p != "Subsystems"]
                        chain.append(obj_result.xml_path.stem)
                        base_qn = f"{ext_config_qn}/Подсистемы/" + "/".join(chain)
                    else:
                        base_qn = f"{ext_config_qn}/{category_ru}/{obj_result.object_name}"
                else:
                    base_qn = f"{ext_config_qn}/{category_ru}/{obj_result.object_name}"

                if element.element_type == "MetadataObject":
                    element_qn = base_qn
                elif element.element_type == "TabularSection":
                    element_qn = f"{base_qn}/TabularPart/{element.element_name}"
                elif element.element_type == "Attribute":
                    if element.parent_name:
                        element_qn = f"{base_qn}/TabularPart/{element.parent_name}/Attribute/{element.element_name}"
                    else:
                        element_qn = f"{base_qn}/Attribute/{element.element_name}"
                elif element.element_type == "Resource":
                    element_qn = f"{base_qn}/Resource/{element.element_name}"
                elif element.element_type == "Dimension":
                    element_qn = f"{base_qn}/Dimension/{element.element_name}"
                elif element.element_type == "EnumValue":
                    element_qn = f"{base_qn}/EnumValue/{element.element_name}"
                elif element.element_type == "Column":
                    element_qn = f"{base_qn}/Graph/{element.element_name}"
                elif element.element_type == "URLTemplate":
                    element_qn = f"{base_qn}/UrlTemplate/{element.element_name}"
                elif element.element_type == "Method":
                    element_qn = f"{base_qn}/UrlTemplate/{element.parent_name}/Method/{element.element_name}"
                elif element.element_type == "AddressingAttribute":
                    element_qn = f"{base_qn}/Attribute/{element.element_name}"

                rows_by_label[label].append({
                    "qn": element_qn,
                    "controlled": element.controlled_properties,
                    "modified": element.modified_properties,
                })

        if not rows_by_label:
            logger.info("  [CLASSIFIER] No elements to classify")
            return

        rows_count = sum(len(v) for v in rows_by_label.values())
        logger.info("  [CLASSIFIER] Updating %d elements with property classification...", rows_count)

        update_count = 0
        not_found_count = 0
        batch_size = self.settings.neo4j_batch_size

        for label, label_rows in rows_by_label.items():
            cypher = f"""
            UNWIND $rows AS row
            MATCH (n:{label} {{qualified_name: row.qn}})
            SET n.controlled_properties = row.controlled,
                n.modified_properties = row.modified
            RETURN count(n) AS updated
            """
            for i in range(0, len(label_rows), batch_size):
                batch = label_rows[i:i + batch_size]
                try:
                    result = session.run(cypher, {"rows": batch})
                    updated = result.single()["updated"]
                    update_count += updated
                    not_found_count += len(batch) - updated
                except Exception as e:
                    logger.error("  [CLASSIFIER] Batch update failed: %s", str(e))

        logger.info(f"  [CLASSIFIER] Updated {update_count} elements with property classification")
        if not_found_count > 0:
            logger.warning(f"  [CLASSIFIER] ⚠ {not_found_count} adopted elements not found in DB (total analyzed: {total_elements})")

    def _save_property_values(
        self,
        session,
        project_name: str,
        ext_config_qn: str,
        extraction_results: List[Tuple[Path, Any]]
    ):
        """
        Сохраняет значения свойств из XML в Neo4j (батчами).

        Обновляет только пустые свойства заимствованных элементов.

        Args:
            session: Neo4j session
            project_name: Имя проекта
            ext_config_qn: Qualified name расширения
            extraction_results: Результаты извлечения свойств
        """
        # Собираем все элементы для обновления
        update_rows = []

        for xml_file, obj_result in extraction_results:
            for element in obj_result.elements:
                if not element.is_adopted:
                    continue

                label = _ELEMENT_LABEL_WHITELIST.get(element.element_type)
                if not label:
                    logger.warning("Unknown element type: %s", element.element_type)
                    continue

                element_qn = self._build_element_qn(
                    ext_config_qn,
                    obj_result.object_type,
                    obj_result.object_name,
                    element,
                    xml_path=getattr(obj_result, "xml_path", None)
                )

                if not element_qn:
                    continue

                properties_from_xml = set(element.property_values.keys()) if element.property_values else set()
                # In XML metadata mode the TXT-only defaults (ТипНомера, ДлинаКода,
                # Иерархический, ...) are never injected by XmlMetadataParser, so the
                # cleanup must be disabled — otherwise _execute_property_values_batch
                # would create empty placeholders for keys that did not exist.
                if getattr(self.settings, "metadata_source", "txt") == "xml":
                    properties_to_clear = []
                else:
                    properties_to_clear = list(EXTENSION_DEFAULT_PROPERTIES - properties_from_xml)

                if element.property_values or properties_to_clear:
                    update_rows.append({
                        "qn": element_qn,
                        "label": label,
                        "properties": element.property_values or {},
                        "properties_to_clear": properties_to_clear,
                    })

        if not update_rows:
            logger.info("  [EXTRACTOR] No property values to update")
            return

        logger.info("  [EXTRACTOR] Updating %d elements with property values...", len(update_rows))

        batch_size = self.settings.neo4j_batch_size
        total_updated = 0
        total_not_found = 0

        rows_by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in update_rows:
            rows_by_label[row["label"]].append(row)

        for label, label_rows in rows_by_label.items():
            for i in range(0, len(label_rows), batch_size):
                batch = label_rows[i:i + batch_size]
                updated, not_found = self._execute_property_values_batch(session, batch, label)
                total_updated += updated
                total_not_found += not_found

        logger.info("  [EXTRACTOR] ✓ Updated %d elements", total_updated)
        if total_not_found > 0:
            logger.warning("  [EXTRACTOR] ⚠ %d elements not found in DB", total_not_found)

    def _execute_property_values_batch(self, session, batch: List[Dict[str, Any]], label: str) -> Tuple[int, int]:
        """
        Выполняет батчевое обновление свойств элементов.

        Args:
            session: Neo4j session
            batch: Список {qn: str, properties: Dict[str, Any], properties_to_clear: List[str]}

        Returns:
            (updated_count, not_found_count)
        """
        if label not in _ELEMENT_LABEL_WHITELIST.values():
            raise ValueError(f"Unexpected Neo4j label: {label!r}")

        cypher = f"""
        UNWIND $rows AS row
        MATCH (n:{label} {{qualified_name: row.qn}})

        // 1. Обновляем свойства из XML
        WITH n, row
        CALL {{
            WITH n, row
            UNWIND keys(row.properties) AS propKey
            WITH n, propKey, row.properties[propKey] AS propValue

            WITH n, propKey, propValue,
                 CASE
                    WHEN n[propKey] IS NULL THEN true
                    WHEN n[propKey] = '' THEN true
                    WHEN n[propKey] = [] THEN true
                    ELSE (propValue IS NOT NULL AND propValue <> '' AND propValue <> [])
                 END AS shouldUpdate

            FOREACH (_ IN CASE WHEN shouldUpdate THEN [1] ELSE [] END |
                SET n[propKey] = propValue
            )
        }}

        // 2. Очищаем свойства со значениями по умолчанию (которых нет в XML)
        WITH n, row
        CALL {{
            WITH n, row
            WITH n, row.properties_to_clear AS clearProps
            WHERE clearProps IS NOT NULL AND size(clearProps) > 0
            UNWIND clearProps AS clearKey
            WITH n, clearKey
            FOREACH (_ IN [1] |
                SET n[clearKey] = ''
            )
        }}

        RETURN n.qualified_name AS qn
        """

        try:
            result = session.run(cypher, rows=batch)

            updated_count = 0
            for record in result:
                updated_count += 1

            not_found_count = len(batch) - updated_count

            return updated_count, not_found_count

        except Exception as e:
            logger.error("  [EXTRACTOR] Batch update failed: %s", str(e))
            return 0, len(batch)

    def _build_element_qn(
        self,
        ext_config_qn: str,
        object_type_en: str,
        object_name: str,
        element,
        xml_path=None
    ) -> str:
        """
        Строит qualified_name для элемента.

        Args:
            ext_config_qn: QN расширения
            object_type_en: Тип объекта (английский)
            object_name: Имя объекта
            element: Элемент
            xml_path: Путь к XML файлу (нужен для Subsystem path-based QN)

        Returns:
            qualified_name элемента
        """
        category_ru = self._get_category_ru(object_type_en)

        # Subsystem: QN по иерархии пути файла для вложенных подсистем
        if object_type_en == "Subsystem" and xml_path is not None:
            parts = xml_path.parts
            code_idx = next((i for i, p in enumerate(parts) if p == "code"), None)
            if code_idx is not None:
                chain = [p for p in parts[code_idx + 1:-1] if p != "Subsystems"]
                chain.append(xml_path.stem)
                base_qn = f"{ext_config_qn}/Подсистемы/" + "/".join(chain)
            else:
                base_qn = f"{ext_config_qn}/{category_ru}/{object_name}"
        else:
            base_qn = f"{ext_config_qn}/{category_ru}/{object_name}"

        if element.element_type == "MetadataObject":
            return base_qn
        elif element.element_type == "TabularSection":
            return f"{base_qn}/TabularPart/{element.element_name}"
        elif element.element_type == "Attribute":
            if element.parent_name:
                return f"{base_qn}/TabularPart/{element.parent_name}/Attribute/{element.element_name}"
            else:
                return f"{base_qn}/Attribute/{element.element_name}"
        elif element.element_type == "Resource":
            return f"{base_qn}/Resource/{element.element_name}"
        elif element.element_type == "Dimension":
            return f"{base_qn}/Dimension/{element.element_name}"
        elif element.element_type == "EnumValue":
            return f"{base_qn}/EnumValue/{element.element_name}"
        elif element.element_type == "Column":
            return f"{base_qn}/Graph/{element.element_name}"
        elif element.element_type == "URLTemplate":
            return f"{base_qn}/UrlTemplate/{element.element_name}"
        elif element.element_type == "Method":
            return f"{base_qn}/UrlTemplate/{element.parent_name}/Method/{element.element_name}"
        elif element.element_type == "AddressingAttribute":
            return f"{base_qn}/Attribute/{element.element_name}"
        else:
            logger.warning(f"Unknown element type: {element.element_type}")
            return ""

    @staticmethod
    def _build_routines_index(routines: list) -> list:
        index = []
        for r in routines:
            params = r.get("params_json") or []
            min_arity = 0
            for p in params:
                if isinstance(p, dict):
                    default_present = p.get("default_present", False)
                    markers = p.get("markers_raw", [])
                    is_optional = default_present or any(
                        "необязатель" in str(m).casefold() for m in markers
                    )
                    if not is_optional:
                        min_arity += 1
                else:
                    min_arity += 1
            index.append({
                "id": r["id"],
                "name": r["name"],
                "module_id": r.get("module_id"),
                "owner_qn": r.get("owner_qn"),
                "module_type": r.get("module_type"),
                "min_arity": min_arity,
                "max_arity": len(params),
                "directives": r.get("directives", []),
                "export": r.get("export", False),
                "params_json": params,
            })
        return index

    @staticmethod
    def _get_category_ru(object_type_en: str) -> str:
        """
        Переводит тип объекта метаданных с английского на русский.

        Args:
            object_type_en: Тип объекта на английском

        Returns:
            Тип объекта на русском
        """
        mapping = {
            "Document": "Документы",
            "Catalog": "Справочники",
            "ChartOfAccounts": "ПланыСчетов",
            "ChartOfCharacteristicTypes": "ПланыВидовХарактеристик",
            "ChartOfCalculationTypes": "ПланыВидовРасчета",
            "InformationRegister": "РегистрыСведений",
            "AccumulationRegister": "РегистрыНакопления",
            "AccountingRegister": "РегистрыБухгалтерии",
            "CalculationRegister": "РегистрыРасчета",
            "BusinessProcess": "БизнесПроцессы",
            "Task": "Задачи",
            "ExchangePlan": "ПланыОбмена",
            "Enum": "Перечисления",
            "DataProcessor": "Обработки",
            "Report": "Отчеты",
            "CommonModule": "ОбщиеМодули",
            "CommonCommand": "ОбщиеКоманды",
            "Constant": "Константы",
            "DocumentJournal": "ЖурналыДокументов",
            "HTTPService": "HTTPСервисы",
            "WebService": "WebСервисы",
            "XDTOPackage": "ПакетыXDTO",
            "CommonTemplate": "ОбщиеМакеты",
            "CommonPicture": "ОбщиеКартинки",
            "StyleItem": "ЭлементыСтиля",
            "FunctionalOption": "ФункциональныеОпции",
            "FunctionalOptionsParameter": "ПараметрыФункциональныхОпций",
            "DefinedType": "ОпределяемыеТипы",
            "Language": "Языки",
            "Role": "Роли",
            "Subsystem": "Подсистемы",
            "CommandGroup": "ГруппыКоманд",
            "CommonAttribute": "ОбщиеРеквизиты",
            "FilterCriterion": "КритерииОтбора",
            "ScheduledJob": "РегламентныеЗадания",
            "SessionParameter": "ПараметрыСеанса",
            "SettingsStorage": "ХранилищаНастроек",
        }
        return mapping.get(object_type_en, object_type_en)
