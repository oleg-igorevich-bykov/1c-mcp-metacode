"""
Main entry point.
Starts MCP server and auto-loads metadata into Neo4j on first run.
"""

# КРИТИЧНО для PyInstaller с multiprocessing!
import multiprocessing
multiprocessing.freeze_support()

import sys
import os
import json
import hashlib
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
from indexer import Indexer  # Now imports from the refactored indexer package
from mcpsrv.server import run_server
from config import settings
from neo4j_loader import Neo4jLoader
from logging_utils import setup_neo4j_debug_filtering
from runtime_memory import trim_process_memory

logger = logging.getLogger(__name__)


def _collect_guid_baseline_rows(
    cfg, project_name, config_name, guid_map, scope, out
) -> None:
    """Walk Configuration → emit identity rows для guid_state.

    Identity row = (label, qualified_name, xcf_name), current_guid = guid_map.get(xcf_name)
    или None. Используется только в `_init_incremental_baseline` для bootstrap sidecar
    после full reload. Лейблы и xcf_name строятся теми же forward helpers, что
    `_enrich_guids` / `collect_guid_state_rows` (через xcf_utils).
    """
    from xcf_utils import (
        xcf_name_object,
        xcf_name_attribute,
        xcf_name_tabular_part,
        xcf_name_tabular_attribute,
        xcf_name_resource,
        xcf_name_dimension,
        xcf_name_form,
    )

    def _emit(label, qn, xcf_name):
        if not xcf_name or not qn:
            return
        out.append({
            "scope": scope,
            "label": label,
            "qualified_name": qn,
            "xcf_name": xcf_name,
            "current_guid": (guid_map or {}).get(xcf_name),
        })

    for cat in cfg.categories:
        cat_name = cat.name
        for obj in cat.metadata_objects:
            obj_name = obj.name
            # Mirror rows_builder.py for subsystem path-based qn
            if cat_name == "Подсистемы":
                chain = (obj.properties or {}).get("ПутьПодсистемы")
                if isinstance(chain, list) and chain:
                    obj_qn = f"{project_name}/{config_name}/{cat_name}/" + "/".join(chain)
                else:
                    obj_qn = f"{project_name}/{config_name}/{cat_name}/{obj_name}"
            else:
                obj_qn = f"{project_name}/{config_name}/{cat_name}/{obj_name}"
            _emit("MetadataObject", obj_qn, xcf_name_object(cat_name, obj_name))
            for attr in getattr(obj, "attributes", []) or []:
                attr_qn = f"{obj_qn}/Attribute/{attr.name}"
                _emit("Attribute", attr_qn, xcf_name_attribute(cat_name, obj_name, attr.name))
            for tp in getattr(obj, "tabular_parts", []) or []:
                tp_qn = f"{obj_qn}/TabularPart/{tp.name}"
                _emit("TabularPart", tp_qn, xcf_name_tabular_part(cat_name, obj_name, tp.name))
                for tpa in getattr(tp, "attributes", []) or []:
                    tpa_qn = f"{tp_qn}/Attribute/{tpa.name}"
                    _emit(
                        "Attribute", tpa_qn,
                        xcf_name_tabular_attribute(cat_name, obj_name, tp.name, tpa.name),
                    )
            for res in getattr(obj, "resources", []) or []:
                res_qn = f"{obj_qn}/Resource/{res.name}"
                _emit("Resource", res_qn, xcf_name_resource(cat_name, obj_name, res.name))
            for dim in getattr(obj, "dimensions", []) or []:
                dim_qn = f"{obj_qn}/Dimension/{dim.name}"
                _emit("Dimension", dim_qn, xcf_name_dimension(cat_name, obj_name, dim.name))
            for form in getattr(obj, "forms", []) or []:
                form_qn = f"{obj_qn}/Form/{form.name}"
                _emit("Form", form_qn, xcf_name_form(cat_name, obj_name, form.name))


def _reset_bsl_code_search_after_bulk_load() -> bool:
    """Invalidate BSL code search sidecar for this project after a Neo4j bulk load.

    Tied to the graph-regeneration event (forced full reload or initial load),
    NOT to enable_bsl_code_search: the sidecar lives on persistent storage and
    survives container recreation independently of the feature flag, so a reload
    with BSL disabled must still invalidate an existing sidecar — otherwise a
    later re-enable (without a new reload) would trust stale ready-state against
    the fresh graph.

    Skip only when no sidecar file exists yet: there is nothing to invalidate,
    and get_bsl_code_sqlite() would otherwise materialize an empty sidecar
    (file + schema) for a possibly-disabled subsystem.

    Returns True on success/skip, False on a failure that must fail the reload.
    Failure is fail-closed only when BSL search is enabled: serving a fresh graph
    behind a stale-but-enabled sidecar is exactly the bug this prevents. When BSL
    is disabled, a failed cleanup must not block serving the new graph.
    """
    sqlite_path = Path(settings.bsl_code_search_sqlite_path)
    if not sqlite_path.exists():
        return True
    try:
        # reset_after_full_reload logs the "sidecar reset after bulk load" line.
        from graphdb.bsl_code_sqlite import get_bsl_code_sqlite
        get_bsl_code_sqlite().reset_after_full_reload(settings.project_name)
        return True
    except Exception:
        logger.exception("BSL code search sidecar reset failed after bulk load")
        return not settings.enable_bsl_code_search


