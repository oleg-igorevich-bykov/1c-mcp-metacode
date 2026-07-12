"""
Detrministic SHA-256 hashes для incremental diff + child-level diff.

Хешируем только структуру из источника (metadata loader emissions). НЕ включаем:
- meta_uuid (записывается dumpinfo_loader, не metadata)
- USED_IN edges (derived, не источник)
- любые *_embedding (записываются vector_indexer)
- internal/служебные поля loader-а

Из плана (Architectural decision 6 + hashing.py module spec):
  In object_hash включать только:
    name, properties, attributes (with their properties), tabular_parts (with their attributes),
    resources, dimensions, forms (names + their properties), commands (names + their properties),
    layouts (names + their properties), enum_values, characteristic_schemes,
    account_flags, subconto_flags.

`INTERNAL_HASH_KEYS` фильтрует служебные ключи на всех уровнях вложенности properties
как defense-in-depth: даже если parsed model случайно получит meta_uuid / *_embedding,
hash останется стабильным.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


INTERNAL_HASH_KEYS = frozenset({
    "meta_uuid",
    "description_embedding",
    "description_embedding_model",
    "description_embedding_updated_at",
})


def _stable_json(obj: Any) -> str:
    """Detrministic JSON dump: sort_keys + ensure_ascii=False, separators tight."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _normalize_properties(value: Any) -> Any:
    """Recursively drop INTERNAL_HASH_KEYS from dicts at any depth.

    Preserves list order (significant for 1C metadata: например порядок ШаблоныURL).
    """
    if isinstance(value, dict):
        return {
            k: _normalize_properties(v)
            for k, v in value.items()
            if k not in INTERNAL_HASH_KEYS
        }
    if isinstance(value, list):
        return [_normalize_properties(x) for x in value]
    return value


def _attribute_to_dict(attr: Any) -> Dict[str, Any]:
    return {
        "name": getattr(attr, "name", ""),
        "properties": _normalize_properties(dict(getattr(attr, "properties", {}) or {})),
    }


def _tabular_part_to_dict(tp: Any) -> Dict[str, Any]:
    return {
        "name": getattr(tp, "name", ""),
        "properties": _normalize_properties(dict(getattr(tp, "properties", {}) or {})),
        "attributes": [_attribute_to_dict(a) for a in getattr(tp, "attributes", []) or []],
    }


def _named_with_properties(item: Any) -> Dict[str, Any]:
    return {
        "name": getattr(item, "name", ""),
        "properties": _normalize_properties(dict(getattr(item, "properties", {}) or {})),
    }


def _build_object_snapshot(metadata_object: Any) -> Dict[str, Any]:
    """Сборка детерминированного snapshot объекта для hashing и child-diff.

    Принимает MetadataObject (см. app/parsers/metadata_parser.py).
    Все properties на всех уровнях нормализованы через _normalize_properties.
    """
    obj = metadata_object
    return {
        "name": getattr(obj, "name", ""),
        "properties": _normalize_properties(dict(getattr(obj, "properties", {}) or {})),
        "attributes": [_attribute_to_dict(a) for a in getattr(obj, "attributes", []) or []],
        "tabular_parts": [_tabular_part_to_dict(tp) for tp in getattr(obj, "tabular_parts", []) or []],
        "resources": [_attribute_to_dict(r) for r in getattr(obj, "resources", []) or []],
        "dimensions": [_attribute_to_dict(d) for d in getattr(obj, "dimensions", []) or []],
        "forms": [_named_with_properties(f) for f in getattr(obj, "forms", []) or []],
        "commands": [_named_with_properties(c) for c in getattr(obj, "commands", []) or []],
        "layouts": [_named_with_properties(l) for l in getattr(obj, "layouts", []) or []],
        "enum_values": [_named_with_properties(e) for e in getattr(obj, "enum_values", []) or []],
        "account_flags": [_attribute_to_dict(a) for a in getattr(obj, "account_flags", []) or []],
        "subconto_flags": [_attribute_to_dict(a) for a in getattr(obj, "subconto_flags", []) or []],
        "characteristic_schemes": _normalize_properties(
            list(getattr(obj, "characteristic_schemes", []) or [])
        ),
    }


def build_object_snapshot(metadata_object: Any) -> Dict[str, Any]:
    """Public alias for `_build_object_snapshot`.

    Call sites which persist the snapshot for child-diff (`_init_incremental_baseline`,
    `metadata_sync` после bulk apply) используют этот alias. Гарантия: hash и snapshot
    всегда синхронны — оба построены из одной функции.
    """
    return _build_object_snapshot(metadata_object)


