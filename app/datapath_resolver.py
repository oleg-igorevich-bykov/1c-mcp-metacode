from __future__ import annotations

from typing import Dict, List, Any, Tuple, Optional, Set
import logging

logger = logging.getLogger(__name__)


def resolve_datapath_bindings(config, rows: Dict[str, List[Dict[str, Any]]], project_name: str) -> None:
    """
    Resolve ПутьКДанным (DataPath) for each FormControl into a target node and
    emit only (FormControl)-[:BINDS_TO]->(Target) edges in rows['data_bindings'].

    Target labels used: Attribute, Dimension, Resource, FormAttribute, MetadataObject.
    No new node types or relationship types are introduced.

    This function is pure in the sense it only appends 'data_bindings' into rows
    and does not require any database access. All lookups are O(1) via in-memory maps.
    """

    cfg_name = getattr(config, "name", None) or ""

    controls: List[Dict[str, Any]] = rows.get("controls") or []
    form_attrs_rows: List[Dict[str, Any]] = rows.get("form_attributes") or []

    # Build metadata indices for O(1) resolution
    obj_attrs: Dict[Tuple[str, str], str] = {}              # (obj_qn, attr_name) -> attr_qn
    tab_attrs: Dict[Tuple[str, str, str], str] = {}         # (obj_qn, tabular_name, attr_name) -> attr_qn
    reg_dims: Dict[Tuple[str, str, str], str] = {}          # (cfg_name, reg_name, dim_name) -> dim_qn
    reg_res: Dict[Tuple[str, str, str], str] = {}           # (cfg_name, reg_name, res_name) -> res_qn

    for cat in getattr(config, "categories", []) or []:
        cat_name = getattr(cat, "name", "")
        for obj in getattr(cat, "metadata_objects", []) or []:
            obj_name = getattr(obj, "name", "")
            if not (cat_name and obj_name):
                continue
            obj_qn = f"{project_name}/{cfg_name}/{cat_name}/{obj_name}"

            # Object-level attributes
            for a in getattr(obj, "attributes", []) or []:
                an = getattr(a, "name", "")
                if an:
                    obj_attrs[(obj_qn, an)] = f"{obj_qn}/Attribute/{an}"

            # Tabular part attributes
            for t in getattr(obj, "tabular_parts", []) or []:
                tn = getattr(t, "name", "")
                if not tn:
                    continue
                for a in getattr(t, "attributes", []) or []:
                    an = getattr(a, "name", "")
                    if an:
                        tab_attrs[(obj_qn, tn, an)] = f"{obj_qn}/TabularPart/{tn}/Attribute/{an}"

            # Registers: dimensions and resources
            if cat_name in ("РегистрыСведений", "РегистрыНакопления"):
                for d in getattr(obj, "dimensions", []) or []:
                    dn = getattr(d, "name", "")
                    if dn:
                        reg_dims[(cfg_name, obj_name, dn)] = f"{obj_qn}/Dimension/{dn}"
                for r in getattr(obj, "resources", []) or []:
                    rn = getattr(r, "name", "")
                    if rn:
                        reg_res[(cfg_name, obj_name, rn)] = f"{obj_qn}/Resource/{rn}"

    # Form attribute index: (form_qn, name) -> {qn, type}
    fa_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for fa in form_attrs_rows:
        form_qn = fa.get("form_qn") or ""
        nm = fa.get("name") or ""
        if not (form_qn and nm):
            continue
        props = fa.get("properties") or {}
        fa_index[(form_qn, nm)] = {
            "qn": fa.get("qn") or f"{form_qn}/FormAttribute/{nm}",
            "type": props.get("Тип") or props.get("Type") or props.get("type") or "",
        }

    def _form_and_object_qn_from_control_qn(ctrl_qn: str) -> Tuple[str, str]:
        """
        Control QN looks like:
          <obj_qn>/Form/<form_name>/Control/...
        Return (form_qn, obj_qn)
        """
        if not ctrl_qn:
            return "", ""
        parts = ctrl_qn.split("/Control/", 1)
        form_qn = parts[0] if parts else ""
        obj_qn = form_qn.rsplit("/Form/", 1)[0] if "/Form/" in form_qn else ""
        return form_qn, obj_qn

    # Heuristics for "Список.*" without creating new nodes
    SAFE_LIST_TO_OBJECT = {"Ссылка"}
    COMMON_OBJECT_ATTR = {"Код", "Наименование", "Номер", "Дата", "ПометкаУдаления", "Проведен"}

    data_bindings: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()

    for ctrl in controls:
        try:
            ctrl_qn = ctrl.get("qn") or ""
            props = ctrl.get("properties") or {}
            dp = props.get("ПутьКДанным")
            if not dp:
                continue
            raw = props.get("ПутьКДанным_RAW") or dp
            form_qn, obj_qn = _form_and_object_qn_from_control_qn(ctrl_qn)
            if not obj_qn:
                continue

            tokens = [t for t in str(dp).split(".") if t]
            if not tokens:
                continue

            target_qn: Optional[str] = None
            target_label: Optional[str] = None
            resolution: Optional[str] = None

            if tokens[0] == "Объект":
                if len(tokens) == 1:
                    target_qn = obj_qn
                    target_label = "MetadataObject"
                    resolution = "MetadataObject"
                elif len(tokens) == 2:
                    attr = tokens[1]
                    tq = obj_attrs.get((obj_qn, attr))
                    if tq:
                        target_qn = tq
                        target_label = "Attribute"
                        resolution = "Attribute"
                elif len(tokens) >= 3:
                    tp, col = tokens[1], tokens[2]
                    tq = tab_attrs.get((obj_qn, tp, col))
                    if tq:
                        target_qn = tq
                        target_label = "Attribute"  # tabular attr nodes are labeled Attribute in the model
                        resolution = "TabularPartAttribute"

            else:
                # Try FormAttribute.<Field> resolution first
                fa_meta = fa_index.get((form_qn, tokens[0]))
                if fa_meta:
                    fa_qn = fa_meta.get("qn")
                    fa_type = str(fa_meta.get("type") or "")
                    if len(tokens) == 1:
                        target_qn = fa_qn
                        target_label = "FormAttribute"
                        resolution = "FormAttribute"
                    else:
                        # If it's a record set of an information register, map field to Dimension/Resource/Attribute
                        if fa_type.startswith("РегистрСведенийНаборЗаписей."):
                            reg_name = fa_type.split(".", 1)[1]
                            field = tokens[1]
                            # Prefer exact Dim/Res; fallback to register requisite as Attribute
                            tq = reg_dims.get((cfg_name, reg_name, field))
                            if tq:
                                target_qn = tq
                                target_label = "Dimension"
                                resolution = "Dimension"
                            else:
                                tq = reg_res.get((cfg_name, reg_name, field))
                                if tq:
                                    target_qn = tq
                                    target_label = "Resource"
                                    resolution = "Resource"
                                else:
                                    # Try register requisite (attributes map includes registers too)
                                    reg_obj_qn = f"{project_name}/{cfg_name}/РегистрыСведений/{reg_name}"
                                    tq = obj_attrs.get((reg_obj_qn, field))
                                    if tq:
                                        target_qn = tq
                                        target_label = "Attribute"
                                        resolution = "RegisterAttribute"
                        else:
                            # Non-register types: bind to the FormAttribute itself (coarse link)
                            target_qn = fa_qn
                            target_label = "FormAttribute"
                            resolution = "FormAttribute"
                elif tokens[0] == "Список":
                    # Map safely without creating extra nodes
                    if len(tokens) == 1 or tokens[1] in SAFE_LIST_TO_OBJECT:
                        target_qn = obj_qn
                        target_label = "MetadataObject"
                        resolution = "MetadataObject"
                    else:
                        field = tokens[1]
                        tq = obj_attrs.get((obj_qn, field))
                        if tq:
                            target_qn = tq
                            target_label = "Attribute"
                            resolution = "Attribute"
                        elif field in COMMON_OBJECT_ATTR:
                            tq = obj_attrs.get((obj_qn, field))
                            if tq:
                                target_qn = tq
                                target_label = "Attribute"
                                resolution = "Attribute"

            if target_qn and target_label:
                dedup_key = (ctrl_qn, target_qn, "DataPath")
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                data_bindings.append({
                    "container_qn": ctrl_qn,
                    "target_qn": target_qn,
                    "target_label": target_label,
                    "via": "DataPath",
                    "raw": raw,
                    "resolved": True,
                    "resolution": resolution or target_label,
                })
        except Exception as e:
            # Continue on individual control failures
            logger.debug("Skip binding for control due to error: %s", e)

    if data_bindings:
        rows["data_bindings"] = data_bindings