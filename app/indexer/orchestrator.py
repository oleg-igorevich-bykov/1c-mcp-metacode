"""
Indexer orchestrator - coordinates all indexing stages.

This is the main coordinator that manages the entire indexing process.
"""

from pathlib import Path
from typing import Optional
import logging
import time
from contextlib import contextmanager

from config import settings
from neo4j_loader import Neo4jLoader
from neo4j_retry import is_transient_neo4j_error
from mcpsrv import index_progress

from .indexing_result import IndexingResult
from .metadata_loader import MetadataLoader
from .scanner import DirectoryScanSession
from .code_file_index import CodeFileIndexer, CodeFileScanConsumers
from .forms_processor import FormsProcessor
from .predefined_processor import PredefinedProcessor
from .help_processor import HelpProcessor
from .bsl_processor import BSLProcessor
from .callsites_resolver import CallsitesResolver
from .role_rights_processor import RoleRightsProcessor
from .extensions_loader import ExtensionsLoader
from .statistics import IndexingStatistics
from .data_structures import (
    ProcessingConfig,
    FormsData,
    PredefinedData,
    HelpData,
)
from runtime_memory import format_mem_snapshot, format_run_summary, reset_peak

logger = logging.getLogger(__name__)


@contextmanager
def _stage_timer(name: str):
    """Log wall-clock duration and memory snapshot of a pipeline stage.

    process_hwm_stage is per-stage only when the HWM reset is supported
    (see runtime_memory); cgroup_peak_global is cumulative for the container.
    """
    start = time.perf_counter()
    reset_peak()
    try:
        yield
    finally:
        logger.info("[TIMING] %s: %.2fs", name, time.perf_counter() - start)
        mem = format_mem_snapshot()
        if mem:
            logger.info("[MEM] %s: %s", name, mem)


