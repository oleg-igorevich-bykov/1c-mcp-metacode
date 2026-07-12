"""
Resolve graph identity for incremental child-level impacts.

Diff-слой (`compute_child_diff`) возвращает domain keys (`group`, `key`) — без
знания о QN-сегментах rows_builder. Этот модуль — единственный источник истины
для маппинга `(group, key) → (label, exact_qn, prefix_qn?)`. Любое расхождение
с `app/graphdb/rows_builder.py` (фактические сегменты QN) — баг здесь.

Маппинг сегментов проверен против rows_builder.py:120-398.
Важные особенности (label != qn-segment):
- account_flags → label=AccountingFlag, segment=AccountFlag
- subconto_flags → label=DimensionAccountingFlag, segment=SubcontoFlag
- url_methods → label=UrlMethod, segment=Method
- journal_graphs → label=JournalGraph, segment=Graph
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .hashing import ChildDomainImpact


@dataclass(frozen=True, slots=True)
class ChildGraphImpact:
    """Graph identity одного child impact.

    `exact_qn` всегда заполнен (точный QN узла).
    `prefix_qn` выставляется только для parent-узлов с subtree (TabularPart-changed,
    UrlTemplate-changed) — это сигнал caller-у, что нужен subtree-cleanup и
    subtree-refresh ADOPTED_FROM (включая сам parent через ветку `qn = p` в Cypher).
    """

    label: str
    exact_qn: str
    prefix_qn: Optional[str] = None
    action: str = "changed"


# (group, label, qn_segment) — single source of truth.
# Composite-key группы (tabular_part_attributes, url_methods) обрабатываются
# отдельно из-за nested QN.
_SIMPLE_MAPPING = {
    "attributes": ("Attribute", "Attribute"),
    "resources": ("Resource", "Resource"),
    "dimensions": ("Dimension", "Dimension"),
    "forms": ("Form", "Form"),
    "commands": ("Command", "Command"),
    "layouts": ("Layout", "Layout"),
    "enum_values": ("EnumValue", "EnumValue"),
    "account_flags": ("AccountingFlag", "AccountFlag"),
    "subconto_flags": ("DimensionAccountingFlag", "SubcontoFlag"),
    "characteristic_schemes": ("Characteristic", "Characteristic"),
    "journal_graphs": ("JournalGraph", "Graph"),
}


def resolve_child_graph_identity(
    object_qn: str,
    impact: ChildDomainImpact,
) -> Optional[ChildGraphImpact]:
    """Map domain impact to graph identity.

    Returns None для групп, не порождающих graph-узлов (например `movements` —
    relationship, не node).
    """
    group = impact.group
    key = impact.key
    action = impact.action

    if group in _SIMPLE_MAPPING:
        label, segment = _SIMPLE_MAPPING[group]
        if not key:
            return None
        name = key[0]
        # CharacteristicScheme без `Индекс` строится в rows_builder как
        # `{obj}/Characteristic` (без суффикса) — см. [rows_builder.py:181].
        # hashing.py использует fallback-ключ `#pos{i}` для diff identity, но
        # graph QN для таких схем не имеет сегмента после Characteristic.
        if group == "characteristic_schemes" and name.startswith("#pos"):
            exact_qn = f"{object_qn}/{segment}"
        else:
            exact_qn = f"{object_qn}/{segment}/{name}"
        return ChildGraphImpact(
            label=label,
            exact_qn=exact_qn,
            prefix_qn=None,
            action=action,
        )

    if group == "tabular_parts":
        if not key:
            return None
        name = key[0]
        exact_qn = f"{object_qn}/TabularPart/{name}"
        # Changed TabularPart: subtree (parent + nested attributes) — нужен prefix.
        prefix_qn = exact_qn if action == "changed" else None
        return ChildGraphImpact(
            label="TabularPart",
            exact_qn=exact_qn,
            prefix_qn=prefix_qn,
            action=action,
        )

    if group == "tabular_part_attributes":
        # key = (tabular_part_name, attribute_name)
        if len(key) != 2:
            return None
        tp_name, attr_name = key
        return ChildGraphImpact(
            label="Attribute",
            exact_qn=f"{object_qn}/TabularPart/{tp_name}/Attribute/{attr_name}",
            prefix_qn=None,
            action=action,
        )

    if group == "url_templates":
        if not key:
            return None
        name = key[0]
        exact_qn = f"{object_qn}/UrlTemplate/{name}"
        # Changed UrlTemplate: subtree (parent + nested methods).
        prefix_qn = exact_qn if action == "changed" else None
        return ChildGraphImpact(
            label="UrlTemplate",
            exact_qn=exact_qn,
            prefix_qn=prefix_qn,
            action=action,
        )

    if group == "url_methods":
        # key = (template_name, method_name); сегмент `Method`, label `UrlMethod`.
        if len(key) != 2:
            return None
        tpl_name, method_name = key
        return ChildGraphImpact(
            label="UrlMethod",
            exact_qn=f"{object_qn}/UrlTemplate/{tpl_name}/Method/{method_name}",
            prefix_qn=None,
            action=action,
        )

    if group == "movements":
        # Movements — relationship, не порождает graph-узлов.
        return None

    return None