def _init_incremental_baseline(result) -> None:
    """Записать baseline state после full reload.

    Заполняет:
    - source_manifest (sha256/size/mtime для текущих .txt или .xml файлов)
    - metadata_object_hashes (object_hash + property_keys_json)
    - configuration_state
    - form_property_keys, command_property_keys
    - stage_state (с актуальным source_type)
    """
    import time as _time
    from pathlib import Path as _Path

    from incremental.artifact_hashing import hash_files_parallel
    from incremental.hashing import (
        build_object_snapshot,
        compute_configuration_hash,
        compute_file_hash,
        compute_object_hash_from_snapshot,
    )
    from incremental.state import IncrementalLoadingState

    project_name = settings.project_name
    metadata_source = result.metadata_source
    configurations = result.configurations
    code_index = result.code_index

    if not configurations:
        return

    # BSL-symmetric: use path as-is (CWD-relative or absolute). В docker CWD=/app,
    # поэтому 'storage/incremental/incremental_loading.sqlite' → '/app/storage/...' → named volume.
    state_path = _Path(settings.incremental_loading_state_path)
    state = IncrementalLoadingState(state_path, project_name)

    t_total_start = _time.perf_counter()
    now_ns = _time.time_ns()

    # --- Phase A: pre-hash XML параллельно (ВНЕ транзакции) -----------------
    # Guard зеркалит текущие ветвления базового baseline:
    #   - TXT-режим: base XML не хешируется (orchestrator всё равно сканирует
    #     code_directory для BSL/artifacts, но source_manifest XML в TXT-режиме
    #     не пишется).
    #   - XML-режим: base XML хешируется.
    # Для расширений отдельный guard на ext_code_index + ext_code_dir.
    xml_paths_to_hash: list = []
    if metadata_source == "xml" and code_index is not None:
        xml_paths_to_hash.extend(getattr(code_index, "metadata_xml_files", []))
    for snapshot in result.extensions:
        if (
            snapshot.source == "xml"
            and snapshot.ext_code_index is not None
            and snapshot.ext_code_dir is not None
        ):
            xml_paths_to_hash.extend(
                getattr(snapshot.ext_code_index, "metadata_xml_files", [])
            )

    t_hash_start = _time.perf_counter()
    xml_stat_map = hash_files_parallel(xml_paths_to_hash) if xml_paths_to_hash else {}
    t_hash_ms = int((_time.perf_counter() - t_hash_start) * 1000)

    # --- Phase B: pre-build TXT manifest rows (последовательно) -------------
    # В TXT-режиме обычно один файл на scope; параллелизм не нужен.
    txt_manifest_rows: list = []
    if metadata_source == "txt":
        for txt_path in sorted(settings.metadata_directory.glob("*.txt")):
            try:
                data = txt_path.read_bytes()
                stat = txt_path.stat()
            except OSError:
                continue
            txt_manifest_rows.append(
                {
                    "source_type": "txt",
                    "rel_path": txt_path.name,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "content_hash": compute_file_hash(data),
                    "emitted_qns": [],
                }
            )

    # --- Phase C: собрать все rows в памяти ---------------------------------
    configuration_state_rows: list = []
    object_state_rows: list = []
    form_rows: list = []
    command_rows: list = []
    manifest_rows: list = []

    # Импорт _resolve_owner_object_qn один раз — текущая реализация вызывала
    # его в try/except per-file внутри цикла.
    try:
        from incremental.xml_walker import _resolve_owner_object_qn
    except Exception:  # pragma: no cover
        _resolve_owner_object_qn = None  # type: ignore[assignment]

    # Base configuration / objects / forms / commands.
    last_base_cfg_name = None
    for cfg in configurations:
        last_base_cfg_name = cfg.name
        config_qn = f"{project_name}/{cfg.name}"
        configuration_state_rows.append(
            {
                "source_type": metadata_source,
                "configuration_qn": config_qn,
                "configuration_hash": compute_configuration_hash(cfg),
                "property_keys": set((cfg.properties or {}).keys()),
            }
        )
        for cat in cfg.categories:
            for obj in cat.metadata_objects:
                # Mirror RowsBuilderMixin: subsystems use path-based QN.
                if cat.name == "Подсистемы":
                    chain = (obj.properties or {}).get("ПутьПодсистемы")
                    if isinstance(chain, list) and chain:
                        obj_qn = f"{project_name}/{cfg.name}/{cat.name}/" + "/".join(chain)
                    else:
                        obj_qn = f"{project_name}/{cfg.name}/{cat.name}/{obj.name}"
                else:
                    obj_qn = f"{project_name}/{cfg.name}/{cat.name}/{obj.name}"
                snap = build_object_snapshot(obj)
                object_state_rows.append(
                    {
                        "source_type": metadata_source,
                        "object_qn": obj_qn,
                        "object_hash": compute_object_hash_from_snapshot(snap),
                        "property_keys": set((obj.properties or {}).keys()),
                        "snapshot": snap,
                    }
                )
                for form in obj.forms or []:
                    form_rows.append(
                        {
                            "source_type": metadata_source,
                            "form_qn": f"{obj_qn}/Form/{form.name}",
                            "property_keys": set((form.properties or {}).keys()),
                        }
                    )
                for cmd in obj.commands or []:
                    command_rows.append(
                        {
                            "source_type": metadata_source,
                            "command_qn": f"{obj_qn}/Command/{cmd.name}",
                            "property_keys": set((cmd.properties or {}).keys()),
                        }
                    )

    # Base source_manifest rows.
    manifest_rows.extend(txt_manifest_rows)
    if metadata_source == "xml" and code_index is not None:
        code_root = _Path(getattr(code_index, "root", settings.code_directory))
        for xml_path in getattr(code_index, "metadata_xml_files", []):
            stat = xml_stat_map.get(xml_path)
            if stat is None:
                continue
            try:
                rel_path = str(_Path(xml_path).relative_to(code_root)).replace("\\", "/")
            except ValueError:
                continue
            owner_qn = None
            if _resolve_owner_object_qn is not None and last_base_cfg_name is not None:
                try:
                    owner_qn = _resolve_owner_object_qn(
                        rel_path, project_name, last_base_cfg_name
                    )
                except Exception:
                    owner_qn = None
            manifest_rows.append(
                {
                    "source_type": "xml",
                    "rel_path": rel_path,
                    "size": stat.size,
                    "mtime_ns": stat.mtime_ns,
                    "content_hash": stat.content_hash,
                    "emitted_qns": [owner_qn] if owner_qn else [],
                }
            )

    # Per-extension rows.
    # Critical: QN строится через snapshot.ext_graph_config_name (с $ext$),
    # а НЕ через snapshot.configuration.name — последнее восстановлено в raw имя
    # после graph load (extensions_loader.py:562-563).
    stage_state_calls: list = [(f"metadata_{metadata_source}", metadata_source)]
    extension_scope_log: list = []
    for snapshot in result.extensions:
        source_scope = f"{snapshot.source}_ext:{snapshot.ext_dir_name}"
        ext_cfg = snapshot.configuration
        ext_cfg_qn = f"{project_name}/{snapshot.ext_graph_config_name}"

        configuration_state_rows.append(
            {
                "source_type": source_scope,
                "configuration_qn": ext_cfg_qn,
                "configuration_hash": compute_configuration_hash(ext_cfg),
                "property_keys": set((ext_cfg.properties or {}).keys()),
            }
        )
        for cat in ext_cfg.categories:
            for obj in cat.metadata_objects:
                if cat.name == "Подсистемы":
                    chain = (obj.properties or {}).get("ПутьПодсистемы")
                    if isinstance(chain, list) and chain:
                        obj_qn = (
                            f"{project_name}/{snapshot.ext_graph_config_name}/{cat.name}/"
                            + "/".join(chain)
                        )
                    else:
                        obj_qn = (
                            f"{project_name}/{snapshot.ext_graph_config_name}/{cat.name}/{obj.name}"
                        )
                else:
                    obj_qn = (
                        f"{project_name}/{snapshot.ext_graph_config_name}/{cat.name}/{obj.name}"
                    )
                snap = build_object_snapshot(obj)
                object_state_rows.append(
                    {
                        "source_type": source_scope,
                        "object_qn": obj_qn,
                        "object_hash": compute_object_hash_from_snapshot(snap),
                        "property_keys": set((obj.properties or {}).keys()),
                        "snapshot": snap,
                    }
                )
                for form in obj.forms or []:
                    form_rows.append(
                        {
                            "source_type": source_scope,
                            "form_qn": f"{obj_qn}/Form/{form.name}",
                            "property_keys": set((form.properties or {}).keys()),
                        }
                    )
                for cmd in obj.commands or []:
                    command_rows.append(
                        {
                            "source_type": source_scope,
                            "command_qn": f"{obj_qn}/Command/{cmd.name}",
                            "property_keys": set((cmd.properties or {}).keys()),
                        }
                    )

        # source_manifest для расширения.
        if snapshot.source == "txt" and snapshot.ext_metadata_dir is not None:
            for txt_path in sorted(snapshot.ext_metadata_dir.glob("*.txt")):
                try:
                    data = txt_path.read_bytes()
                    stat = txt_path.stat()
                except OSError:
                    continue
                manifest_rows.append(
                    {
                        "source_type": source_scope,
                        "rel_path": txt_path.name,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "content_hash": compute_file_hash(data),
                        "emitted_qns": [],
                    }
                )
        elif (
            snapshot.source == "xml"
            and snapshot.ext_code_index is not None
            and snapshot.ext_code_dir is not None
        ):
            ext_code_root = _Path(snapshot.ext_code_dir)
            for xml_path in getattr(snapshot.ext_code_index, "metadata_xml_files", []):
                fstat = xml_stat_map.get(xml_path)
                if fstat is None:
                    continue
                try:
                    rel_path = str(
                        _Path(xml_path).relative_to(ext_code_root)
                    ).replace("\\", "/")
                except ValueError:
                    continue
                owner_qn = None
                if _resolve_owner_object_qn is not None:
                    try:
                        owner_qn = _resolve_owner_object_qn(
                            rel_path, project_name, snapshot.ext_graph_config_name
                        )
                    except Exception:
                        owner_qn = None
                manifest_rows.append(
                    {
                        "source_type": source_scope,
                        "rel_path": rel_path,
                        "size": fstat.size,
                        "mtime_ns": fstat.mtime_ns,
                        "content_hash": fstat.content_hash,
                        "emitted_qns": [owner_qn] if owner_qn else [],
                    }
                )

        stage_state_calls.append((f"metadata_{source_scope}", source_scope))
        extension_scope_log.append((source_scope, snapshot.ext_graph_config_name))

    # --- Phase D-pre: collect GUID baseline rows (guid_state + manifest)  ----
    # Симметричный с metadata baseline: state.upsert_guid_state_many пишет
    # identity rows (label, qualified_name, xcf_name) + current_guid из текущей
    # ConfigDumpInfo.xml для соответствующего scope. Без этого first incremental
    # после full reload не сможет отработать ConfigDumpInfo-only сценарий.
    guid_state_rows: list = []
    guid_manifest_rows: list = []
    if getattr(settings, "load_metadata_guids", True):
        try:
            from dumpinfo_loader import load_dumpinfo_map as _load_guid_map
            from incremental.guid_sync import (
                _read_guid_map_and_stats as _read_guid_for_baseline,
            )
            # base ConfigDumpInfo.xml + Configuration
            base_code_dir = _Path(settings.code_directory)
            base_guid_map, base_stats = _read_guid_for_baseline(base_code_dir)
            for _cfg in configurations:
                _collect_guid_baseline_rows(
                    _cfg, project_name, _cfg.name, base_guid_map,
                    scope="guid:base", out=guid_state_rows,
                )
            if base_stats is not None:
                size, mtime_ns, content_hash = base_stats
                guid_manifest_rows.append({
                    "source_type": "guid",
                    "rel_path": "guid:base",
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "content_hash": content_hash,
                    "emitted_qns": [],
                })
            # per-extension
            for snapshot in result.extensions:
                ext_code_dir = (
                    _Path(snapshot.ext_code_dir)
                    if snapshot.ext_code_dir is not None
                    else None
                )
                if ext_code_dir is None or not ext_code_dir.exists():
                    continue
                ext_guid_map, ext_stats = _read_guid_for_baseline(ext_code_dir)
                ext_scope = f"guid_ext:{snapshot.source}:{snapshot.ext_dir_name}"
                _collect_guid_baseline_rows(
                    snapshot.configuration,
                    project_name,
                    snapshot.ext_graph_config_name,
                    ext_guid_map,
                    scope=ext_scope,
                    out=guid_state_rows,
                )
                if ext_stats is not None:
                    size, mtime_ns, content_hash = ext_stats
                    guid_manifest_rows.append({
                        "source_type": "guid",
                        "rel_path": ext_scope,
                        "size": size,
                        "mtime_ns": mtime_ns,
                        "content_hash": content_hash,
                        "emitted_qns": [],
                    })
        except Exception:  # noqa: BLE001
            logger.exception("Baseline: GUID state collection failed")

    # --- Phase D: одна транзакция вокруг reset + всех batch writes ----------
    # reset_after_full_reload использует conn.execute(...) и корректно
    # участвует во внешней транзакции. При ошибке middle-of-write rollback
    # откатит и DELETE-ы — старое состояние сохранится (атомарность baseline).
    # Закрытие state выполняем через finally, включая ошибочные пути (метаданных
    # транзакция или artifact baseline). Исключение НЕ проглатывается — fatal-решение
    # принимает вызывающий check_and_load_metadata (гейтит на incremental_loading_enabled).
    try:
        t_sqlite_start = _time.perf_counter()
        with state.transaction():
            state.reset_after_full_reload()
            state.upsert_configuration_state_many(configuration_state_rows)
            state.upsert_object_state_many(object_state_rows)
            state.upsert_form_property_keys_many(form_rows)
            state.upsert_command_property_keys_many(command_rows)
            state.upsert_source_manifest_many(manifest_rows + guid_manifest_rows)
            if guid_state_rows:
                state.upsert_guid_state_many(guid_state_rows)
            for stage_name, source_type in stage_state_calls:
                state.upsert_stage_state(stage_name, source_type, now_ns)
        t_sqlite_ms = int((_time.perf_counter() - t_sqlite_start) * 1000)
        t_total_ms = int((_time.perf_counter() - t_total_start) * 1000)

        for source_scope, ext_cfg_name in extension_scope_log:
            logger.info(
                "Incremental baseline initialized for extension scope=%s (cfg=%s)",
                source_scope, ext_cfg_name,
            )

        logger.info(
            "Incremental metadata baseline persist complete: "
            "configs=%d objects=%d forms=%d commands=%d source_manifest=%d "
            "hash_ms=%d sqlite_ms=%d total_ms=%d",
            len(configuration_state_rows),
            len(object_state_rows),
            len(form_rows),
            len(command_rows),
            len(manifest_rows),
            t_hash_ms,
            t_sqlite_ms,
            t_total_ms,
        )

        # Artifact baseline (phase 2-4). Симметрично metadata baseline пишет
        # `artifact_manifest` и `bsl_file_artifacts` после full reload, чтобы первый
        # incremental cycle мог сравнить artifacts с этим baseline. Исключение
        # распространяется (fatal-решение — у вызывающего).
        _init_artifact_baseline(state, result)
    finally:
        state.close()
    logger.info("Incremental baseline initialized for source=%s", metadata_source)