class IndexerOrchestrator:
    """
    Main orchestrator for the indexing process.

    Coordinates:
    1. Metadata loading (base configuration)
    2. Directory scanning (streaming)
    3. Forms processing
    4. Predefined values
    5. BSL processing (multi-process)
    6. Callsites resolution
    7. Event subscriptions
    8. Role rights
    9. Extensions loading
    10. Statistics display
    """

    def __init__(self):
        """Initialize the orchestrator"""
        self.loader = Neo4jLoader()

        # Component processors
        self.metadata_loader = MetadataLoader()
        self.forms_processor = FormsProcessor()
        self.predefined_processor = PredefinedProcessor()
        self.help_processor = HelpProcessor()
        self.bsl_processor = BSLProcessor()
        self.callsites_resolver = CallsitesResolver()
        self.role_rights_processor = RoleRightsProcessor()
        self.extensions_loader = ExtensionsLoader(self.loader, settings)
        self.statistics = IndexingStatistics(self.loader)

    def run_indexing(
        self, directory: Optional[Path] = None, clear_db: bool = False
    ) -> IndexingResult:
        """
        Run the complete indexing process.

        Returns:
            IndexingResult — contains success flag + parsed configurations + code_index
            for downstream consumers (e.g., incremental loading baseline init in main.py).
        """
        # Use provided directory or default from config
        metadata_dir = directory or settings.metadata_directory
        metadata_source = getattr(settings, "metadata_source", "txt")
        _captured_configurations: list = []
        _captured_code_index = None

        logger.info("Starting metadata indexing from: %s", metadata_dir)
        logger.info("=" * 80)

        # BSL worker lifecycle flags — hoisted so the outer finally can guarantee
        # workers are terminated even if an exception occurs after they started.
        bsl_started = False
        bsl_finalized = False

        # TASK-index-progress.md: mark the bootstrap phase for the anonymous
        # GET /api/console/metrics/index Prometheus endpoint. processed_getter
        # reads the BSL processor's already-maintained parsed-file counter live
        # (no push needed) — BSL parsing is typically the longest-running part
        # of a full/initial load, so this is the most useful live counter
        # available; total is left unknown (streaming discovery finds files
        # during the scan, so there is no upfront total to report).
        index_progress.begin_phase(
            "loading_metadata",
            processed_getter=lambda: self.bsl_processor.bsl_parsed_count,
        )

        try:
            metadata_source = getattr(settings, "metadata_source", "txt")
            logger.info("Metadata source: %s", metadata_source)

            # =================================================================
            # STAGE 1: Clear database (validation moved into load_configurations)
            # =================================================================
            if clear_db:
                logger.info("Clearing existing data for project: %s", settings.project_name)
                self.loader.clear_project(settings.project_name)
                logger.info("Project data cleared successfully")
                logger.info("-" * 60)

            # =================================================================
            # STAGE 2: Load GUID map (optional)
            # =================================================================
            guid_map = self.metadata_loader.load_guid_map(
                settings.code_directory,
                settings.load_metadata_guids
            )

            # Pass GUID map to loader for enrichment
            try:
                self.loader.set_guid_map(guid_map)
            except Exception:
                # Older loader versions may not have this method; ignore gracefully
                pass

            # =================================================================
            # STAGE 3+4: Streaming metadata + secondary scan (single os.walk).
            # The walk overlaps with XML metadata parsing (XML mode) or with
            # secondary parsing + BSL queueing (TXT mode). Write order into Neo4j
            # is unchanged — only parse timing moves earlier.
            # =================================================================
            proc_config = ProcessingConfig.from_settings(settings)

            forms_data = FormsData()
            predef_data = PredefinedData()
            help_data = HelpData()
            formbin_results = []
            event_subscriptions = []
            bsl_data = None

            scan_session: Optional[DirectoryScanSession] = None
            xml_session = None
            # Base Configuration captured in the XML branch; stays None for txt so the
            # shared extensions block below can pass it unconditionally.
            base_configuration = None

            try:
                if metadata_source == "xml":
                    # --- XML: walk feeds metadata descriptors into ProcessPool ---
                    from xml_metadata import XmlMetadataParseSession
                    from config import resolve_xml_standard_attributes_mode

                    xml_materialize, xml_preserve = resolve_xml_standard_attributes_mode(
                        settings.xml_standard_attributes_mode
                    )
                    logger.info("XML standard attributes mode: %s", settings.xml_standard_attributes_mode)

                    xml_workers = (
                        getattr(settings, "XML_PROCESS_WORKERS", None)
                        or getattr(settings, "PROCESS_WORKERS", None)
                        or 4
                    )
                    xml_session = XmlMetadataParseSession(
                        workers=int(xml_workers),
                        materialize_standard_attrs=xml_materialize,
                        preserve_listed_standard_attrs=xml_preserve,
                        root=Path(settings.code_directory),
                        project_name="",
                    )
                    with _stage_timer("CodeFileIndexer scan + XML submit"):
                        code_index = CodeFileIndexer.scan(
                            settings.code_directory,
                            consumers=CodeFileScanConsumers(on_metadata_xml=xml_session.submit),
                        )
                    with _stage_timer("XML metadata finish"):
                        configurations = xml_session.finish(is_extension=False)
                    xml_session = None  # finish() already shut down the pool

                    if not configurations:
                        return IndexingResult(
                            success=False,
                            metadata_source=metadata_source,
                            metadata_dir=metadata_dir,
                        )
                    _captured_configurations = list(configurations)
                    _captured_code_index = code_index
                    base_configuration = configurations[0]
                    logger.info("-" * 60)
                    logger.info("Loading metadata into Neo4j...")
                    with _stage_timer("Metadata graph load"):
                        # Full-load / initial load runs at startup: use the bounded
                        # embedding probe for vector DDL so a dead endpoint can't stall.
                        # SCHEMA_MANAGED_EXTERNALLY skips create_indexes() here so a
                        # fleet-wide simultaneous bootstrap does not deadlock on shared
                        # schema locks (schema pre-created by `main.py --ensure-schema`).
                        self.loader.load_configurations(
                            configurations,
                            use_startup_probe_for_vectors=True,
                            ensure_indexes=not settings.schema_managed_externally,
                        )
                    logger.info("-" * 60)

                    # Secondary parse via list-replay; BSL queued FIRST so its
                    # workers run in parallel with the ThreadPool secondary parse.
                    scan_session = DirectoryScanSession(
                        proc_config, configurations, settings.project_name,
                        code_index.root,
                        bsl_queue_callback=self.bsl_processor.queue_file if proc_config.enable_bsl else None,
                    )
                    if proc_config.enable_bsl:
                        cfg_name = configurations[0].name if configurations else ""
                        self.bsl_processor.start_workers(
                            settings.code_directory, settings.project_name, cfg_name, settings
                        )
                        bsl_started = True
                        scan_session.queue_bsl_from_index(code_index)
                    with _stage_timer("Secondary parse (xml)"):
                        scan_session.submit_index_non_bsl(code_index)
                        scan_results = scan_session.finish()
                    scan_session = None
                else:
                    # --- TXT: parse metadata first (no walk needed), then walk
                    #     overlaps secondary parse + BSL queueing via callbacks ---
                    with _stage_timer("TXT metadata parse"):
                        configurations = self.metadata_loader.load_configurations(
                            metadata_dir, None, source="txt", is_extension=False,
                        )
                    if not configurations:
                        return IndexingResult(
                            success=False,
                            metadata_source=metadata_source,
                            metadata_dir=metadata_dir,
                        )
                    _captured_configurations = list(configurations)
                    logger.info("-" * 60)
                    logger.info("Loading metadata into Neo4j...")
                    with _stage_timer("Metadata graph load"):
                        # Full-load / initial load runs at startup: use the bounded
                        # embedding probe for vector DDL so a dead endpoint can't stall.
                        # SCHEMA_MANAGED_EXTERNALLY skips create_indexes() here so a
                        # fleet-wide simultaneous bootstrap does not deadlock on shared
                        # schema locks (schema pre-created by `main.py --ensure-schema`).
                        self.loader.load_configurations(
                            configurations,
                            use_startup_probe_for_vectors=True,
                            ensure_indexes=not settings.schema_managed_externally,
                        )
                    logger.info("-" * 60)

                    if proc_config.enable_bsl:
                        cfg_name = configurations[0].name if configurations else ""
                        self.bsl_processor.start_workers(
                            settings.code_directory, settings.project_name, cfg_name, settings
                        )
                        bsl_started = True

                    scan_session = DirectoryScanSession(
                        proc_config, configurations, settings.project_name,
                        Path(settings.code_directory),
                        bsl_queue_callback=self.bsl_processor.queue_file if proc_config.enable_bsl else None,
                    )
                    logger.info("Base code scan: 1")
                    with _stage_timer("CodeFileIndexer scan + secondary parse (txt)"):
                        code_index = CodeFileIndexer.scan(
                            settings.code_directory,
                            consumers=scan_session.consumers(include_bsl=True),
                        )
                        scan_results = scan_session.finish()
                    scan_session = None
                    _captured_code_index = code_index

                forms_data = scan_results["forms_data"]
                predef_data = scan_results["predef_data"]
                help_data = scan_results["help_data"]
                formbin_results = scan_results["formbin_results"]
                event_subscriptions = scan_results.get("event_subscriptions", [])

                # Process Form.bin results (accumulate into BSL processor)
                for formbin_result in formbin_results:
                    self.bsl_processor.process_formbin_result(formbin_result)
                # Payloads are now owned by the locals above / BSL processor;
                # keeping the scan dict alive would hold every stage payload
                # until return.
                formbin_results = None
                scan_results = None
            except Exception:
                # Abort cleanup for long-lived pools before re-raising.
                if xml_session is not None:
                    xml_session.shutdown()
                if scan_session is not None:
                    scan_session.shutdown()
                if bsl_started and not bsl_finalized:
                    self.bsl_processor.terminate_workers()
                raise

            # Metadata parse + graph load + BSL file parsing (concurrent with the
            # scan) are done; remaining stages build out the rest of the graph
            # (forms/help/predefined/BSL linking/callsites/events/rights/extensions).
            index_progress.begin_phase(
                "building_graph",
                processed_getter=lambda: self.bsl_processor.bsl_parsed_count,
            )

            # =================================================================
            # STAGE 5: Load forms data
            # =================================================================
            with _stage_timer("Forms load"):
                if proc_config.enable_forms and self.forms_processor.has_data(forms_data):
                    logger.info("Loading accumulated Form XCF definitions into Neo4j ...")
                    logger.info("Accumulated data_bindings: %d", len(forms_data.data_bindings))
                    self.loader.load_form_definitions(forms_data.to_dict())
                    if forms_data.form_content_hashes:
                        logger.info("Updating form_content_hash for %d base forms ...", len(forms_data.form_content_hashes))
                        self.loader.update_form_hashes(forms_data.form_content_hashes)
                    logger.info("Form XCF loading finished")
                    logger.info("-" * 60)
                elif not proc_config.enable_forms:
                    logger.info("Skipping Form.xml loading (LOAD_FORMS_FROM_XML=false)")
            forms_data = None

            # =================================================================
            # STAGE 6: Load help content
            # =================================================================
            with _stage_timer("Help load"):
                if help_data.help_by_object:
                    logger.info("Loading help content (Справка) for %d metadata objects into Neo4j...", len(help_data.help_by_object))
                    for cfg in configurations:
                        try:
                            self.loader.load_help_content(help_data.help_by_object, settings.project_name, cfg.name)
                        except Exception as e:
                            logger.error("Failed to load help content for configuration %s: %s", cfg.name, str(e))
                    logger.info("Help content loading finished")
                    logger.info("-" * 60)
                else:
                    logger.info("No Help/ru.html files discovered during streaming scan")
            help_data = None

            # =================================================================
            # STAGE 7: Load predefined values
            # =================================================================
            with _stage_timer("Predefined load"):
                if proc_config.enable_predefined and predef_data.items:
                    logger.info("Loading predefined values (Predefined.xml) into Neo4j...")
                    for cfg in configurations:
                        try:
                            self.loader.load_predefined(predef_data.items, predef_data.relations, settings.project_name, cfg.name)
                        except Exception as e:
                            logger.error("Failed to load predefined values for configuration %s: %s", cfg.name, str(e))
                    logger.info("Predefined values loading finished")
                    logger.info("-" * 60)
                elif not proc_config.enable_predefined:
                    logger.info("Skipping Predefined.xml loading (LOAD_PREDEFINED_VALUES=false)")
                else:
                    logger.info("No Predefined.xml entries discovered during streaming scan")
            predef_data = None

            # =================================================================
            # STAGE 8: Finalize BSL processing and post-phase
            # =================================================================
            if proc_config.enable_bsl:
                # Finalize BSL workers and collect results
                with _stage_timer("BSL finalize"):
                    bsl_data = self.bsl_processor.finalize(settings)
                bsl_finalized = True

                # Post-phase linking (form events, commands)
                cfg_name = configurations[0].name if configurations else ""
                self.bsl_processor.post_phase_linking(
                    self.loader,
                    settings.project_name,
                    cfg_name,
                    bsl_data.form_routines
                )

                # Mark SSL API routines
                with self.loader.driver.session(database=settings.neo4j_database) as session:
                    self.bsl_processor.mark_ssl_api_routines(self.loader, settings, session)

                logger.info("-" * 60)

                # =================================================================
                # STAGE 9: Resolve callsites and load CALLS
                # =================================================================
                with _stage_timer("CALLS resolve+load"):
                    try:
                        call_rows, sorted_callers = self.callsites_resolver.resolve_calls(
                            bsl_data.routines_indexes,
                            bsl_data.callsites,
                            settings.project_name
                        )

                        if sorted_callers:
                            self.loader.load_bsl_calls(settings.project_name, call_rows, sorted_callers)
                            logger.info("BSL CALLS loaded: callers=%d, edges=%d", len(sorted_callers), len(call_rows))
                        else:
                            logger.info("BSL CALLS: no callsites detected")
                        call_rows = None
                        sorted_callers = None

                    except Exception as ce:
                        logger.error("BSL CALLS resolution failed: %s", str(ce))

            elif not proc_config.enable_bsl:
                logger.info("Skipping BSL signatures loading (LOAD_BSL_SIGNATURES=false)")

            # =================================================================
            # STAGE 10: Load Event Subscriptions
            # =================================================================
            if proc_config.enable_event_subscriptions:
                with _stage_timer("Event Subscriptions"):
                    try:
                        subscriptions = event_subscriptions
                        logger.info("Event Subscriptions parsed during scan: %d", len(subscriptions))

                        if subscriptions:
                            logger.info("Loading %d Event Subscriptions into Neo4j", len(subscriptions))
                            for config in configurations:
                                self.loader.load_event_subscriptions(subscriptions, settings.project_name, config.name)
                            logger.info("Event Subscriptions loading finished")

                            # Link to handlers
                            logger.info("Linking Event Subscriptions to their handlers...")
                            for config in configurations:
                                self.loader.link_event_subscriptions_to_handlers(settings.project_name, config.name)
                            logger.info("Event Subscriptions handler linking finished")
                        else:
                            logger.info("No Event Subscription files found")
                        subscriptions = None
                        event_subscriptions = None

                        logger.info("-" * 60)
                    except Exception as e:
                        logger.error("Event Subscriptions loading failed: %s", str(e))
            else:
                logger.info("Skipping Event Subscriptions loading (LOAD_EVENT_SUBSCRIPTIONS=false)")

            # =================================================================
            # STAGE 11: Load Role Rights
            # =================================================================
            if proc_config.enable_role_rights:
                with _stage_timer("Role rights"):
                    try:
                        cfg_name = configurations[0].name if configurations else ""
                        rr_rows = self.role_rights_processor.process_role_rights(
                            settings.code_directory,
                            settings.project_name,
                            cfg_name,
                            rights_xml_files=code_index.rights_xml_files,
                        )

                        if rr_rows:
                            logger.info("Loading Role rights with precise targets into Neo4j ...")
                            self.loader.load_role_rights_targets(rr_rows)
                            logger.info("Role rights loading finished")
                        rr_rows = None

                        logger.info("-" * 60)
                    except Exception as e:
                        logger.error("Role rights stage failed: %s", str(e))
            else:
                logger.info("Skipping Rights.xml loading (LOAD_ROLE_RIGHTS=false)")

            # =================================================================
            # STAGE 12: Load Extensions
            # =================================================================
            if proc_config.enable_extensions:
                try:
                    logger.info("=" * 80)
                    logger.info("Loading 1C Extensions")
                    logger.info("=" * 80)

                    base_config_name = configurations[0].name if configurations else ""

                    if not base_config_name:
                        logger.warning("Cannot load extensions: base configuration name not found")
                    else:
                        _base_routines_indexes = bsl_data.routines_indexes if bsl_data is not None else []
                        with _stage_timer("Extensions total"):
                            ext_result = self.extensions_loader.load_extensions(
                                base_config_name,
                                settings.code_directory,
                                callsites_resolver=self.callsites_resolver,
                                base_routines_indexes=_base_routines_indexes,
                                base_configuration=base_configuration,
                            )
                        extensions_count = ext_result["count"]
                        loaded_ext_pairs = ext_result["ext_pairs"]
                        loaded_ext_snapshots = ext_result.get("extensions", [])

                        if extensions_count > 0:
                            logger.info("✓ Successfully loaded %d extension(s)", extensions_count)
                        else:
                            logger.info("No extensions loaded (none found or all skipped)")

                        # STAGE 12b — EXTENDS_ROUTINE + EXTENDS_MODULE links for extensions
                        for ext_cfg_name, base_cfg_name in loaded_ext_pairs:
                            links_count = self.loader.create_extension_routine_links(
                                settings, ext_cfg_name, base_cfg_name
                            )
                            logger.info(
                                "[STAGE 12b] EXTENDS_ROUTINE %s → %s: %d links created",
                                ext_cfg_name, base_cfg_name, links_count,
                            )
                            mod_links_count = self.loader.create_extension_module_links(
                                settings, ext_cfg_name, base_cfg_name
                            )
                            logger.info(
                                "[STAGE 12b] EXTENDS_MODULE %s → %s: %d links created",
                                ext_cfg_name, base_cfg_name, mod_links_count,
                            )

                    logger.info("-" * 60)
                except Exception as e:
                    logger.error("Extensions loading stage failed: %s", str(e))
            else:
                logger.info("Skipping extensions loading (LOAD_EXTENSIONS=false)")

            # =================================================================
            # STAGE 13: Display Statistics
            # =================================================================
            self.display_statistics()

            mem_summary = format_run_summary()
            if mem_summary:
                logger.info("[MEM] run total: %s", mem_summary)

            return IndexingResult(
                success=True,
                configurations=_captured_configurations,
                code_index=_captured_code_index,
                metadata_source=metadata_source,
                metadata_dir=metadata_dir,
                extensions=locals().get("loaded_ext_snapshots", []),
                bsl_data=bsl_data,
            )

        except Exception as e:
            transient = is_transient_neo4j_error(e)
            if transient:
                # Same root cause as the constraint/index retry (indexes.py), but
                # here it hit a write path instead of DDL: under fleet-wide
                # concurrent bulk-load (many graph containers writing to the same
                # shared Neo4j at once), a batch/query can exhaust its own retries
                # (LockClientStopped, TransactionTimedOutClientConfiguration —
                # 22.07 incident: 18-container concurrent provisioning run) and
                # bubble up here. Classified so the caller (main.py:
                # check_and_load_metadata) can retry the whole pass once with a
                # fresh Neo4jLoader instead of exiting immediately.
                logger.warning(
                    "Indexing failed with a transient Neo4j error (likely fleet-wide "
                    "Neo4j contention during bulk load): %s", e,
                )
            logger.error("Error during indexing: %s", str(e))
            return IndexingResult(
                success=False,
                metadata_source=metadata_source,
                metadata_dir=metadata_dir,
                transient_error=transient,
            )

        finally:
            # Guarantee BSL workers are not left running on any error path.
            if bsl_started and not bsl_finalized:
                try:
                    self.bsl_processor.terminate_workers()
                except Exception as te:
                    logger.error("BSL terminate on cleanup failed: %s", te)
            # This pass (success or failure) is done either way — clear both
            # phase markers unconditionally so a failed/short-circuited run
            # (e.g. early "no configurations found" return) never leaves a
            # stale phase active for /api/console/metrics/index.
            index_progress.end_phase("loading_metadata")
            index_progress.end_phase("building_graph")
            # Ensure connection is closed
            self.loader.close()

    def verify_connection(self) -> bool:
        """
        Verify Neo4j connection.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            # Perform simple read-only query to validate connection
            self.loader.execute_query_readonly("RETURN 1")
            logger.info("Neo4j connection verified successfully")
            return True
        except Exception as e:
            logger.error("Neo4j connection failed: %s", str(e))
            return False
        finally:
            # Ensure the connection is properly closed in verify-only mode
            try:
                self.loader.close()
            except Exception:
                pass

    def display_statistics(self):
        """Display statistics about the loaded data"""
        self.statistics.display_statistics(settings)