def compute_object_hash(metadata_object: Any) -> str:
    snapshot = _build_object_snapshot(metadata_object)
    payload = _stable_json(snapshot).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_object_hash_from_snapshot(snapshot: Dict[str, Any]) -> str:
    """Hash от уже построенного snapshot. Совпадает с compute_object_hash(obj),
    если snapshot был получен через build_object_snapshot(obj).
    """
    payload = _stable_json(snapshot).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_configuration_hash(configuration: Any) -> str:
    """Hash top-level Configuration.properties (без объектов внутри).

    Объекты конфигурации диффятся отдельно через `compute_object_hash`.
    """
    props = _normalize_properties(dict(getattr(configuration, "properties", {}) or {}))
    payload = _stable_json({"properties": props}).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_file_hash(file_bytes: bytes) -> str:
    """Hash источника-файла (.txt / .xml) для source_manifest.content_hash."""
    return hashlib.sha256(file_bytes).hexdigest()


# ---------------------------------------------------------------------------
# Child-level diff
# ---------------------------------------------------------------------------


@dataclass
class ChildDiffStats:
    """Counters for child-level diff.

    `counts[group][action]` где action ∈ {"added","changed","deleted"}.
    Группы добавляются динамически — отсутствие группы означает «нулевые counters».
    """

    counts: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def add(self, group: str, action: str, n: int = 1) -> None:
        if n <= 0:
            return
        bucket = self.counts.setdefault(
            group, {"added": 0, "changed": 0, "deleted": 0}
        )
        bucket[action] = bucket.get(action, 0) + n

    def merge(self, other: "ChildDiffStats") -> None:
        for group, bucket in other.counts.items():
            for action, n in bucket.items():
                self.add(group, action, n)

    def is_empty(self) -> bool:
        return not any(any(v for v in b.values()) for b in self.counts.values())

    def nonzero_groups(self) -> List[Tuple[str, Dict[str, int]]]:
        """Sorted (group_name, {added,changed,deleted}) pairs with at least one non-zero counter."""
        return sorted(
            (g, b) for g, b in self.counts.items() if any(v for v in b.values())
        )


@dataclass(frozen=True, slots=True)
class ChildDomainImpact:
    """Domain-level impact одного child entity.

    `key` — composite tuple для уникальной идентификации внутри группы:
    - simple groups (attributes, resources, dimensions, forms, ...): `(name,)`
    - tabular_part_attributes: `(tabular_part_name, attribute_name)`
    - url_methods: `(template_name, method_name)`
    - characteristic_schemes: `(index_or_pos_key,)`

    Graph identity (label, QN) резолвится отдельно через `resolve_child_graph_identity`
    в [app/incremental/child_identity.py](app/incremental/child_identity.py) — это
    защищает diff-слой от знания о QN-сегментах rows_builder.
    """

    group: str
    key: Tuple[str, ...]
    action: str  # "added" | "changed" | "deleted"


def _extract_simple_named(items: Optional[Sequence[Any]]) -> Dict[str, str]:
    """Build {name: serialized_json} map from snapshot list of named items."""
    result: Dict[str, str] = {}
    for item in items or []:
        if isinstance(item, Mapping):
            name = item.get("name")
        else:
            name = getattr(item, "name", None)
        if name is None:
            continue
        result[str(name)] = _stable_json(item)
    return result


def _extract_tabular_parts_without_attrs(items: Optional[Sequence[Any]]) -> Dict[str, str]:
    """Tabular parts diff по `name`+`properties`, без вложенных `attributes`.

    Graph identity: `RowsBuilderMixin` строит TabularPart row только из `tab.properties`
    ([rows_builder.py:207-219]). Cypher `CYPHER_UPSERT_TABULAR_PART` отдельный от
    `CYPHER_UPSERT_TABULAR_ATTRIBUTE`. Изменение реквизита НЕ меняет узел `:TabularPart`,
    поэтому `tabular_parts.changed` не должен срабатывать. Изменения реквизитов
    считаются отдельно в `tabular_part_attributes`.

    Симметрично `_extract_url_templates` (там исключаются `Методы` чтобы изменение
    метода не дублировалось как url_templates.changed).
    """
    result: Dict[str, str] = {}
    for item in items or []:
        if isinstance(item, Mapping):
            name = item.get("name")
            view = {k: v for k, v in item.items() if k != "attributes"}
        else:
            name = getattr(item, "name", None)
            view = {
                "name": name,
                "properties": dict(getattr(item, "properties", {}) or {}),
            }
        if name is None:
            continue
        result[str(name)] = _stable_json(view)
    return result