def _init_artifact_baseline(state, result) -> None:
    """Записать artifact_manifest + bsl_file_artifacts для базы и расширений.

    Использует уже агрегированный `result.bsl_data` (заполняется orchestrator-ом)
    и `result.code_index` / `snapshot.ext_code_index`. POSIX-rel ключ для BSL
    строится через `_bsl_key_fn` — тот же, что у full-load BSL scanner (совместимо
    с `Routine.file_path` / `Module.path`).

    Hashing идёт параллельно через `hash_files_parallel` (один общий
    ProcessPoolExecutor для base + extensions). Запись в SQLite — batch upserts.
    """
    from pathlib import Path as _PPath

    from incremental.artifact_sync import (
        ART_BASE_BSL,
        ART_BASE_FORM_XML,
        ART_BASE_FORM_BIN,
        ART_BASE_PREDEFINED,
        ART_BASE_HELP,
        ART_BASE_EVENT_SUBSCRIPTION,
        ART_BASE_RIGHTS,
        ext_artifact_scope,
        _bsl_key_fn,
    )
    from incremental.artifact_hashing import hash_files_parallel
    from incremental.bsl_artifact_builder import (
        build_artifacts_from_bsl_data,
        persist_artifacts,
    )
    from indexer.data_structures import ProcessingConfig

    code_index = result.code_index
    if code_index is None:
        # Нет code_index (напр. TXT-режим без code dir): artifact-скоупов нет, но
        # baseline-фаза завершена успешно — пишем completion stage (пустой baseline),
        # иначе evaluator ошибочно решит, что artifact baseline не завершён.
        with state.transaction():
            state.upsert_stage_state(
                "artifact_baseline", result.metadata_source, time.time_ns()
            )
        logger.info("Artifact baseline: no code_index — wrote empty completion stage")
        return

    # Baseline должен содержать manifest только тех scope-ов, которые full load реально
    # загружает при текущих настройках. Иначе после переключения флага incremental увидит
    # baseline-manifest для scope-а, в Neo4j данных которого нет (manifest сравнивается
    # с пустым графом — все файлы помечаются как unchanged и pipeline ничего не загрузит).
    proc_config = ProcessingConfig.from_settings(settings)

    data_dir = getattr(settings, "data_directory", None)
    code_root = _PPath(getattr(code_index, "root", settings.code_directory))

    # Собираем (scope, path, root_for_rel) для всех файлов baseline и делаем один
    # общий параллельный hash, чтобы амортизировать запуск ProcessPoolExecutor.
    plan_entries: list = []  # [(scope, path, root_for_rel)]

    def _add_simple(scope: str, files, root_for_rel: _PPath) -> None:
        for path in files or []:
            plan_entries.append((scope, _PPath(path), root_for_rel))

    # ---- 1. Собрать пути всех baseline scopes (base + extensions) ----
    # Simple (non-BSL) — relative_to(root_for_rel). Base scope-ы гейтятся всеми 7 флагами,
    # потому что full load для base гейтит их через streaming scanner callbacks + orchestrator.
    if proc_config.enable_forms:
        _add_simple(
            ART_BASE_FORM_XML,
            [e.form_xml_path for e in getattr(code_index, "form_xml_files", []) or []],
            code_root,
        )
    if proc_config.enable_bsl:
        _add_simple(ART_BASE_FORM_BIN, getattr(code_index, "form_bin_files", []) or [], code_root)
    if proc_config.enable_predefined:
        _add_simple(ART_BASE_PREDEFINED, getattr(code_index, "predefined_xml_files", []) or [], code_root)
    if proc_config.enable_help:
        _add_simple(ART_BASE_HELP, getattr(code_index, "help_html_files", []) or [], code_root)
    if proc_config.enable_event_subscriptions:
        _add_simple(
            ART_BASE_EVENT_SUBSCRIPTION,
            getattr(code_index, "event_subscription_xml_files", []) or [],
            code_root,
        )
    if proc_config.enable_role_rights:
        _add_simple(ART_BASE_RIGHTS, getattr(code_index, "rights_xml_files", []) or [], code_root)

    # BSL pool — отдельно, потому что key_fn другой (POSIX-rel от data_directory).
    # (scope, path, key_fn) для batched hashing.
    bsl_entries: list = []  # [(scope, path, key_fn)]
    bsl_files: list = []
    form_bin_files_base: list = []
    if proc_config.enable_bsl:
        bsl_files = list(getattr(code_index, "bsl_files", []) or [])
        form_bin_files_base = list(getattr(code_index, "form_bin_files", []) or [])
        base_key_fn = _bsl_key_fn(data_dir, code_root)
        for path in bsl_files:
            bsl_entries.append((ART_BASE_BSL, _PPath(path), base_key_fn))
        for path in form_bin_files_base:
            bsl_entries.append((ART_BASE_FORM_BIN, _PPath(path), base_key_fn))

    # Extensions — собираем simple + BSL. Целиком пропускаем, если LOAD_EXTENSIONS=false.
    # Per-scope гейтинг внутри ext loop зеркалит фактические проверки full load в
    # `ExtensionsLoader.load_extensions` (только help/event_subscription/rights — остальные
    # ext scope-ы full load загружает всегда при наличии файлов).
    ext_records: list = []  # [(snapshot, mode, ext_dir_name, ext_root, ext_idx)] — для второго прохода
    if not proc_config.enable_extensions:
        result_extensions: list = []
    else:
        result_extensions = list(result.extensions or [])
    for snapshot in result_extensions:
        ext_dir_name = snapshot.ext_dir_name
        ext_idx = snapshot.ext_code_index
        ext_code_dir = snapshot.ext_code_dir
        if ext_idx is None and ext_code_dir is not None:
            try:
                from indexer.code_file_index import CodeFileIndexer

                ext_idx = CodeFileIndexer.scan(_PPath(ext_code_dir))
            except Exception:
                logger.exception(
                    "Artifact baseline (ext=%s): fallback CodeFileIndexer.scan failed",
                    ext_dir_name,
                )
                continue
        if ext_idx is None or ext_code_dir is None:
            continue
        mode = snapshot.source
        ext_root = _PPath(ext_code_dir)
        _add_simple(
            ext_artifact_scope(mode, ext_dir_name, "form_xml"),
            [e.form_xml_path for e in getattr(ext_idx, "form_xml_files", []) or []],
            ext_root,
        )
        _add_simple(
            ext_artifact_scope(mode, ext_dir_name, "form_bin"),
            getattr(ext_idx, "form_bin_files", []) or [],
            ext_root,
        )
        _add_simple(
            ext_artifact_scope(mode, ext_dir_name, "predefined"),
            getattr(ext_idx, "predefined_xml_files", []) or [],
            ext_root,
        )
        if proc_config.enable_help:
            _add_simple(
                ext_artifact_scope(mode, ext_dir_name, "help"),
                getattr(ext_idx, "help_html_files", []) or [],
                ext_root,
            )
        if proc_config.enable_event_subscriptions:
            _add_simple(
                ext_artifact_scope(mode, ext_dir_name, "event_subscription"),
                getattr(ext_idx, "event_subscription_xml_files", []) or [],
                ext_root,
            )
        if proc_config.enable_role_rights:
            _add_simple(
                ext_artifact_scope(mode, ext_dir_name, "rights"),
                getattr(ext_idx, "rights_xml_files", []) or [],
                ext_root,
            )
        _add_simple(
            ext_artifact_scope(mode, ext_dir_name, "property_analysis"),
            getattr(ext_idx, "extension_property_analysis_xml_files", []) or [],
            ext_root,
        )
        ext_bsl_list = list(getattr(ext_idx, "bsl_files", []) or [])
        ext_form_bin = list(getattr(ext_idx, "form_bin_files", []) or [])
        if ext_bsl_list or ext_form_bin:
            ext_key_fn = _bsl_key_fn(data_dir, ext_root)
            scope_bsl = ext_artifact_scope(mode, ext_dir_name, "bsl")
            scope_fbin = ext_artifact_scope(mode, ext_dir_name, "form_bin")
            for path in ext_bsl_list:
                bsl_entries.append((scope_bsl, _PPath(path), ext_key_fn))
            for path in ext_form_bin:
                bsl_entries.append((scope_fbin, _PPath(path), ext_key_fn))
        ext_records.append((snapshot, mode, ext_dir_name, ext_root, ext_idx))

    # ---- 2. Один общий параллельный hash для всех путей ----
    all_paths = [e[1] for e in plan_entries] + [e[1] for e in bsl_entries]
    t_hash_start = time.monotonic()
    stat_map = hash_files_parallel(all_paths)
    logger.info(
        "Artifact baseline: hashed %d files in %s (workers=parallel)",
        len(stat_map), _fmt_duration(time.monotonic() - t_hash_start),
    )

    # ---- 3. Batch manifest writes ----
    t_persist_start = time.monotonic()

    t = time.monotonic()
    simple_rows: list = []
    for scope, path, root_for_rel in plan_entries:
        st = stat_map.get(path)
        if st is None:
            continue
        try:
            rel = str(path.relative_to(root_for_rel)).replace("\\", "/")
        except ValueError:
            continue
        simple_rows.append({
            "source_scope": scope,
            "rel_path": rel,
            "size": st.size,
            "mtime_ns": st.mtime_ns,
            "content_hash": st.content_hash,
        })

    # BSL: помимо manifest, собираем file_path_to_hash для bsl_file_artifacts.
    bsl_rows: list = []
    base_file_path_to_hash: dict = {}
    ext_file_path_to_hash_by_dir: dict = {}  # ext_dir_name → {key: hash}
    for scope, path, key_fn in bsl_entries:
        st = stat_map.get(path)
        if st is None:
            continue
        try:
            key = key_fn(path)
        except (ValueError, Exception):
            continue
        bsl_rows.append({
            "source_scope": scope,
            "rel_path": key,
            "size": st.size,
            "mtime_ns": st.mtime_ns,
            "content_hash": st.content_hash,
        })
        if scope == ART_BASE_BSL or scope == ART_BASE_FORM_BIN:
            base_file_path_to_hash[key] = st.content_hash
        else:
            # ext scope: "artifact:ext:<mode>:<dir>:bsl" / "...:form_bin"
            parts = scope.split(":")
            if len(parts) >= 5:
                ext_dir_name = parts[3]
                ext_file_path_to_hash_by_dir.setdefault(ext_dir_name, {})[key] = st.content_hash
    t_manifest_build = time.monotonic() - t

    # ---- 4. bsl_file_artifacts: build artifacts (сериализация — потоково в п.5) ----
    # Строим per-file артефакты, но НЕ материализуем полный список JSON-строк:
    # сериализация и запись идут батчами в п.5. После построения base-артефактов
    # отвязываем агрегированный result.bsl_data (последний потребитель) — это
    # начинает постепенное освобождение routines/callsites/form_links.
    base_cfg_name = result.configurations[0].name if result.configurations else ""

    t = time.monotonic()
    base_artifacts: list = []
    if (bsl_files or form_bin_files_base) and result.bsl_data is not None:
        base_artifacts = build_artifacts_from_bsl_data(
            bsl_data=result.bsl_data,
            source_scope=ART_BASE_BSL,
            file_path_to_hash=base_file_path_to_hash,
            default_config_name=base_cfg_name,
        )
    t_bsl_base_build = time.monotonic() - t
    result.bsl_data = None

    ext_built: list = []  # [(scope_bsl, ext_artifacts_list)]
    t_bsl_ext_build_total = 0.0
    for snapshot, mode, ext_dir_name, ext_root, ext_idx in ext_records:
        ext_bsl_list = list(getattr(ext_idx, "bsl_files", []) or [])
        ext_form_bin = list(getattr(ext_idx, "form_bin_files", []) or [])
        if not (ext_bsl_list or ext_form_bin):
            continue
        scope_bsl = ext_artifact_scope(mode, ext_dir_name, "bsl")
        ext_path_hash = ext_file_path_to_hash_by_dir.get(ext_dir_name, {})
        # snapshot.bsl_data is extension-owned; None means "no BSLData for this
        # extension" (BSL stage skipped/failed). Do NOT fall back to base
        # result.bsl_data — that would build extension artifacts from base data,
        # mixing scopes and hiding an extension BSL-parse failure.
        if snapshot.bsl_data is None:
            logger.warning(
                "Extension BSL artifacts skipped for %s: no BSLData "
                "(BSL inputs present but parse produced nothing)",
                ext_dir_name,
            )
            continue
        ext_bsl_data = snapshot.bsl_data
        t = time.monotonic()
        ext_artifacts = build_artifacts_from_bsl_data(
            bsl_data=ext_bsl_data,
            source_scope=scope_bsl,
            file_path_to_hash=ext_path_hash,
            default_config_name=snapshot.ext_graph_config_name,
        )
        t_bsl_ext_build_total += time.monotonic() - t
        # Артефакты построены → отвязываем extension-owned bsl_data.
        snapshot.bsl_data = None
        ext_built.append((scope_bsl, ext_artifacts))

    # ---- 4.5. Property analysis sidecar baseline ----
    # Запускаем classifier+extractor на extension XML-файлах, чтобы заполнить
    # ext_analyzer_outputs (sidecar) до первого incremental cycle. Без этого
    # первое удаление analyzer-owned свойства после full reload не сможет очистить
    # Neo4j (нет prev sidecar для diff).
    pa_sidecar_rows: list = []
    try:
        from indexer.extension_property_analysis import (
            analyze_xml_files,
            derive_element_qn_and_label,
        )
        import json as _json

        for snapshot, mode, ext_dir_name, ext_root, ext_idx in ext_records:
            pa_files = list(
                getattr(ext_idx, "extension_property_analysis_xml_files", []) or []
            )
            if not pa_files:
                continue
            try:
                pa_cls, pa_ext = analyze_xml_files(pa_files)
            except Exception:
                logger.exception(
                    "Baseline property_analysis: analyze failed for ext=%s",
                    ext_dir_name,
                )
                continue
            scope = ext_artifact_scope(mode, ext_dir_name, "property_analysis")
            ext_cfg_qn = f"{settings.project_name}/{snapshot.ext_graph_config_name}"
            # rel_path lookup
            def _rel(p):
                try:
                    return str(_PPath(p).relative_to(ext_root)).replace("\\", "/")
                except ValueError:
                    return None
            for xml_file, obj_result in pa_cls:
                rel = _rel(xml_file)
                if not rel:
                    continue
                for element in getattr(obj_result, "elements", []) or []:
                    if not getattr(element, "is_adopted", False):
                        continue
                    label, qn = derive_element_qn_and_label(
                        obj_result, element, ext_cfg_qn, code_root=ext_root,
                    )
                    if not label or not qn:
                        continue
                    payload = _json.dumps({
                        "controlled": list(getattr(element, "controlled_properties", []) or []),
                        "modified": list(getattr(element, "modified_properties", []) or []),
                    }, ensure_ascii=False, sort_keys=True)
                    pa_sidecar_rows.append({
                        "source_scope": scope,
                        "rel_path": rel,
                        "label": label,
                        "qualified_name": qn,
                        "output_kind": "classification",
                        "property_key": "__classification__",
                        "payload_json": payload,
                    })
            for xml_file, obj_result in pa_ext:
                rel = _rel(xml_file)
                if not rel:
                    continue
                for element in getattr(obj_result, "elements", []) or []:
                    prop_values = getattr(element, "property_values", None)
                    if not prop_values:
                        continue
                    label, qn = derive_element_qn_and_label(
                        obj_result, element, ext_cfg_qn, code_root=ext_root,
                    )
                    if not label or not qn:
                        continue
                    for key, value in prop_values.items():
                        payload = _json.dumps(
                            {"value": value}, ensure_ascii=False, sort_keys=True
                        )
                        pa_sidecar_rows.append({
                            "source_scope": scope,
                            "rel_path": rel,
                            "label": label,
                            "qualified_name": qn,
                            "output_kind": "property_value",
                            "property_key": key,
                            "payload_json": payload,
                        })
    except Exception:
        logger.exception("Baseline property_analysis sidecar build failed")

    # ---- 5. SQLite writes: per-batch commit, completion stage последним ----
    # `reset_after_full_reload()` уже отработал в metadata-транзакции, поэтому
    # artifact-таблицы пусты. Пишем per-batch commit-ами (WAL не раздувается), а
    # durable-признак завершённости — `stage_state('artifact_baseline')` —
    # отдельным ФИНАЛЬНЫМ commit-ом. Он единственный сигнал достоверности baseline:
    # обрыв на середине оставит частичные строки без stage → следующий full reload
    # их снесёт `reset_after_full_reload()`.
    manifest_total_rows = len(simple_rows) + len(bsl_rows)
    t_sqlite_start = time.monotonic()

    # 5.1 manifest — один commit (строки мелкие).
    with state.transaction():
        state.upsert_artifact_manifest_many(simple_rows + bsl_rows)

    # 5.2 BSL артефакты — per-batch commit; ownership payload освобождается ПОСЛЕ commit-а.
    totals = {"artifacts": 0, "routines": 0, "callsites": 0, "form_links": 0, "json_bytes": 0}
    last_committed = {"scope": None, "batch": 0}

    def _stream_scope(scope_name: str, artifacts_list: list) -> None:
        scope_total = len(artifacts_list)
        if not scope_total:
            return
        batch_no = 0
        processed = 0
        for rows, consumed, bstats in state.iter_bsl_file_artifact_batches(artifacts_list):
            batch_no += 1
            _t = time.monotonic()
            with state.transaction():
                state.upsert_bsl_file_artifacts_rows(rows)
            batch_seconds = time.monotonic() - _t
            last_committed["scope"] = scope_name
            last_committed["batch"] = batch_no
            processed += bstats["artifacts"]
            for _k in totals:
                totals[_k] += bstats[_k]
            batch_mib = bstats["json_bytes"] / (1024.0 * 1024.0)
            # Освобождаем payload обработанного батча ПОСЛЕ подтверждённого commit-а.
            for art in consumed:
                art.routines_index = []
                art.callsites = []
                art.form_links = []
            rows.clear()
            consumed.clear()
            logger.info(
                "Artifact baseline: scope=%s batch=%d committed artifacts=%d/%d (%.0f%%) "
                "batch_json=%.2f MiB cum_json=%.2f MiB elapsed=%.1fs %.1f MiB/s",
                scope_name, batch_no, processed, scope_total,
                100.0 * processed / scope_total,
                batch_mib, totals["json_bytes"] / (1024.0 * 1024.0),
                time.monotonic() - t_sqlite_start,
                (batch_mib / batch_seconds) if batch_seconds > 0 else 0.0,
            )

    try:
        _stream_scope(ART_BASE_BSL, base_artifacts)
        for scope_bsl, ext_artifacts in ext_built:
            _stream_scope(scope_bsl, ext_artifacts)

        # 5.3 property-analysis sidecar — один commit.
        if pa_sidecar_rows:
            with state.transaction():
                state.upsert_ext_analyzer_outputs_many(pa_sidecar_rows)

        # 5.4 completion stage — СТРОГО ПОСЛЕДНИМ отдельным commit-ом (durable-успех).
        logger.info(
            "Artifact baseline: committing completion stage (artifacts=%d, json=%.2f MiB)",
            totals["artifacts"], totals["json_bytes"] / (1024.0 * 1024.0),
        )
        with state.transaction():
            state.upsert_stage_state(
                "artifact_baseline", result.metadata_source, time.time_ns()
            )
    except Exception:
        # Обрыв: последний успешно закоммиченный батч уже durable, но completion
        # stage НЕ записан → baseline невалиден (evaluator даст FULL_RELOAD_REQUIRED),
        # частичные строки снесёт reset_after_full_reload() следующего reload.
        logger.exception(
            "Artifact baseline aborted after last committed scope=%s batch=%d "
            "(committed artifacts=%d); completion stage NOT written",
            last_committed["scope"], last_committed["batch"], totals["artifacts"],
        )
        raise
    t_sqlite_total = time.monotonic() - t_sqlite_start
    logger.info("Artifact baseline: commit complete in %s", _fmt_duration(t_sqlite_total))

    # 5.5 best-effort WAL checkpoint — НЕ влияет на durable-успех (не пробрасываем).
    try:
        _t = time.monotonic()
        cp = state._connect().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        logger.info(
            "Artifact baseline: wal_checkpoint(TRUNCATE) result=%s in %.1fs",
            cp, time.monotonic() - _t,
        )
    except Exception:
        logger.warning(
            "Artifact baseline: post-commit wal_checkpoint failed (non-fatal)",
            exc_info=True,
        )

    # ---- 6. Summary ----
    total_seconds = time.monotonic() - t_persist_start
    logger.info(
        "Artifact baseline persist complete:\n"
        "  manifest rows=%d build=%s\n"
        "  bsl artifacts=%d routines=%d callsites=%d form_links=%d\n"
        "  bsl build: base=%s ext(%d)=%s\n"
        "  sqlite (per-batch commits + completion stage)=%s\n"
        "  json=%.2f MB total=%s",
        manifest_total_rows, _fmt_duration(t_manifest_build),
        totals["artifacts"], totals["routines"], totals["callsites"], totals["form_links"],
        _fmt_duration(t_bsl_base_build), len(ext_built), _fmt_duration(t_bsl_ext_build_total),
        _fmt_duration(t_sqlite_total),
        totals["json_bytes"] / (1024.0 * 1024.0), _fmt_duration(total_seconds),
    )


