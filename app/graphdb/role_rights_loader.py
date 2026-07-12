"""
RoleRightsLoaderMixin: loads Role rights both in simple 'object_qn/right' mode and
with precise target resolution to concrete nodes (MetadataObject/Attribute/TabularPart/Resource/
Dimension/Command/Form/Configuration).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional
import logging

from config import settings
from .cypher_templates import (
    CYPHER_ROLE_RIGHTS_GRANTS_ACCESS_SIMPLE,
    CYPHER_ROLE_RIGHTS_UPDATE_ROLE_FLAGS,
    cypher_role_rights_grants_access_to,
    cypher_role_rights_grants_access_to_ext,
)

logger = logging.getLogger(__name__)


class RoleRightsLoaderMixin:
    def load_role_rights(self, rows: List[Dict[str, Any]]) -> None:
        """
        Load Role rights parsed from Roles/*/Ext/Rights.xml as relationships:
          (roleMo:MetadataObject)-[rel:GRANTS_ACCESS_TO {object_qn, right}]->(target:MetadataObject)
        Relationship properties:
          - right_en: original English key
          - allowed: boolean
          - condition: optional string
          - object_full: original full object path from Rights.xml
        Idempotent via MERGE on (object_qn, right).
        """
        if not rows:
            logger.info("No Role rights rows to load")
            return

        bs = settings.neo4j_batch_size
        total = len(rows)
        chunks_total = (total + bs - 1) // bs
        logger.info("Loading Role rights (GRANTS_ACCESS_TO): rows=%d, batch=%d, chunks=%d", total, bs, chunks_total)

        def _tx_once(tx, payload):
            tx.run(CYPHER_ROLE_RIGHTS_GRANTS_ACCESS_SIMPLE, rows=payload)

        with self.driver.session(database=settings.neo4j_database) as session:
            i = 0
            for chunk in self._chunked(rows, bs):
                i += 1
                logger.info("Role rights chunk %d/%d (size=%d)", i, chunks_total, len(chunk))
                self._write(session, _tx_once, chunk)

        logger.info("Loaded Role rights GRANTS_ACCESS_TO: %d", total)

    def load_role_rights_targets(self, rows: List[Dict[str, Any]]) -> None:
        """
        Load Role rights with precise target resolution:
          One GRANTS_ACCESS_TO relationship per (Role, TargetNode) where TargetNode can be:
            - MetadataObject, Attribute, TabularPart, Resource, Dimension, Command, Form, Configuration
        Relationship properties are per-right with EN prefix:
          - K_allowed: bool
          - K_ru: str
          - K_has_condition: bool
          - K_condition: str (only when non-empty)
        Plus service fields:
          - rights_present_en: list[str]
          - target_kind: str (label name)
          - target_qn: str (qualified_name of target node, for diagnostics)
        """
        if not rows:
            logger.info("No Role rights rows to load")
            return

        # Collect role-level flags from rows (identical within one Rights.xml per role)
        role_flags: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            rq = r.get("role_qn") or ""
            if not rq:
                continue
            if rq not in role_flags:
                role_flags[rq] = {
                    "setForNewObjects": bool(r.get("setForNewObjects", False)),
                    "setForAttributesByDefault": bool(r.get("setForAttributesByDefault", False)),
                    "independentRightsOfChildObjects": bool(r.get("independentRightsOfChildObjects", True)),
                }

        # Persist role flags on role nodes
        if role_flags:
            role_props_rows = [{"role_qn": rq, **flags} for rq, flags in role_flags.items()]
            with self.driver.session(database=settings.neo4j_database) as session:
                def _tx_flags(tx, payload):
                    tx.run(CYPHER_ROLE_RIGHTS_UPDATE_ROLE_FLAGS, rows=payload)
                self._write(session, _tx_flags, role_props_rows)

        # Aggregate by (role_qn, target_label, target_qn)
        groups: Dict[tuple, Dict[str, Any]] = {}
        for row in rows:
            try:
                role_qn = row.get("role_qn") or ""
                object_qn = row.get("object_qn") or ""
                object_full = row.get("object_full") or ""
                right_en = row.get("right_en") or ""
                right_ru = row.get("right_ru") or right_en
                allowed = bool(row.get("allowed"))
                condition_raw = row.get("condition")
                condition = condition_raw.strip() if isinstance(condition_raw, str) else ""
                # Resolve precise target
                target_label, target_qn = self._resolve_rr_target(object_full, object_qn, role_qn)

                # Enforce independentRightsOfChildObjects: when False, skip child targets entirely
                indep = role_flags.get(role_qn, {}).get("independentRightsOfChildObjects", True)
                if not indep and target_label in {"Attribute","TabularPart","Resource","Dimension","Command","Form"}:
                    # Skip this right entry (children inherit implicitly; explicit child links are not created)
                    continue

                key = (role_qn, target_label, target_qn)
                g = groups.get(key)
                if g is None:
                    g = {
                        "role_qn": role_qn,
                        "target_label": target_label,
                        "target_qn": target_qn,
                        "props": {
                            "rights_present_en": set(),
                            "target_kind": target_label,
                            "target_qn": target_qn,
                            "object_full_all": set(),
                        }
                    }
                    groups[key] = g
                props = g["props"]
                # Track present right and source paths
                props["rights_present_en"].add(right_en)
                if object_full:
                    props["object_full_all"].add(object_full)
                # Per-right properties with EN prefix
                k_allowed = f"{right_en}_allowed"
                k_ru = f"{right_en}_ru"
                k_has = f"{right_en}_has_condition"
                k_cond = f"{right_en}_condition"

                # Deny has priority if collisions occur
                if k_allowed in props:
                    if props[k_allowed] is True and allowed is False:
                        props[k_allowed] = False
                else:
                    props[k_allowed] = allowed

                # Set RU name (only if not set yet)
                if k_ru not in props:
                    props[k_ru] = right_ru

                # has_condition flag per-right
                current_has = bool(props.get(k_has, False))
                new_has = True if condition else False
                props[k_has] = current_has or new_has

                # condition text (store when non-empty; keep first if already present)
                if condition and (k_cond not in props or not props[k_cond]):
                    props[k_cond] = condition

            except Exception as e:
                logger.warning("Skip invalid rights row due to error: %s", e)

        if not groups:
            logger.info("No resolvable Role rights groups to load")
            return

        # Build payloads per target label
        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for (_, target_label, _), g in groups.items():
            props = g["props"]
            # finalize sets -> lists
            try:
                if isinstance(props.get("rights_present_en"), set):
                    props["rights_present_en"] = sorted(list(props["rights_present_en"]))
                if isinstance(props.get("object_full_all"), set):
                    props["object_full_all"] = sorted(list(props["object_full_all"]))
            except Exception:
                pass
            buckets.setdefault(target_label, []).append({
                "role_qn": g["role_qn"],
                "target_qn": g["target_qn"],
                "props": props,
            })

        bs = settings.neo4j_batch_size
        with self.driver.session(database=settings.neo4j_database) as session:
            def _tx_for_label_once(tx, label: str, rows_chunk: List[Dict[str, Any]]):
                if not rows_chunk:
                    return
                tx.run(cypher_role_rights_grants_access_to(label), rows=rows_chunk)

            # Execute per label to use label-specific index on qualified_name
            for label, payload in buckets.items():
                total = len(payload)
                chunks_total = (total + bs - 1) // bs if total else 0
                logger.info("Role rights targets [%s]: rows=%d, batch=%d, chunks=%d", label, total, bs, chunks_total)
                i = 0
                for chunk in self._chunked(payload, bs):
                    i += 1
                    logger.info("Role rights chunk %d/%d for label %s (size=%d)", i, chunks_total, label, len(chunk))
                    self._write(session, _tx_for_label_once, label, chunk)

        logger.info("Loaded Role rights GRANTS_ACCESS_TO targets: %d", sum(len(v) for v in buckets.values()))

    # --- Shared helpers extracted from load_role_rights_targets ---

    @staticmethod
    def _proj_cfg_from_rr_qn(qn: str) -> Tuple[str, str]:
        try:
            parts = (qn or "").split("/")
            return parts[0], parts[1]
        except Exception:
            return "", ""

    def _resolve_rr_target(self, object_full: str, object_qn: str, role_qn: str) -> Tuple[str, str]:
        project, config = self._proj_cfg_from_rr_qn(role_qn)
        s = (object_full or "").strip()
        if not s:
            return "MetadataObject", object_qn
        parts = s.split(".")
        head = parts[0] if parts else ""
        try:
            base_heads = {
                "Catalog": "MetadataObject",
                "Document": "MetadataObject",
                "InformationRegister": "MetadataObject",
                "AccumulationRegister": "MetadataObject",
                "BusinessProcess": "MetadataObject",
                "Task": "MetadataObject",
                "Enumeration": "MetadataObject",
                "Report": "MetadataObject",
                "DataProcessor": "MetadataObject",
                "ChartOfAccounts": "MetadataObject",
                "ChartOfCharacteristicTypes": "MetadataObject",
            }
            if head in base_heads:
                i = 2
                if i < len(parts) and parts[i] == "TabularSection":
                    if i + 1 < len(parts):
                        tabular = parts[i + 1]
                        i += 2
                        if i < len(parts) and parts[i] == "Attribute" and (i + 1) < len(parts):
                            attr = parts[i + 1]
                            return "Attribute", f"{object_qn}/TabularPart/{tabular}/Attribute/{attr}"
                        return "TabularPart", f"{object_qn}/TabularPart/{tabular}"
                if i < len(parts) and parts[i] == "Attribute" and (i + 1) < len(parts):
                    attr = parts[i + 1]
                    return "Attribute", f"{object_qn}/Attribute/{attr}"
                if head in ("InformationRegister", "AccumulationRegister"):
                    if i < len(parts) and parts[i] == "Dimension" and (i + 1) < len(parts):
                        dim = parts[i + 1]
                        return "Dimension", f"{object_qn}/Dimension/{dim}"
                    if i < len(parts) and parts[i] == "Resource" and (i + 1) < len(parts):
                        res = parts[i + 1]
                        return "Resource", f"{object_qn}/Resource/{res}"
                if i < len(parts) and parts[i] == "Command" and (i + 1) < len(parts):
                    cmd = parts[i + 1]
                    return "Command", f"{object_qn}/Command/{cmd}"
                if i < len(parts) and parts[i] == "Form" and (i + 1) < len(parts):
                    form = parts[i + 1]
                    return "Form", f"{object_qn}/Form/{form}"
                return "MetadataObject", object_qn
            if head == "CommonForm" and len(parts) >= 2:
                form_name = parts[1]
                return "MetadataObject", f"{project}/{config}/ОбщиеФормы/{form_name}"
            if head == "Configuration":
                return "Configuration", f"{project}/{config}"
            return "MetadataObject", object_qn
        except Exception:
            return "MetadataObject", object_qn

    # --- Extension Role Rights loader ---

    def load_role_rights_targets_ext(
        self,
        rows: List[Dict[str, Any]],
        ext_config_name: str,
        base_config_name: str,
    ) -> None:
        """
        Load Role rights for an extension with dual-config target resolution:
        - Adopted objects resolve via ADOPTED_FROM to the base node.
        - Own extension objects resolve to the ext node.
        - Objects absent from extension metadata fall back to base by QN substitution.
        - Configuration rights always target the base Configuration node.
        """
        if not rows:
            logger.info("No extension Role rights rows to load")
            return

        role_flags: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            rq = r.get("role_qn") or ""
            if not rq:
                continue
            if rq not in role_flags:
                role_flags[rq] = {
                    "setForNewObjects": bool(r.get("setForNewObjects", False)),
                    "setForAttributesByDefault": bool(r.get("setForAttributesByDefault", False)),
                    "independentRightsOfChildObjects": bool(r.get("independentRightsOfChildObjects", True)),
                }

        if role_flags:
            role_props_rows = [{"role_qn": rq, **flags} for rq, flags in role_flags.items()]
            with self.driver.session(database=settings.neo4j_database) as session:
                def _tx_flags(tx, payload):
                    tx.run(CYPHER_ROLE_RIGHTS_UPDATE_ROLE_FLAGS, rows=payload)
                self._write(session, _tx_flags, role_props_rows)

        def _ext_qn_to_base(qn: str) -> str:
            if qn == f"{project}/{ext_config_name}":
                return f"{project}/{base_config_name}"
            prefix = f"{project}/{ext_config_name}/"
            if qn.startswith(prefix):
                return f"{project}/{base_config_name}/" + qn[len(prefix):]
            return qn

        project = self._proj_cfg_from_rr_qn(next(iter(role_flags), ""))[0] if role_flags else ""

        groups: Dict[tuple, Dict[str, Any]] = {}
        for row in rows:
            try:
                role_qn = row.get("role_qn") or ""
                object_qn = row.get("object_qn") or ""
                object_full = row.get("object_full") or ""
                right_en = row.get("right_en") or ""
                right_ru = row.get("right_ru") or right_en
                allowed = bool(row.get("allowed"))
                condition_raw = row.get("condition")
                condition = condition_raw.strip() if isinstance(condition_raw, str) else ""
                target_label, target_qn = self._resolve_rr_target(object_full, object_qn, role_qn)

                indep = role_flags.get(role_qn, {}).get("independentRightsOfChildObjects", True)
                if not indep and target_label in {"Attribute", "TabularPart", "Resource", "Dimension", "Command", "Form"}:
                    continue

                target_qn_base = _ext_qn_to_base(target_qn)
                key = (role_qn, target_label, target_qn)
                g = groups.get(key)
                if g is None:
                    g = {
                        "role_qn": role_qn,
                        "target_label": target_label,
                        "target_qn": target_qn,
                        "target_qn_base": target_qn_base,
                        "props": {
                            "rights_present_en": set(),
                            "target_kind": target_label,
                            "target_qn": target_qn,
                            "object_full_all": set(),
                        }
                    }
                    groups[key] = g
                props = g["props"]
                props["rights_present_en"].add(right_en)
                if object_full:
                    props["object_full_all"].add(object_full)
                k_allowed = f"{right_en}_allowed"
                k_ru = f"{right_en}_ru"
                k_has = f"{right_en}_has_condition"
                k_cond = f"{right_en}_condition"
                if k_allowed in props:
                    if props[k_allowed] is True and allowed is False:
                        props[k_allowed] = False
                else:
                    props[k_allowed] = allowed
                if k_ru not in props:
                    props[k_ru] = right_ru
                current_has = bool(props.get(k_has, False))
                props[k_has] = current_has or bool(condition)
                if condition and (k_cond not in props or not props[k_cond]):
                    props[k_cond] = condition
            except Exception as e:
                logger.warning("Skip invalid ext rights row: %s", e)

        if not groups:
            logger.info("No resolvable extension Role rights groups to load")
            return

        buckets: Dict[str, List[Dict[str, Any]]] = {}
        for (_, target_label, _), g in groups.items():
            props = g["props"]
            try:
                if isinstance(props.get("rights_present_en"), set):
                    props["rights_present_en"] = sorted(list(props["rights_present_en"]))
                if isinstance(props.get("object_full_all"), set):
                    props["object_full_all"] = sorted(list(props["object_full_all"]))
            except Exception:
                pass
            buckets.setdefault(target_label, []).append({
                "role_qn": g["role_qn"],
                "target_qn": g["target_qn"],
                "target_qn_base": g["target_qn_base"],
                "props": props,
            })

        bs = settings.neo4j_batch_size
        with self.driver.session(database=settings.neo4j_database) as session:
            def _tx_ext(tx, label: str, rows_chunk: List[Dict[str, Any]]):
                if not rows_chunk:
                    return
                tx.run(cypher_role_rights_grants_access_to_ext(label), rows=rows_chunk)

            for label, payload in buckets.items():
                total = len(payload)
                chunks_total = (total + bs - 1) // bs if total else 0
                logger.info("Ext Role rights targets [%s]: rows=%d, chunks=%d", label, total, chunks_total)
                for chunk in self._chunked(payload, bs):
                    self._write(session, _tx_ext, label, chunk)

        logger.info("Loaded ext Role rights GRANTS_ACCESS_TO targets: %d", sum(len(v) for v in buckets.values()))