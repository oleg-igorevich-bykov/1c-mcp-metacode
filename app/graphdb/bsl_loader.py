"""
BSLLoaderMixin: loads BSL module/routine signatures and DECLARES links.
Also links FormEvent -> Routine handlers (including CommonForms pass).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import logging
import json

from config import settings
from .cypher_templates import (
    CYPHER_UPSERT_MODULE,
    CYPHER_LINK_MODULE_OWNER_FORM,
    CYPHER_LINK_MODULE_OWNER_METADATAOBJECT,
    CYPHER_LINK_MODULE_OWNER_CONFIGURATION,
    CYPHER_LINK_MODULE_OWNER_COMMAND,
    CYPHER_UPSERT_ROUTINE,
    CYPHER_DECLARES_MODULE_TO_ROUTINE,
    CYPHER_DECLARES_COMMONMODULE_OWNER_TO_ROUTINE,
    # Extension decorator links
    CYPHER_CREATE_EXTENDS_ROUTINE,
    CYPHER_CREATE_EXTENDS_MODULE,
    # CALL GRAPH
    CYPHER_DELETE_CALLS_BY_CALLERS,
    CYPHER_MERGE_CALLS,
    # URL methods -> handlers
    CYPHER_LINK_URLMETHOD_HANDLER_EXPLICIT,
    # SSL API marking
    CYPHER_MARK_SSL_API_ROUTINES_BY_OWNERS,
)

logger = logging.getLogger(__name__)


class BSLLoaderMixin:
    def load_bsl_signatures(
        self,
        project_name: str,
        config_name: str,
        modules: List[Dict[str, Any]],
        routines: List[Dict[str, Any]],
        declares: List[Dict[str, Any]],
        common_declares: List[Dict[str, Any]],
        form_routines: Dict[str, List[Dict[str, Any]]] = None,
        do_linking: bool = True,
    ) -> None:
        """
        Load BSL module/routine signatures into Neo4j.
        Rules:
          - For Document/Form/Configuration modules:
              (Owner)-[:HAS_MODULE]->(Module)
              (Module)-[:DECLARES]->(Routine)
          - For CommonModules:
              No Module node; DECLARES goes from the CommonModule's MetadataObject directly to Routine.

        When do_linking is False, only nodes/relations for modules/routines/declares are upserted.
        All linking passes (Form events/controls, commands, URL methods) are skipped.
        """
        if not (modules or routines or declares or common_declares):
            return

        # Ensure essential constraints for fast MERGE/MATCH on Module/Routine ids
        # ensure_bsl_indexes suppressed: all indexes are created in create_indexes()
        # This avoids redundant DDL and reduces startup noise.
        # If needed, run create_indexes() once at configuration load stage.
        # (no-op)

        bs = settings.neo4j_batch_size
        # Normalize routine parameter fields to Neo4j-friendly types IN-PLACE to avoid memory duplication
        safe_routines: List[Dict[str, Any]] = []
        for r in routines or []:
            prms = r.get("params_json") or []
            try:
                names = [p.get("name") for p in prms if isinstance(p, dict)]
            except Exception:
                names = []
            # Modify in-place instead of copying to avoid doubling memory usage
            r["param_names"] = names
            try:
                r["params_json_str"] = json.dumps(prms, ensure_ascii=False)
            except Exception:
                r["params_json_str"] = "[]"
            # NOTE: Keep params_json for later use in CALLS resolution (indexer.py)
            # It will be removed after Phase 2 completes
            safe_routines.append(r)

        def _tx_modules(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_UPSERT_MODULE, rows=rows_chunk)
            labels_in_chunk = {r.get("owner_label") for r in rows_chunk}
            if "Form" in labels_in_chunk:
                tx.run(CYPHER_LINK_MODULE_OWNER_FORM, rows=rows_chunk)
            if "MetadataObject" in labels_in_chunk:
                tx.run(CYPHER_LINK_MODULE_OWNER_METADATAOBJECT, rows=rows_chunk)
            if "Configuration" in labels_in_chunk:
                tx.run(CYPHER_LINK_MODULE_OWNER_CONFIGURATION, rows=rows_chunk)
            if "Command" in labels_in_chunk:
                tx.run(CYPHER_LINK_MODULE_OWNER_COMMAND, rows=rows_chunk)

        def _tx_routines(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_UPSERT_ROUTINE, rows=rows_chunk)

        def _tx_declares_modules(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_DECLARES_MODULE_TO_ROUTINE, rows=rows_chunk)

        def _tx_common_declares(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_DECLARES_COMMONMODULE_OWNER_TO_ROUTINE, rows=rows_chunk)

        # 1) Modules in separate session
        if modules:
            with self.driver.session(database=settings.neo4j_database) as session:
                chunks = list(self._chunked(modules, bs)) or []
                total = len(chunks)
                logger.info("Loading BSL Modules: %d (chunks=%d, batch=%d)", len(modules), total, bs)
                for i, chunk in enumerate(chunks, 1):
                    logger.info("BSL Modules chunk %d/%d (size=%d)", i, total, len(chunk))
                    self._write(session, _tx_modules, chunk)

        # 2) Routines + declares with adaptive batching (limit by both count AND size)
        if safe_routines:
            # Adaptive batching: respect both max count (bs) and max size (MB)
            max_batch_mb = settings.neo4j_bsl_batch_max_mb
            max_size_bytes = int(max_batch_mb * 1024 * 1024)
            routine_chunks = []
            current_chunk = []
            current_size_bytes = 0

            for r in safe_routines:
                r_size = len(r.get("body", "").encode("utf-8"))
                # Start new chunk if either limit is exceeded
                if current_chunk and (len(current_chunk) >= bs or current_size_bytes + r_size > max_size_bytes):
                    routine_chunks.append(current_chunk)
                    current_chunk = []
                    current_size_bytes = 0
                current_chunk.append(r)
                current_size_bytes += r_size

            # Add last chunk
            if current_chunk:
                routine_chunks.append(current_chunk)

            total = len(routine_chunks)
            logger.info("Loading BSL Routines (adaptive: max_count=%d, max_size=%.2fMB): routines=%d, chunks=%d",
                       bs, max_batch_mb, len(safe_routines), total)
            all_declares = declares or []
            all_common_declares = common_declares or []

            # Pre-group once to avoid O(chunks × N) linear scans per chunk
            declares_by_id: Dict[str, List] = {}
            for d in all_declares:
                rid = d.get("routine_id")
                if rid:
                    declares_by_id.setdefault(rid, []).append(d)
            common_declares_by_id: Dict[str, List] = {}
            for d in all_common_declares:
                rid = d.get("routine_id")
                if rid:
                    common_declares_by_id.setdefault(rid, []).append(d)

            session = None
            refresh_interval = settings.neo4j_session_refresh_interval
            try:
                for i, r_chunk in enumerate(routine_chunks, 1):
                    # Recreate session periodically to prevent memory buildup
                    if session is None or (refresh_interval > 0 and (i - 1) % refresh_interval == 0 and i > 1):
                        if session is not None:
                            session.close()
                            logger.debug("Closed session after chunk %d, reopening", i - 1)
                        session = self.driver.session(database=settings.neo4j_database)
                    # Calculate chunk size in bytes for diagnostics
                    chunk_size_bytes = sum(len(r.get("body", "").encode("utf-8")) for r in r_chunk)
                    chunk_size_mb = chunk_size_bytes / (1024 * 1024)
                    logger.info("BSL Routines chunk %d/%d (routines=%d, body_size=%.2fMB)",
                               i, total, len(r_chunk), chunk_size_mb)
                    # 1) Upsert routines for this chunk
                    self._write(session, _tx_routines, r_chunk)

                    # 2) Interleave DECLARES for routines of this chunk
                    ids = {r.get("id") for r in r_chunk if r.get("id")}
                    if ids:
                        # Module -> Routine
                        if declares_by_id:
                            sub = [d for rid in ids for d in declares_by_id.get(rid, [])]
                            if sub:
                                for d_chunk in self._chunked(sub, bs):
                                    self._write(session, _tx_declares_modules, d_chunk)
                        # CommonModule owner -> Routine
                        if common_declares_by_id:
                            subc = [d for rid in ids for d in common_declares_by_id.get(rid, [])]
                            if subc:
                                for dc_chunk in self._chunked(subc, bs):
                                    self._write(session, _tx_common_declares, dc_chunk)
            finally:
                if session is not None:
                    session.close()

        if do_linking:
            # 3) Link FormEvents to Routines and Commands (config-scoped)
            with self.driver.session(database=settings.neo4j_database) as session:
                self._create_form_event_links(session, project_name, config_name, form_routines or {})
                # 3b) Also link Commands to Routines based on Command.Action property
                self._create_command_links(session, project_name, config_name, form_routines or {})

            # 3c) Link UrlMethod handlers (explicit only; no linking when handler is empty)
            linked_url_handlers = 0
            try:
                with self.driver.session(database=settings.neo4j_database) as session:
                    res = session.run(
                        CYPHER_LINK_URLMETHOD_HANDLER_EXPLICIT,
                        project_name=project_name,
                        config_name=config_name,
                    )
                    if res:
                        rec = res.single()
                        if rec:
                            linked_url_handlers = rec.get("matched", 0)
                    logger.info("Linked UrlMethod handlers (explicit): %d", linked_url_handlers)
            except Exception as e:
                logger.error("UrlMethod handler linking failed: %s", e, exc_info=True)

        logger.info(
            "BSL load finished: modules=%d, routines=%d, declares=%d, common_declares=%d",
            len(modules or []), len(routines or []), len(declares or []), len(common_declares or [])
        )

    def link_form_events_and_commands(self, project_name: str, config_name: str, form_routines: Dict[str, List[Dict[str, Any]]] = None) -> None:
        """
        Post-phase linking config-scoped:
          - (FormEvent|FormControl Event)-[:HAS_HANDLER]->(Routine)
          - (Command)-[:HAS_HANDLER]->(Routine)
          - URLMethod explicit handlers
        """
        # Link form events and commands using provided form_routines where available
        with self.driver.session(database=settings.neo4j_database) as session:
            self._create_form_event_links(session, project_name, config_name, form_routines or {})
            self._create_command_links(session, project_name, config_name, form_routines or {})

        # Link UrlMethod handlers (explicit only)
        linked_url_handlers = 0
        try:
            with self.driver.session(database=settings.neo4j_database) as session:
                res = session.run(
                    CYPHER_LINK_URLMETHOD_HANDLER_EXPLICIT,
                    project_name=project_name,
                    config_name=config_name,
                )
                if res:
                    rec = res.single()
                    if rec:
                        linked_url_handlers = rec.get("matched", 0)
            logger.info("Linked UrlMethod handlers (explicit): %d", linked_url_handlers)
        except Exception as e:
            logger.error("UrlMethod handler linking failed: %s", e, exc_info=True)

    def collect_form_routines_for_forms(
        self,
        project_name: str,
        config_name: str,
        form_qns: List[str],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Прочитать существующие Routine из Neo4j для форм без BSL re-parse.

        Закрывает сценарий «Form.xml-only change без BSL delta»: после cleanup
        FormEventAction handler edges и rebuild Form node нужно знать имена
        процедур, которые есть в графе как Routine owner_qn == form_qn (regular
        form) или owner_qn == object_qn (когда form-level events ссылаются на
        процедуры модуля объекта). Для CommonForms owner_qn = MetadataObject QN.

        Возвращает: {form_qn: [{"name": routine_name}, ...]} — тот же формат, что
        ожидают `_create_form_event_links` / `_create_command_links`.
        """
        if not form_qns:
            return {}

        # Для каждой формы owner_qn варианты: form_qn (regular form), object_qn
        # (=split form_qn '/Form/'[0]). split возвращает MetadataObject QN для
        # common form тоже — это правильно.
        cypher = """
        UNWIND $form_qns AS form_qn
        WITH form_qn,
             form_qn AS o1,
             split(form_qn, '/Form/')[0] AS o2
        UNWIND CASE WHEN o1 = o2 THEN [o1] ELSE [o1, o2] END AS owner_qn
        MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: owner_qn})
        RETURN form_qn AS form_qn, r.name AS routine_name
        """
        result: Dict[str, List[Dict[str, Any]]] = {}
        try:
            with self.driver.session(database=settings.neo4j_database) as session:
                res = session.run(
                    cypher,
                    form_qns=list(form_qns),
                    project_name=project_name,
                    config_name=config_name,
                )
                for rec in res or []:
                    fq = rec.get("form_qn")
                    name = rec.get("routine_name")
                    if fq and name:
                        result.setdefault(fq, []).append({"name": name})
        except Exception as e:
            logger.error("collect_form_routines_for_forms failed [%s]: %s", config_name, e, exc_info=True)
        return result

    def run_deferred_extensions_linking(
        self,
        project_name: str,
        deferred: list,
    ) -> None:
        """Per-extension config-scoped form/command/URL linking.

        После того как `_create_form_event_links` / `_create_command_links` стали
        config-scoped, full-load deferred linking тоже идёт per-extension —
        иначе merged-вызов с form_routines из всех extensions нельзя
        корректно отфильтровать в одном config-scoped query.
        """
        for config_name, form_routines in deferred:
            try:
                with self.driver.session(database=settings.neo4j_database) as session:
                    self._create_form_event_links(session, project_name, config_name, form_routines or {})
                    self._create_command_links(session, project_name, config_name, form_routines or {})
            except Exception as e:
                logger.error("Deferred form/command linking failed [%s]: %s", config_name, e, exc_info=True)

            try:
                with self.driver.session(database=settings.neo4j_database) as session:
                    res = session.run(
                        CYPHER_LINK_URLMETHOD_HANDLER_EXPLICIT,
                        project_name=project_name,
                        config_name=config_name,
                    )
                    linked = 0
                    if res:
                        rec = res.single()
                        if rec:
                            linked = rec.get("matched", 0)
                    logger.info("Linked UrlMethod handlers [%s]: %d", config_name, linked)
            except Exception as e:
                logger.error("UrlMethod handler linking failed [%s]: %s", config_name, e, exc_info=True)

    def _create_form_event_links(self, session, project_name: str, config_name: str, form_routines: Dict[str, List[Dict[str, Any]]]):
        """Create (FormEventAction)-[:HAS_HANDLER]->(Routine) links for matching
        names, scoped по config_name. Без config_name filter CommonForms pass и
        Routine match шли бы по всему project, сшивая extensions с base."""

        links = []
        for form_qn, routines in form_routines.items():
            for r in routines:
                routine_name = r.get("name")
                if routine_name:
                    links.append({"form_qn": form_qn, "routine_name": routine_name})

        bs = settings.neo4j_batch_size
        if links:
            cypher_form_events = """
            UNWIND $rows AS row
            WITH row,
                 row.form_qn AS o1,
                 split(row.form_qn, '/Form/')[0] AS o2
            UNWIND CASE WHEN o1 = o2 THEN [o1] ELSE [o1, o2] END AS owner_qn
            MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: owner_qn, name: row.routine_name})
            MATCH (f:Form {qualified_name: row.form_qn})
            WHERE f.config_name = $config_name
            MATCH (f)-[:HAS_EVENT]->(fe:FormEvent)
                     -[:HAS_EVENT_ACTION]->(a:FormEventAction)
            WHERE a.handler_name = row.routine_name
               OR (a.handler_name IS NULL OR a.handler_name = '') AND fe.name = row.routine_name
            MERGE (a)-[:HAS_HANDLER]->(r)
            """
            cypher_ctrl_events = """
            UNWIND $rows AS row
            WITH row,
                 row.form_qn AS o1,
                 split(row.form_qn, '/Form/')[0] AS o2
            UNWIND CASE WHEN o1 = o2 THEN [o1] ELSE [o1, o2] END AS owner_qn
            MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: owner_qn, name: row.routine_name})
            MATCH (f:Form {qualified_name: row.form_qn})
            WHERE f.config_name = $config_name
            MATCH (f)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[:HAS_EVENT]->(fe:FormEvent)
                                                             -[:HAS_EVENT_ACTION]->(a:FormEventAction)
            WHERE a.handler_name = row.routine_name
               OR (a.handler_name IS NULL OR a.handler_name = '') AND fe.name = row.routine_name
            MERGE (a)-[:HAS_HANDLER]->(r)
            """
            for chunk in self._chunked(links, bs):
                self._write(session, lambda tx, payload: tx.run(cypher_form_events, rows=payload, project_name=project_name, config_name=config_name), chunk)
                self._write(session, lambda tx, payload: tx.run(cypher_ctrl_events, rows=payload, project_name=project_name, config_name=config_name), chunk)

        # CommonForms pass — scoped по config_name (иначе extension common forms
        # получили бы handler edges на base routines и наоборот).
        owners_res = session.run("""
            MATCH (m:MetadataObject {project_name: $project_name, config_name: $config_name, category_name: 'ОбщиеФормы'})
            RETURN m.qualified_name AS obj_qn
        """, project_name=project_name, config_name=config_name)
        owners = [rec["obj_qn"] for rec in owners_res] if owners_res else []
        if owners:
            cypher_cf_events = """
            UNWIND $owners AS obj_qn
            MATCH (m:MetadataObject {qualified_name: obj_qn})
            WHERE m.config_name = $config_name
            MATCH (m)-[:HAS_EVENT]->(fe:FormEvent)
                     -[:HAS_EVENT_ACTION]->(a:FormEventAction)
            WITH obj_qn, fe, a,
                 CASE WHEN a.handler_name IS NOT NULL AND a.handler_name <> ''
                      THEN a.handler_name ELSE fe.name END AS match_name
            MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: obj_qn, name: match_name})
            MERGE (a)-[:HAS_HANDLER]->(r)
            """
            cypher_cf_ctrl_events = """
            UNWIND $owners AS obj_qn
            MATCH (m:MetadataObject {qualified_name: obj_qn})
            WHERE m.config_name = $config_name
            MATCH (m)-[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)-[:HAS_EVENT]->(fe:FormEvent)
                                                             -[:HAS_EVENT_ACTION]->(a:FormEventAction)
            WITH obj_qn, fe, a,
                 CASE WHEN a.handler_name IS NOT NULL AND a.handler_name <> ''
                      THEN a.handler_name ELSE fe.name END AS match_name
            MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: obj_qn, name: match_name})
            MERGE (a)-[:HAS_HANDLER]->(r)
            """
            for owner_chunk in self._chunked(owners, bs):
                self._write(
                    session,
                    lambda tx, payload: tx.run(cypher_cf_events, owners=payload["owners"], project_name=payload["project_name"], config_name=payload["config_name"]),
                    {"owners": owner_chunk, "project_name": project_name, "config_name": config_name}
                )
                self._write(
                    session,
                    lambda tx, payload: tx.run(cypher_cf_ctrl_events, owners=payload["owners"], project_name=payload["project_name"], config_name=payload["config_name"]),
                    {"owners": owner_chunk, "project_name": project_name, "config_name": config_name}
                )

        logger.info("Created FormEventAction -> Routine links scoped [%s] (incl. CommonForms pass)", config_name)

    def _create_command_links(self, session, project_name: str, config_name: str, form_routines: Dict[str, List[Dict[str, Any]]]):
        """
        Create (Command)-[:HAS_HANDLER]->(Routine) links scoped по config_name.
        Использует Command.`Действие` (Action) с fallback на command.name. Покрывает:
          - Form-level commands (Form -> Command)
          - CommonForms / MetadataObject-level commands (MetadataObject -> Command)
          - Command-owned modules где Routine.owner_qn == Command.qualified_name
        Все global passes фильтруются по config_name, иначе extension commands
        получили бы handler edges на base routines и наоборот.
        """
        # Build form-specific link rows from provided form_routines (form_qn -> [routines])
        links = []
        for form_qn, routines in (form_routines or {}).items():
            for r in (routines or []):
                routine_name = r.get("name")
                if routine_name:
                    links.append({"form_qn": form_qn, "routine_name": routine_name})

        bs = settings.neo4j_batch_size
        linked_form = 0
        linked_form_action = 0
        if links:
            cypher_cmd = """
            UNWIND $rows AS row
            WITH row,
                 row.form_qn AS o1,
                 split(row.form_qn, '/Form/')[0] AS o2
            UNWIND CASE WHEN o1 = o2 THEN [o1] ELSE [o1, o2] END AS owner_qn
            MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: owner_qn, name: row.routine_name})
            MATCH (f:Form {qualified_name: row.form_qn})
            WHERE f.config_name = $config_name
            MATCH (f)-[:HAS_COMMAND]->(cmd:Command)
            WHERE cmd.`Действие` = row.routine_name
               OR ((cmd.`Действие` IS NULL OR trim(cmd.`Действие`) = '') AND coalesce(cmd.name, '') = row.routine_name)
            MERGE (cmd)-[:HAS_HANDLER]->(r)
            RETURN count(*) AS matched
            """
            cypher_cmd_action = """
            UNWIND $rows AS row
            WITH row,
                 row.form_qn AS o1,
                 split(row.form_qn, '/Form/')[0] AS o2
            UNWIND CASE WHEN o1 = o2 THEN [o1] ELSE [o1, o2] END AS owner_qn
            MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: owner_qn, name: row.routine_name})
            MATCH (f:Form {qualified_name: row.form_qn})
            WHERE f.config_name = $config_name
            MATCH (f)-[:HAS_COMMAND]->(cmd:Command)
            WHERE row.routine_name IN coalesce(cmd.action_handlers, [])
            MERGE (cmd)-[:HAS_HANDLER]->(r)
            RETURN count(*) AS matched
            """
            for chunk in self._chunked(links, bs):
                try:
                    res = session.run(cypher_cmd, rows=chunk, project_name=project_name, config_name=config_name)
                    if res:
                        rec = res.single()
                        if rec:
                            linked_form += rec.get("matched", 0) or 0
                except Exception as e:
                    logger.warning("Form command linking failed [%s]: %s", config_name, e)

                try:
                    res = session.run(cypher_cmd_action, rows=chunk, project_name=project_name, config_name=config_name)
                    if res:
                        rec = res.single()
                        if rec:
                            linked_form_action += rec.get("matched", 0) or 0
                except Exception as e:
                    logger.warning("Form command linking (action_handlers) failed [%s]: %s", config_name, e)

        logger.info("Linked Form Command handlers (Action or by name) [%s]: %d", config_name, linked_form)
        logger.info("Linked Form Command handlers (action_handlers) [%s]: %d", config_name, linked_form_action)

        # 2) Global linking attempts for other command ownership patterns — scoped по config_name.

        # 2a) Link commands to routines declared in command-owned modules:
        #     (cmd:Command) -> owner_qn == cmd.qualified_name
        try:
            res = session.run(
                """
                MATCH (cmd:Command {project_name: $project_name, config_name: $config_name})
                WHERE cmd.`Действие` IS NOT NULL AND trim(cmd.`Действие`) <> ''
                MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: cmd.qualified_name, name: cmd.`Действие`})
                MERGE (cmd)-[:HAS_HANDLER]->(r)
                RETURN count(*) AS matched
                """,
                project_name=project_name, config_name=config_name,
            )
            linked_by_cmd_owner = 0
            if res:
                rec = res.single()
                if rec:
                    linked_by_cmd_owner = rec.get("matched", 0)
            logger.info("Linked Command handlers by command-owned modules (explicit Action) [%s]: %d", config_name, linked_by_cmd_owner)
        except Exception as e:
            logger.warning("Command handler linking (by command owner, explicit Action) failed [%s]: %s", config_name, e)

        # 2a-bis) Link commands to standard handler "ОбработкаКоманды" in command-owned modules
        try:
            res = session.run(
                """
                MATCH (cmd:Command {project_name: $project_name, config_name: $config_name})
                MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: cmd.qualified_name, name: 'ОбработкаКоманды'})
                MERGE (cmd)-[:HAS_HANDLER]->(r)
                RETURN count(*) AS matched
                """,
                project_name=project_name, config_name=config_name,
            )
            linked_by_standard_handler = 0
            if res:
                rec = res.single()
                if rec:
                    linked_by_standard_handler = rec.get("matched", 0)
            logger.info("Linked Command handlers by standard handler 'ОбработкаКоманды' [%s]: %d", config_name, linked_by_standard_handler)
        except Exception as e:
            logger.warning("Command handler linking (standard handler) failed [%s]: %s", config_name, e)

        # 2b) Link commands attached to MetadataObject (including object-level commands)
        try:
            res = session.run(
                """
                MATCH (m:MetadataObject {project_name: $project_name, config_name: $config_name})-[:HAS_COMMAND]->(cmd:Command)
                WHERE cmd.`Действие` IS NOT NULL AND trim(cmd.`Действие`) <> ''
                MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: m.qualified_name, name: cmd.`Действие`})
                MERGE (cmd)-[:HAS_HANDLER]->(r)
                RETURN count(*) AS matched
                """,
                project_name=project_name, config_name=config_name,
            )
            linked_meta_explicit = 0
            if res:
                rec = res.single()
                if rec:
                    linked_meta_explicit = rec.get("matched", 0)
            logger.info("Linked Command handlers (metadata owner, explicit Action) [%s]: %d", config_name, linked_meta_explicit)
        except Exception as e:
            logger.warning("Command handler linking (metadata explicit) failed [%s]: %s", config_name, e)

        try:
            res = session.run(
                """
                MATCH (m:MetadataObject {project_name: $project_name, config_name: $config_name})-[:HAS_COMMAND]->(cmd:Command)
                WHERE cmd.`Действие` IS NULL OR trim(cmd.`Действие`) = ''
                MATCH (r:Routine {project_name: $project_name, config_name: $config_name, owner_qn: m.qualified_name, name: coalesce(cmd.name,'')})
                WHERE coalesce(cmd.name,'') <> ''
                MERGE (cmd)-[:HAS_HANDLER]->(r)
                RETURN count(*) AS matched
                """,
                project_name=project_name, config_name=config_name,
            )
            linked_meta_byname = 0
            if res:
                rec = res.single()
                if rec:
                    linked_meta_byname = rec.get("matched", 0)
            logger.info("Linked Command handlers (metadata owner, by command.name) [%s]: %d", config_name, linked_meta_byname)
        except Exception as e:
            logger.warning("Command handler linking (metadata by name) failed [%s]: %s", config_name, e)


    def _mark_ssl_api_routines_by_owners(self, session, project_name: str, owners_qn: List[str]) -> int:
        """
        Mark routines as SSL API for the provided list of owner qualified_names.
        Only routines in 'ПрограммныйИнтерфейс' area (including nested) will be marked.
        """
        try:
            if not owners_qn:
                return 0
            res = session.run(
                CYPHER_MARK_SSL_API_ROUTINES_BY_OWNERS,
                project_name=project_name,
                owners_qn=list(owners_qn),
            )
            if res:
                rec = res.single()
                if rec:
                    return rec.get("marked_count", 0)
            return 0
        except Exception as e:
            logger.error("SSL API marking by owners failed: %s", e, exc_info=True)
            return 0

    def load_bsl_calls(
        self,
        project_name: str,
        rows: List[Dict[str, Any]],
        caller_ids: List[str],
        lease: Any = None,
    ) -> None:
        """
        Load resolved CALLS edges between routines.

        Args:
          project_name: Project scope for MATCH/cleanup guards
          rows: list of {project_name, caller_id, callee_id, kind, count, lines[]}
          caller_ids: list of caller routine ids whose outgoing CALLS must be refreshed
          lease: Optional LockLease — main-thread caller передаёт scheduler lease,
            метод вызывает `lease.heartbeat()` между chunked Neo4j writes.
        """
        if not (rows or caller_ids):
            return

        bs = settings.neo4j_batch_size

        # Normalize inputs to builtins-only containers to satisfy Neo4j/packstream strict typing
        safe_callers: List[str] = sorted({str(cid) for cid in (caller_ids or []) if cid})
        safe_rows: List[Dict[str, Any]] = []
        for row in (rows or []):
            try:
                safe_rows.append({
                    "project_name": str(row.get("project_name") or project_name or ""),
                    "caller_id": str(row.get("caller_id") or ""),
                    "callee_id": str(row.get("callee_id") or ""),
                    "kind": str(row.get("kind") or ""),
                    "count": int(row.get("count") or 0),
                    "lines": [int(x) for x in (row.get("lines") or []) if x is not None],
                })
            except Exception:
                # Skip malformed rows
                continue

        def _tx_delete(tx, ids_chunk: List[str]):
            tx.run(CYPHER_DELETE_CALLS_BY_CALLERS, ids=ids_chunk, project_name=project_name)

        def _tx_merge(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_MERGE_CALLS, rows=rows_chunk)

        with self.driver.session(database=settings.neo4j_database) as session:
            # 1) Cleanup old CALLS for provided callers
            if safe_callers:
                for chunk in self._chunked(safe_callers, bs):
                    self._write(session, _tx_delete, chunk)
                    if lease is not None:
                        lease.heartbeat()

            # 2) Insert/merge new CALLS
            if safe_rows:
                for chunk in self._chunked(safe_rows, bs):
                    self._write(session, _tx_merge, chunk)
                    if lease is not None:
                        lease.heartbeat()

        try:
            logger.info(
                "BSL CALLS loaded: callers=%d, edges=%d",
                len(set(safe_callers or [])),
                len(safe_rows or []),
            )
        except Exception:
            pass

    def create_extension_routine_links(
        self,
        settings,
        ext_config_name: str,
        base_config_name: str,
    ) -> int:
        """
        Создаёт EXTENDS_ROUTINE рёбра от рутин расширения к базовым процедурам.

        Args:
            settings: объект настроек (.project_name, .neo4j_database)
            ext_config_name: имя конфига расширения с маркером $ext$
            base_config_name: имя базовой конфигурации

        Returns:
            Количество созданных рёбер
        """
        find_q = """
        MATCH (r:Routine {project_name: $project_name, config_name: $ext_config_name})
        WHERE r.decorator_target <> ''
        RETURN r.id AS id, r.owner_qn AS owner_qn,
               r.decorator_type AS decorator_type,
               r.decorator_target AS decorator_target
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            records = session.run(
                find_q,
                project_name=settings.project_name,
                ext_config_name=ext_config_name,
            ).data()

        if not records:
            return 0

        # QN-трансформация: повторяем паттерн ExtensionRelationshipsBuilder —
        # заменяем только второй сегмент пути (index 1), не делаем глобальный string replace
        def _to_base_qn(qn: str) -> str:
            parts = qn.split("/")
            if len(parts) >= 2 and parts[1] == ext_config_name:
                parts[1] = base_config_name
            return "/".join(parts)

        rows = [
            {
                "ext_routine_id": rec["id"],
                "base_owner_qn":  _to_base_qn(rec["owner_qn"]),
                "decorator_type": rec["decorator_type"],
                "decorator_target": rec["decorator_target"],
            }
            for rec in records
        ]

        bs = settings.neo4j_batch_size

        def _tx_extends(tx, chunk):
            res = tx.run(CYPHER_CREATE_EXTENDS_ROUTINE, rows=chunk, project_name=settings.project_name)
            return res.consume().counters.relationships_created

        created = 0
        with self.driver.session(database=settings.neo4j_database) as session:
            for chunk in self._chunked(rows, bs):
                created += self._write(session, _tx_extends, chunk) or 0
        return created

    def create_extension_module_links(
        self,
        settings,
        ext_config_name: str,
        base_config_name: str,
    ) -> int:
        """
        Создаёт EXTENDS_MODULE рёбра от Module-узлов расширения к Module-узлам
        базовой конфигурации.

        Returns:
            Количество созданных рёбер
        """
        find_q = """
        MATCH (m:Module {project_name: $project_name, config_name: $ext_config_name})
        RETURN m.id AS id, m.owner_qn AS owner_qn, m.module_type AS module_type, m.name AS name
        """
        with self.driver.session(database=settings.neo4j_database) as session:
            records = session.run(
                find_q,
                project_name=settings.project_name,
                ext_config_name=ext_config_name,
            ).data()

        if not records:
            return 0

        def _to_base_qn(qn: str) -> str:
            parts = qn.split("/")
            if len(parts) >= 2 and parts[1] == ext_config_name:
                parts[1] = base_config_name
            return "/".join(parts)

        rows = [
            {
                "ext_module_id": rec["id"],
                "base_owner_qn": _to_base_qn(rec["owner_qn"]),
                "module_type":   rec["module_type"],
                "module_name":   rec["name"],
            }
            for rec in records
        ]

        bs = settings.neo4j_batch_size

        def _tx_extends_module(tx, chunk):
            res = tx.run(CYPHER_CREATE_EXTENDS_MODULE, rows=chunk, project_name=settings.project_name)
            return res.consume().counters.relationships_created

        created = 0
        with self.driver.session(database=settings.neo4j_database) as session:
            for chunk in self._chunked(rows, bs):
                created += self._write(session, _tx_extends_module, chunk) or 0
        return created