def _fmt_duration(seconds: float) -> str:
    """Format duration as HH:MM:SS.mmm"""
    try:
        secs_int = int(seconds)
        ms = int(round((seconds - secs_int) * 1000))
        h, rem = divmod(secs_int, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    except Exception:
        return f"{seconds:.3f}s"


# ============================================================================
# One-shot FULL_METADATA_RELOAD marker
# ----------------------------------------------------------------------------
# FULL_METADATA_RELOAD=true запускает forced full reload только один раз в рамках
# текущего контейнера. Признак "флаг уже отработал" хранится в container-local
# marker-файле в app-owned каталоге ниже: несмонтированный подкаталог /app в
# writable layer образа переживает `docker restart`, но исчезает при recreate
# (`docker compose up -d --force-recreate <service>`). /tmp сознательно не
# используется — его семантика допускает tmpfs/cleanup, что незаметно превратило
# бы "один раз на контейнер" в "один раз до очистки /tmp".
# ВАЖНО: этот путь нельзя монтировать на persistent volume, иначе marker
# переживёт recreate и одноразовость сломается.
# ============================================================================
_RELOAD_MARKER_DIR = Path("/app/.container-state")


def _reload_marker_path() -> Path:
    """Путь к marker-файлу для текущей цели reload.

    Имя включает короткий hash от identity (`project_name` + `neo4j_uri` +
    `neo4j_database`), чтобы нестандартный запуск нескольких проектов в одном
    контейнере не пересёкся на одном marker-е. Пароль Neo4j в identity не входит.
    """
    identity = f"{settings.project_name}|{settings.neo4j_uri}|{settings.neo4j_database}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return _RELOAD_MARKER_DIR / f"full_metadata_reload_{digest}.json"


def _read_reload_marker(path: Path):
    """Прочитать marker.

    Возвращает:
      - None, если файла нет (reload в этом контейнере ещё не запускался);
      - dict с payload-ом при успешном чтении;
      - {"status": "corrupt"} при ошибке чтения/парсинга — трактуется как
        незавершённая/повреждённая попытка (fail closed), а не как отсутствие.
    """
    try:
        if not path.exists():
            return None
    except OSError:
        # Не смогли даже проверить наличие — считаем состояние неизвестным/повреждённым.
        return {"status": "corrupt"}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or "status" not in data:
            return {"status": "corrupt"}
        return data
    except (OSError, ValueError):
        return {"status": "corrupt"}


def _write_reload_marker(path: Path, status: str, **fields) -> None:
    """Атомарно записать marker со статусом started/succeeded/failed.

    Пишем во временный файл рядом и заменяем через os.replace. Бросает при любой
    ошибке записи — вызывающий обязан трактовать это как "marker создать нельзя"
    и не запускать destructive reload без marker-а.
    """
    payload = {
        "status": status,
        "project_name": settings.project_name,
        "neo4j_uri": settings.neo4j_uri,
        "neo4j_database": settings.neo4j_database,
    }
    payload.update(fields)

    os.makedirs(path.parent, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def _decide_forced_reload(path: Path):
    """Решение по one-shot marker-у для текущего контейнера.

    Возвращает (decision, message):
      - "run"   — marker-а нет, forced reload можно выполнить;
      - "skip"  — marker `succeeded`, флаг уже отработал в этом контейнере;
      - "abort" — marker `started`/`failed`/повреждён: предыдущая destructive
                  попытка не завершилась, стартовать нельзя (fail closed).
    """
    marker = _read_reload_marker(path)
    if marker is None:
        return "run", ""
    status = marker.get("status")
    if status == "succeeded":
        return (
            "skip",
            "FULL_METADATA_RELOAD уже отработал в этом контейнере (marker: %s). "
            "Обычный docker restart full reload не повторяет. Для нового full "
            "reload пересоздайте контейнер: "
            "docker compose up -d --force-recreate <service>." % path,
        )
    # started / failed / corrupt / любой неизвестный статус — fail closed.
    return (
        "abort",
        "Предыдущая попытка full reload в этом контейнере не завершилась "
        "(marker status=%s, %s). Сервер не запущен, чтобы не обслуживать "
        "потенциально частично очищенный граф. Устраните причину и пересоздайте "
        "контейнер: docker compose up -d --force-recreate <service>."
        % (status, path),
    )


def check_and_load_metadata():
    """
    Check if metadata is loaded in Neo4j, and load it if necessary.

    Uses settings.full_metadata_reload (env FULL_METADATA_RELOAD) to control drop-and-reload.
    Returns (success, loaded_now):
      - (True, False) — metadata already existed, ничего не делалось.
      - (True, True)  — full reload или initial load выполнен в этом процессе.
      - (False, False) — ошибка.
    `loaded_now=True` означает, что _init_incremental_baseline только что записал свежий baseline,
    и startup incremental one-shot в run_server должен быть пропущен.
    """
    loader = None
    # Init before try: the except block below reads `marker_started`, and an
    # exception may be raised earlier than the marker write (e.g. Neo4jLoader()
    # when Neo4j is down). Without this, an early error would raise
    # UnboundLocalError instead of the established (False, False) contract.
    marker_started = False
    marker_path = _reload_marker_path()
    try:
        # Initialize loader to check database status
        loader = Neo4jLoader()
        stats = loader.get_statistics()

        # Check if current project already has data (project-scoped)
        project_has_data = False
        try:
            res = loader.execute_query_readonly(
                "MATCH (p:Project {name: $name}) RETURN count(p) AS cnt",
                {"name": settings.project_name}
            )
            project_has_data = bool(res and res[0].get('cnt', 0) > 0)
        except Exception:
            # Fallback rationale:
            # - The read-only MATCH on Project may fail in restricted environments
            #   (RO users without access to system db, cluster maintenance, or when the
            #   driver falls back to a non-writable router).
            # - In that case we derive a coarse "has data" signal from aggregated project-scoped statistics.
            #   This may slightly over-report if multiple projects share the database, but it is sufficient
            #   to decide whether we must bootstrap an initial load for the current container.
            project_has_data = (
                stats.get('MetadataObject', 0) > 0 or
                stats.get('Attribute', 0) > 0
            )

        # Determine reload flag from settings/environment.
        # One-shot marker semantics (see _decide_forced_reload):
        #   - abort (incomplete/corrupt marker) fails closed regardless of the
        #     flag — a previous destructive reload did not finish, so the graph
        #     may be partially cleared and we must not serve it;
        #   - skip (succeeded marker) only matters when the flag is true: the
        #     forced reload already ran once in this container.
        reload_flag = settings.full_metadata_reload
        decision, msg = _decide_forced_reload(marker_path)

        if decision == "abort":
            logger.error(msg)
            return False, False

        if reload_flag and decision == "skip":
            logger.warning(msg)
            reload_flag = False

        if project_has_data and not reload_flag:
            logger.info("[OK] Project metadata already loaded in Neo4j")
            logger.info("   Objects: %s, Attributes: %s, Categories: %s",
                        stats.get('MetadataObject', 0),
                        stats.get('Attribute', 0),
                        stats.get('MetadataCategory', 0))
            loader.close()
            return True, False

        # Full reload vs first load:
        # - If reload_flag is True we will do a drop-and-reload for the current project.
        # - If False, we treat the project as not loaded and perform an initial import.
        # Note: cleaning existing project data happens inside Indexer.index_metadata()
        #       via the clear_db flag; we do it there to keep all DDL and write batches
        #       in one place and in the correct sequence.
        logger.info("Full metadata reload requested. Clearing existing data..." if reload_flag else "Neo4j database is empty. Loading metadata...")
        loader.close()

        indexer = Indexer()

        # Resolve metadata directory from settings (Indexer handles file discovery)
        metadata_dir = settings.metadata_directory
        metadata_source = getattr(settings, "metadata_source", "txt")
        logger.info("Using metadata source: %s, metadata directory: %s",
                    metadata_source, metadata_dir)

        # Validate inputs depending on source.
        if metadata_source == "xml":
            config_xml = settings.code_directory / "Configuration.xml"
            if not config_xml.exists():
                logger.error("XML mode: Configuration.xml not found.")
                logger.error("   - %s", config_xml)
                return False, False
        else:
            # Default TXT mode — require the metadata/ directory upfront.
            if not metadata_dir.exists():
                logger.error("Metadata directory not found.")
                logger.error("   - %s", settings.metadata_directory)
                return False, False

        # One-shot marker: record `started` right before the destructive reload,
        # but only after input validation passed. If the marker cannot be
        # written, refuse to run the forced reload (we would lose the one-shot
        # guarantee and could re-clear the graph on every restart).
        if reload_flag:
            try:
                _write_reload_marker(
                    marker_path,
                    "started",
                    started_at=datetime.now(timezone.utc).isoformat(),
                )
                marker_started = True
            except OSError as marker_err:
                logger.error(
                    "Cannot create full-reload marker at %s (%s). Forced full "
                    "reload aborted to preserve one-shot semantics.",
                    marker_path, marker_err,
                )
                return False, False

        # Index all supported files in directory (Indexer handles *.txt discovery)
        _t0 = time.perf_counter()
        result = indexer.index_metadata(directory=metadata_dir, clear_db=reload_flag)
        success = result.success  # IndexingResult.__bool__ also works
        _t = time.perf_counter() - _t0
        logger.info("index took %s", _fmt_duration(_t))

        # Invalidate the BSL code search sidecar for this project now that the
        # Neo4j graph was regenerated. Must run BEFORE the marker is promoted to
        # `succeeded`, so a failed reset (when BSL is enabled) fails closed and
        # the container does not serve the fresh graph behind a stale sidecar.
        if success:
            if not _reset_bsl_code_search_after_bulk_load():
                success = False

        # Baseline init для incremental loading — ДО promotion marker в `succeeded`.
        # После full reload записываем metadata + artifact baseline в SQLite state.
        # Fatal-решение гейтится на incremental_loading_enabled: при выключенном
        # incremental subsystem его baseline сервером не используется
        # (_run_startup_incremental и scheduler и так пропускаются), поэтому сбой —
        # non-fatal и сохраняет текущий bootstrap без incremental.
        if success:
            try:
                _init_incremental_baseline(result)
            except Exception:
                if settings.incremental_loading_enabled:
                    logger.exception(
                        "Incremental/artifact baseline init failed; full reload treated as failed"
                    )
                    success = False
                else:
                    logger.warning(
                        "Baseline init failed but INCREMENTAL_LOADING_ENABLED=false → non-fatal",
                        exc_info=True,
                    )

        # One-shot marker: promote to succeeded/failed по ИТОГОВОМУ success (после baseline).
        if marker_started:
            if success:
                _write_reload_marker(
                    marker_path,
                    "succeeded",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                _write_reload_marker(
                    marker_path,
                    "failed",
                    failed_at=datetime.now(timezone.utc).isoformat(),
                    error="index_metadata/baseline failed",
                )

        # Full reload неуспешен (index / sidecar reset / baseline при enabled) —
        # не запускаем server и фоновые индексаторы.
        if not success:
            return False, False

        logger.info("Metadata loaded successfully!")
        # Show updated statistics.
        # Open a fresh Neo4jLoader instance intentionally to read post-load counts
        # from a new session/connection, avoiding any stale state from the loader
        # used earlier only for pre-checks.
        loader = Neo4jLoader()
        stats = loader.get_statistics()
        logger.info("   Objects: %s, Attributes: %s, Categories: %s",
                    stats.get('MetadataObject', 0),
                    stats.get('Attribute', 0),
                    stats.get('MetadataCategory', 0))
        loader.close()

        # Heavy full-load work is done and IndexingResult is no longer needed
        # (incremental baseline above already consumed result.code_index /
        # result.bsl_data / snapshots). Drop the heavy references and return
        # allocator-retained memory to the OS before returning. Best-effort:
        # trim_process_memory never raises.
        return_value = (success, success)
        result = None
        indexer = None
        trim_process_memory(
            "metadata full load completed",
            enabled=settings.memory_trim_enabled,
        )
        return return_value

    except Exception as e:
        logger.error("Error checking/loading metadata: %s", e)
        # If a forced reload was already in progress, record the failure so a
        # plain restart in this container fails closed instead of silently
        # re-running the destructive reload.
        if marker_started:
            try:
                _write_reload_marker(
                    marker_path,
                    "failed",
                    failed_at=datetime.now(timezone.utc).isoformat(),
                    error=str(e)[:500],
                )
            except Exception:
                logger.exception("Could not record failed full-reload marker")
        return False, False
    finally:
        try:
            if loader:
                loader.close()
        except Exception:
            pass


def _preflight_artifact_baseline_or_exit(loaded_now: bool) -> None:
    """Синхронный fail-closed preflight готовности artifact baseline до run_server().

    Запускается только на restart-пути (`not loaded_now`) при включённом incremental:
    свежий full/initial load (`loaded_now=True`) только что записал baseline, повторно
    проверять нечего. Единый evaluator `evaluate_artifact_baseline_readiness` — тот же,
    что защищает scheduler. Любой fail-closed исход → ERROR + `sys.exit(1)`; тем самым
    vector embedding / BSL code search / object summaries / periodic scheduler не стартуют.
    """
    if loaded_now or not getattr(settings, "incremental_loading_enabled", False):
        return
    from pathlib import Path as _Path

    from incremental.state import ArtifactBaselineReadiness, IncrementalLoadingState

    metadata_source = getattr(settings, "metadata_source", "txt")
    state = IncrementalLoadingState(
        _Path(settings.incremental_loading_state_path), settings.project_name
    )
    try:
        readiness = state.evaluate_artifact_baseline_readiness(metadata_source)
    finally:
        state.close()

    if readiness in (
        ArtifactBaselineReadiness.READY,
    ):
        return

    action = (
        "Пересоздайте контейнер с FULL_METADATA_RELOAD=true: "
        "docker compose up -d --force-recreate <service>."
    )
    if readiness == ArtifactBaselineReadiness.SOURCE_MISMATCH:
        logger.error(
            "Artifact baseline preflight FAILED (project=%s, source=%s): METADATA_SOURCE "
            "изменён относительно записанного baseline. %s",
            settings.project_name, metadata_source, action,
        )
    elif readiness == ArtifactBaselineReadiness.BASELINE_NOT_INITIALIZED:
        logger.error(
            "Artifact baseline preflight FAILED (project=%s, source=%s): incremental "
            "baseline отсутствует (state потерян или не создавался). %s",
            settings.project_name, metadata_source, action,
        )
    else:  # FULL_RELOAD_REQUIRED
        logger.error(
            "Artifact baseline preflight FAILED (project=%s, source=%s): metadata baseline "
            "есть, но artifact baseline не завершён (completion stage отсутствует). %s",
            settings.project_name, metadata_source, action,
        )
    sys.exit(1)


def _clear_project_cli(project_name: str) -> int:
    """One-off maintenance mode: `python main.py --clear-project <name>`.

    Удаляет из Neo4j все данные указанного проекта (батчёванный
    loader.clear_project) и выходит, НЕ запуская MCP-сервер. Предназначен для
    decommission-сценария fleet-автоматизации: запуск one-off контейнером тем же
    образом, что и сервис (та же docker-сеть, тот же .env):

        docker run --rm --network <net> --env-file .env <image> \
            python main.py --clear-project kgg-do30-main

    Внимание: серверный state (storage/ volume, чекаут data/) этим не очищается —
    это зона decommission-скрипта. Здесь только граф Neo4j.
    Возвращает exit code: 0 — успех, 1 — ошибка подключения/удаления.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    project_name = (project_name or "").strip()
    if not project_name:
        logger.error("--clear-project requires a non-empty project name")
        return 1
    loader = Neo4jLoader()
    try:
        loader.connect()
        logger.info("Clearing Neo4j data for project: %s", project_name)
        loader.clear_project(project_name)
        logger.info("Project cleared: %s", project_name)
        return 0
    except Exception as e:
        logger.error("Failed to clear project %s: %s", project_name, e, exc_info=True)
        return 1
    finally:
        try:
            loader.close()
        except Exception:
            pass


def main():
    """Main entry point: auto-load metadata (if needed) and run MCP server.
    All configuration is via environment variables; see config.py and docker-compose.yml.

    Maintenance mode: `python main.py --clear-project <name>` — удалить данные
    проекта из Neo4j и выйти (см. _clear_project_cli)."""

    # Maintenance mode: разбирается ДО любого bootstrap'а сервера.
    if "--clear-project" in sys.argv:
        idx = sys.argv.index("--clear-project")
        name = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        sys.exit(_clear_project_cli(name))

    # Установка метода запуска процессов для PyInstaller
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass  # Уже установлен

    # Bootstrap logging for early messages from main.
    # If no handlers are configured yet (root defaults WARNING), ensure INFO/DEBUG go to stdout.
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        logging.basicConfig(
            level=logging.DEBUG if settings.enable_debug else logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )

    # Set up Neo4j debug log filtering
    setup_neo4j_debug_filtering()

    # Optional staggered startup to reduce concurrent handshakes/DDL storms
    if getattr(settings, "startup_delay_seconds", 0) and settings.startup_delay_seconds > 0:
        logger.info("Startup delay: %ss", settings.startup_delay_seconds)
        time.sleep(settings.startup_delay_seconds)

    logger.info("Starting: checking metadata and running MCP server")
    logger.info("=" * 80)

    success, loaded_now = check_and_load_metadata()
    if not success:
        logger.error("Failed to load metadata. Server will not be started.")
        sys.exit(1)

    # Fail-closed preflight: на restart-пути (без свежего reload) проверяем, что
    # artifact baseline готов для текущего source. Неполное/несогласованное состояние
    # завершает процесс до старта server pipeline.
    _preflight_artifact_baseline_or_exit(loaded_now)

    logger.info("Starting MCP server...")
    logger.info("=" * 80)
    run_server(skip_startup_incremental=loaded_now)


if __name__ == "__main__":
    main()