def _extract_tabular_attrs(parts: Optional[Sequence[Any]]) -> Dict[Tuple[str, str], str]:
    """Composite key (tabular_part.name, attribute.name) across all tabular parts."""
    result: Dict[Tuple[str, str], str] = {}
    for tp in parts or []:
        if isinstance(tp, Mapping):
            tp_name = tp.get("name")
            attrs = tp.get("attributes") or []
        else:
            tp_name = getattr(tp, "name", None)
            attrs = getattr(tp, "attributes", []) or []
        if tp_name is None:
            continue
        for attr in attrs:
            if isinstance(attr, Mapping):
                attr_name = attr.get("name")
            else:
                attr_name = getattr(attr, "name", None)
            if attr_name is None:
                continue
            result[(str(tp_name), str(attr_name))] = _stable_json(attr)
    return result


def _extract_characteristic_schemes(items: Optional[Sequence[Any]]) -> Dict[str, str]:
    """Key by `str(scheme["Индекс"])`, matching graph identity .../Characteristic/{Индекс}.

    Fallback to positional key `#pos{i}` if `Индекс` отсутствует (XML edge case).
    """
    result: Dict[str, str] = {}
    for i, item in enumerate(items or []):
        if isinstance(item, Mapping):
            key = str(item.get("Индекс", "")) or f"#pos{i}"
        else:
            key = f"#pos{i}"
        result[key] = _stable_json(item)
    return result


def _extract_url_templates(obj_properties: Mapping[str, Any]) -> Dict[str, str]:
    """{template_name: serialized template_without_methods}.

    Methods diff отдельно через `_extract_url_methods`, чтобы изменение в одном методе
    не дублировалось как url_templates.changed=1 + url_methods.changed=1.
    """
    result: Dict[str, str] = {}
    templates = obj_properties.get("ШаблоныURL") if obj_properties else None
    if not isinstance(templates, list):
        return result
    for t in templates:
        if not isinstance(t, Mapping):
            continue
        name = t.get("Имя")
        if not name:
            continue
        snapshot_view = {k: v for k, v in t.items() if k != "Методы"}
        result[str(name)] = _stable_json(snapshot_view)
    return result


def _extract_url_methods(obj_properties: Mapping[str, Any]) -> Dict[Tuple[str, str], str]:
    """Composite key (template_name, method_name) aggregated across all templates."""
    result: Dict[Tuple[str, str], str] = {}
    templates = obj_properties.get("ШаблоныURL") if obj_properties else None
    if not isinstance(templates, list):
        return result
    for t in templates:
        if not isinstance(t, Mapping):
            continue
        t_name = t.get("Имя")
        if not t_name:
            continue
        methods = t.get("Методы") or []
        if not isinstance(methods, list):
            continue
        for m in methods:
            if not isinstance(m, Mapping):
                continue
            m_name = m.get("Имя")
            if not m_name:
                continue
            result[(str(t_name), str(m_name))] = _stable_json(m)
    return result


