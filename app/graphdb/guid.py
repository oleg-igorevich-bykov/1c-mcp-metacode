"""
GuidEnrichmentMixin: utilities to inject meta_uuid into prepared rows using
prebuilt XCF name -> GUID mapping from ConfigDumpInfo.xml.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, List
import logging

from xcf_utils import (
    xcf_name_object,
    xcf_name_attribute,
    xcf_name_tabular_part,
    xcf_name_tabular_attribute,
    xcf_name_resource,
    xcf_name_dimension,
    xcf_name_form,
)

logger = logging.getLogger(__name__)


@dataclass
class GuidStateRow:
    """Pure identity row для sidecar guid_state.

    Не содержит GUID — это identity-only registry GUID-eligible nodes.
    Incremental layer заполняет current_guid отдельно по xcf_name через
    текущий ConfigDumpInfo.xml map.
    """
    label: str
    qualified_name: str
    xcf_name: str


class GuidEnrichmentMixin:
    """Provide GUID map injection and helpers to set meta_uuid on row properties."""

    def set_guid_map(self, mapping: Optional[Dict[str, str]]):
        """Inject prebuilt XCF name -> GUID map. Pass {} or None to disable."""
        try:
            self._guid_map = dict(mapping or {})
        except Exception:
            # Be resilient to non-dict inputs
            self._guid_map = {}

    def _guid_lookup(self, xcf_name: Optional[str]) -> Optional[str]:
        m = getattr(self, "_guid_map", None)
        if not m or not xcf_name:
            return None
        return m.get(xcf_name)

    # XCF helpers: compute name and lookup GUID
    def _guid_for_object(self, category_ru: str, object_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_object(category_ru, object_name))

    def _guid_for_attribute(self, category_ru: str, object_name: str, attr_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_attribute(category_ru, object_name, attr_name))

    def _guid_for_tabular(self, category_ru: str, object_name: str, tabular_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_tabular_part(category_ru, object_name, tabular_name))

    def _guid_for_tabular_attribute(self, category_ru: str, object_name: str, tabular_name: str, attr_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_tabular_attribute(category_ru, object_name, tabular_name, attr_name))

    def _guid_for_resource(self, category_ru: str, object_name: str, res_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_resource(category_ru, object_name, res_name))

    def _guid_for_dimension(self, category_ru: str, object_name: str, dim_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_dimension(category_ru, object_name, dim_name))

    def _guid_for_form(self, category_ru: str, object_name: str, form_name: str) -> Optional[str]:
        return self._guid_lookup(xcf_name_form(category_ru, object_name, form_name))

    def _enrich_guids(
        self,
        objects: List[Dict[str, Any]],
        tabulars: List[Dict[str, Any]],
        obj_attrs: List[Dict[str, Any]],
        tab_attrs: List[Dict[str, Any]],
        resources: List[Dict[str, Any]],
        dimensions: List[Dict[str, Any]],
        forms: List[Dict[str, Any]],
    ) -> None:
        """
        Add meta_uuid to row.properties where GUID is available.
        Safe no-op if no mapping set.

        Replaces row["properties"] with a shallow copy before writing meta_uuid,
        so that parsed model (form.properties / cmd.properties / a.to_dict() / ...)
        is never mutated. Otherwise compute_object_hash() picks meta_uuid into
        baseline and the next incremental run sees phantom changed objects.
        """
        if not getattr(self, "_guid_map", None):
            return

        def _set_meta(row: Dict[str, Any], guid: Optional[str]) -> None:
            if not guid:
                return
            props = row.get("properties")
            if isinstance(props, dict):
                if "meta_uuid" in props:
                    return
                new_props = dict(props)
            else:
                new_props = {}
            new_props["meta_uuid"] = guid
            row["properties"] = new_props

        # MetadataObject
        for row in objects or []:
            guid = self._guid_for_object(row.get("category_name",""), row.get("obj_name",""))
            _set_meta(row, guid)

        # TabularPart
        for row in tabulars or []:
            guid = self._guid_for_tabular(row.get("category_name",""), row.get("obj_name",""), row.get("tabular_name",""))
            _set_meta(row, guid)

        # Attribute (object-level)
        for row in obj_attrs or []:
            guid = self._guid_for_attribute(row.get("category_name",""), row.get("object_name",""), row.get("attr_name",""))
            _set_meta(row, guid)

        # Attribute (tabular part attribute)
        for row in tab_attrs or []:
            guid = self._guid_for_tabular_attribute(row.get("category_name",""), row.get("object_name",""), row.get("tabular_name",""), row.get("attr_name",""))
            _set_meta(row, guid)

        # Resource
        for row in resources or []:
            guid = self._guid_for_resource(row.get("category_name",""), row.get("object_name",""), row.get("res_name",""))
            _set_meta(row, guid)

        # Dimension
        for row in dimensions or []:
            guid = self._guid_for_dimension(row.get("category_name",""), row.get("object_name",""), row.get("dim_name",""))
            _set_meta(row, guid)

        # Form (from metadata .txt, not Ext/Form.xml)
        for row in forms or []:
            guid = self._guid_for_form(row.get("category_name",""), row.get("object_name",""), row.get("form_name",""))
            _set_meta(row, guid)


def collect_guid_state_rows(
    objects: List[Dict[str, Any]],
    tabulars: List[Dict[str, Any]],
    obj_attrs: List[Dict[str, Any]],
    tab_attrs: List[Dict[str, Any]],
    resources: List[Dict[str, Any]],
    dimensions: List[Dict[str, Any]],
    forms: List[Dict[str, Any]],
) -> List[GuidStateRow]:
    """Собрать identity-only rows для guid_state по тем же rows, что _enrich_guids,
    через forward XCF helpers. Не зависит от guid_map: возвращает rows для всех
    GUID-eligible nodes, у которых вычислим xcf_name.

    Identity row-а: (label, qualified_name). xcf_name — атрибут для diff.
    """
    out: List[GuidStateRow] = []

    def _emit(label: str, qn_field: str, row: Dict[str, Any], xcf_name: Optional[str]) -> None:
        if not xcf_name:
            return
        qn = row.get(qn_field)
        if not qn:
            return
        out.append(GuidStateRow(label=label, qualified_name=qn, xcf_name=xcf_name))

    for row in objects or []:
        _emit("MetadataObject", "obj_qn", row, xcf_name_object(
            row.get("category_name", ""), row.get("obj_name", "")
        ))
    for row in tabulars or []:
        _emit("TabularPart", "tab_qn", row, xcf_name_tabular_part(
            row.get("category_name", ""), row.get("obj_name", ""),
            row.get("tabular_name", ""),
        ))
    for row in obj_attrs or []:
        _emit("Attribute", "attr_qn", row, xcf_name_attribute(
            row.get("category_name", ""), row.get("object_name", ""),
            row.get("attr_name", ""),
        ))
    for row in tab_attrs or []:
        _emit("Attribute", "attr_qn", row, xcf_name_tabular_attribute(
            row.get("category_name", ""), row.get("object_name", ""),
            row.get("tabular_name", ""), row.get("attr_name", ""),
        ))
    for row in resources or []:
        _emit("Resource", "res_qn", row, xcf_name_resource(
            row.get("category_name", ""), row.get("object_name", ""),
            row.get("res_name", ""),
        ))
    for row in dimensions or []:
        _emit("Dimension", "dim_qn", row, xcf_name_dimension(
            row.get("category_name", ""), row.get("object_name", ""),
            row.get("dim_name", ""),
        ))
    for row in forms or []:
        _emit("Form", "form_qn", row, xcf_name_form(
            row.get("category_name", ""), row.get("object_name", ""),
            row.get("form_name", ""),
        ))
    return out