def _extract_journal_graphs(obj_properties: Mapping[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    graphs = obj_properties.get("ГрафыЖурнала") if obj_properties else None
    if not isinstance(graphs, list):
        return result
    for g in graphs:
        if not isinstance(g, Mapping):
            continue
        name = g.get("Имя") or g.get("name")
        if not name:
            continue
        result[str(name)] = _stable_json(g)
    return result


def _extract_movements(obj_properties: Mapping[str, Any]) -> Dict[Tuple[str, str], str]:
    """Согласовано с app/graphdb/rows_builder.py:304-343.

    Key — (reg_category, reg_name) — соответствует target Register QN.
    Value сериализует тот же payload, что rows_builder кладёт в movement row и
    `CYPHER_UPSERT_DO_MOVEMENTS_IN` пишет в свойства relationship: prefix +
    doc-level `Проведение` / `ЗаписьДвиженийПриПроведении` / `УдалениеДвижений`.
    Иначе при изменении этих свойств target тот же → serialized тот же →
    `movements.changed=0`, тогда как graph реально пересоздаёт relationship с
    новыми значениями (`cleanup_metadata_object_node` сносит DO_MOVEMENTS_IN,
    `load_configurations` пересоздаёт).
    """
    result: Dict[Tuple[str, str], str] = {}
    if not obj_properties:
        return result
    moves = obj_properties.get("Движения")
    if isinstance(moves, str) and moves.strip():
        moves_list: List[str] = [moves.strip()]
    elif isinstance(moves, list):
        moves_list = [str(m).strip() for m in moves if str(m).strip()]
    else:
        return result
    prefix_to_category = {
        "РегистрНакопления": "РегистрыНакопления",
        "РегистрСведений": "РегистрыСведений",
    }
    # Doc-level props, которые попадают в relationship на ВСЕ движения объекта.
    doc_provedenie = obj_properties.get("Проведение")
    doc_record = obj_properties.get("ЗаписьДвиженийПриПроведении")
    doc_delete = obj_properties.get("УдалениеДвижений")
    seen: set = set()
    for mv in moves_list:
        parts = mv.split(".", 1)
        if len(parts) != 2:
            continue
        prefix, reg_name = parts[0].strip(), parts[1].strip()
        if prefix not in prefix_to_category:
            continue
        reg_category = prefix_to_category[prefix]
        key = (reg_category, reg_name)
        if key in seen:
            continue
        seen.add(key)
        payload = {
            "ВидРегистра": prefix,
            "reg_category": reg_category,
            "reg_name": reg_name,
            "Проведение": doc_provedenie,
            "ЗаписьДвиженийПриПроведении": doc_record,
            "УдалениеДвижений": doc_delete,
        }
        result[key] = _stable_json(payload)
    return result


def _diff_maps(prev_map: Mapping[Any, str], new_map: Mapping[Any, str]) -> Tuple[int, int, int]:
    """Returns (added, changed, deleted) counters for two key->serialized maps."""
    prev_keys = set(prev_map.keys())
    new_keys = set(new_map.keys())
    added = len(new_keys - prev_keys)
    deleted = len(prev_keys - new_keys)
    changed = sum(1 for k in prev_keys & new_keys if prev_map[k] != new_map[k])
    return added, changed, deleted


def _diff_maps_detailed(
    prev_map: Mapping[Any, str], new_map: Mapping[Any, str]
) -> Tuple[List[Any], List[Any], List[Any]]:
    """Returns (added_keys, changed_keys, deleted_keys) for two key->serialized maps."""
    prev_keys = set(prev_map.keys())
    new_keys = set(new_map.keys())
    added = sorted(new_keys - prev_keys, key=lambda k: str(k))
    deleted = sorted(prev_keys - new_keys, key=lambda k: str(k))
    changed = sorted(
        (k for k in prev_keys & new_keys if prev_map[k] != new_map[k]),
        key=lambda k: str(k),
    )
    return added, changed, deleted


def _impacts_from_keys(
    group: str,
    added_keys: List[Any],
    changed_keys: List[Any],
    deleted_keys: List[Any],
) -> List[ChildDomainImpact]:
    """Convert per-action key lists into ChildDomainImpact records.

    Простой ключ (str) превращается в `(key,)`; tuple-ключ — в сам tuple.
    """

    def _as_tuple(k: Any) -> Tuple[str, ...]:
        if isinstance(k, tuple):
            return tuple(str(x) for x in k)
        return (str(k),)

    impacts: List[ChildDomainImpact] = []
    for k in added_keys:
        impacts.append(ChildDomainImpact(group, _as_tuple(k), "added"))
    for k in changed_keys:
        impacts.append(ChildDomainImpact(group, _as_tuple(k), "changed"))
    for k in deleted_keys:
        impacts.append(ChildDomainImpact(group, _as_tuple(k), "deleted"))
    return impacts


def compute_child_diff(
    prev_snapshot: Optional[Mapping[str, Any]],
    new_snapshot: Mapping[str, Any],
) -> Tuple[ChildDiffStats, List[ChildDomainImpact]]:
    """Compare two snapshots and return per-group counters plus per-entity impacts.

    `prev_snapshot=None` (legacy state без сохранённого `object_snapshot_json`) -> empty
    stats + empty impacts. Report caller тогда обновляет только object-level counter;
    legacy объект получит child stats на следующем цикле. Для added object impacts
    тоже пустые — субtree-refresh ADOPTED_FROM делается через prefix=object_qn
    (см. план §D, AdoptedFromImpact).
    """
    stats = ChildDiffStats()
    impacts: List[ChildDomainImpact] = []
    if prev_snapshot is None:
        return stats, impacts

    simple_groups = (
        "attributes", "resources", "dimensions",
        "forms", "commands", "layouts", "enum_values",
        "account_flags", "subconto_flags",
    )
    for group in simple_groups:
        prev_map = _extract_simple_named(prev_snapshot.get(group))
        new_map = _extract_simple_named(new_snapshot.get(group))
        a_keys, c_keys, d_keys = _diff_maps_detailed(prev_map, new_map)
        stats.add(group, "added", len(a_keys))
        stats.add(group, "changed", len(c_keys))
        stats.add(group, "deleted", len(d_keys))
        impacts.extend(_impacts_from_keys(group, a_keys, c_keys, d_keys))

    # `tabular_parts` diff БЕЗ вложенных attributes — изменения реквизитов
    # идут отдельным счётчиком tabular_part_attributes и не должны дублироваться
    # как tabular_parts.changed (node :TabularPart в graph не меняется от
    # изменения её реквизитов; см. _extract_tabular_parts_without_attrs).
    prev_tp = _extract_tabular_parts_without_attrs(prev_snapshot.get("tabular_parts"))
    new_tp = _extract_tabular_parts_without_attrs(new_snapshot.get("tabular_parts"))
    a_keys, c_keys, d_keys = _diff_maps_detailed(prev_tp, new_tp)
    stats.add("tabular_parts", "added", len(a_keys))
    stats.add("tabular_parts", "changed", len(c_keys))
    stats.add("tabular_parts", "deleted", len(d_keys))
    impacts.extend(_impacts_from_keys("tabular_parts", a_keys, c_keys, d_keys))

    prev_tpa = _extract_tabular_attrs(prev_snapshot.get("tabular_parts"))
    new_tpa = _extract_tabular_attrs(new_snapshot.get("tabular_parts"))
    a_keys, c_keys, d_keys = _diff_maps_detailed(prev_tpa, new_tpa)
    stats.add("tabular_part_attributes", "added", len(a_keys))
    stats.add("tabular_part_attributes", "changed", len(c_keys))
    stats.add("tabular_part_attributes", "deleted", len(d_keys))
    impacts.extend(_impacts_from_keys("tabular_part_attributes", a_keys, c_keys, d_keys))

    prev_cs = _extract_characteristic_schemes(prev_snapshot.get("characteristic_schemes"))
    new_cs = _extract_characteristic_schemes(new_snapshot.get("characteristic_schemes"))
    a_keys, c_keys, d_keys = _diff_maps_detailed(prev_cs, new_cs)
    stats.add("characteristic_schemes", "added", len(a_keys))
    stats.add("characteristic_schemes", "changed", len(c_keys))
    stats.add("characteristic_schemes", "deleted", len(d_keys))
    impacts.extend(_impacts_from_keys("characteristic_schemes", a_keys, c_keys, d_keys))

    prev_props = prev_snapshot.get("properties") or {}
    new_props = new_snapshot.get("properties") or {}

    for group, extractor in (
        ("url_templates", _extract_url_templates),
        ("journal_graphs", _extract_journal_graphs),
    ):
        prev_map_s = extractor(prev_props)
        new_map_s = extractor(new_props)
        a_keys, c_keys, d_keys = _diff_maps_detailed(prev_map_s, new_map_s)
        stats.add(group, "added", len(a_keys))
        stats.add(group, "changed", len(c_keys))
        stats.add(group, "deleted", len(d_keys))
        impacts.extend(_impacts_from_keys(group, a_keys, c_keys, d_keys))

    prev_um = _extract_url_methods(prev_props)
    new_um = _extract_url_methods(new_props)
    a_keys, c_keys, d_keys = _diff_maps_detailed(prev_um, new_um)
    stats.add("url_methods", "added", len(a_keys))
    stats.add("url_methods", "changed", len(c_keys))
    stats.add("url_methods", "deleted", len(d_keys))
    impacts.extend(_impacts_from_keys("url_methods", a_keys, c_keys, d_keys))

    # movements — это relationship, не порождает graph-узлов, impact не нужен.
    prev_mv = _extract_movements(prev_props)
    new_mv = _extract_movements(new_props)
    a, c, d = _diff_maps(prev_mv, new_mv)
    stats.add("movements", "added", a)
    stats.add("movements", "changed", c)
    stats.add("movements", "deleted", d)

    return stats, impacts
