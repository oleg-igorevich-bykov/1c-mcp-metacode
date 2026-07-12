"""
Canonical category normalization utility for metadata search.

This module provides canon_category and canon_categories functions that
map user-supplied category names and synonyms (case-insensitive, with 'ё' normalization)
to the canonical category names used in the graph (e.g., 'Справочники', 'Документы', ...).

It prefers existing dictionaries from the codebase when available (optional imports)
and falls back to an internal minimal mapping. No database access required.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Dict, Set

logger = logging.getLogger(__name__)

# Canonical RU category names. Must include every value that scanner can write to
# `Routine.owner_category` (which goes through `ru_category_from_folder`), so query-side
# `canon_categories([RU])` round-trips. Keep in sync with the value set of
# `xml_metadata.folder_map.FOLDER_TO_RU_CATEGORY`.
CANONICAL_CATEGORIES = [
    "Справочники",
    "Документы",
    "Перечисления",
    "РегистрыСведений",
    "РегистрыНакопления",
    "РегистрыБухгалтерии",
    "РегистрыРасчета",
    "ПланыСчетов",
    "ПланыВидовХарактеристик",
    "ПланыВидовРасчета",
    "БизнесПроцессы",
    "Задачи",
    "ПланыОбмена",
    "ОбщиеМодули",
    "ОбщиеФормы",
    "ОбщиеКоманды",
    "ЖурналыДокументов",
    "НумераторыДокументов",
    "Последовательности",
    "Роли",
    "HTTPСервисы",
    "WebСервисы",
    "WSСсылки",
    "СервисыИнтеграции",
    "ВнешниеИсточникиДанных",
    "ПакетыXDTO",
    "Отчеты",
    "Обработки",
    "Константы",
    "ХранилищаНастроек",
    "ПодпискиНаСобытия",
    "РегламентныеЗадания",
    "ОбщиеРеквизиты",
    "ОпределяемыеТипы",
    "ФункциональныеОпции",
    "ПараметрыФункциональныхОпций",
    "ГруппыКоманд",
    "КритерииОтбора",
    "ПараметрыСеанса",
    "ОбщиеМакеты",
    "Подсистемы",
    "Языки",
    "Стили",
    "ЭлементыСтиля",
    "ОбщиеКартинки",
    "Интерфейсы",
]

def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("ё", "е")

CANON_SET_LOWER: Set[str] = {_norm(c) for c in CANONICAL_CATEGORIES}
CANON_LOWER_TO_CASED: Dict[str, str] = {_norm(c): c for c in CANONICAL_CATEGORIES}

# Single-target synonyms: lower -> canonical cased
SYN_SINGLE: Dict[str, str] = {**CANON_LOWER_TO_CASED}

# Multi-target synonyms: lower -> list[canonical]
SYN_MULTI: Dict[str, List[str]] = {}

# Internal baseline synonyms
def _seed_internal() -> None:
    pairs = {
        "catalogs": "Справочники",
        "catalog": "Справочники",
        "documents": "Документы",
        "document": "Документы",
        "enums": "Перечисления",
        "enumerations": "Перечисления",
        "enum": "Перечисления",
        "informationregisters": "РегистрыСведений",
        "informationregister": "РегистрыСведений",
        "accumulationregisters": "РегистрыНакопления",
        "accumulationregister": "РегистрыНакопления",
        "chartofaccounts": "ПланыСчетов",
        "chartofcharacteristictypes": "ПланыВидовХарактеристик",
        "chartofcalculationtypes": "ПланыВидовРасчета",
        "businessprocesses": "БизнесПроцессы",
        "businessprocess": "БизнесПроцессы",
        "tasks": "Задачи",
        "task": "Задачи",
        "dataprocessors": "Обработки",
        "dataprocessor": "Обработки",
        "httpservices": "HTTPСервисы",
        "webservices": "WebСервисы",
        "webservice": "WebСервисы",
        # Russian singulars
        "справочник": "Справочники",
        "документ": "Документы",
        "перечисление": "Перечисления",
        "регистр сведений": "РегистрыСведений",
        "регистры сведений": "РегистрыСведений",
        "регистр накопления": "РегистрыНакопления",
        "регистры накопления": "РегистрыНакопления",
        "регистр бухгалтерии": "РегистрыБухгалтерии",
        "регистры бухгалтерии": "РегистрыБухгалтерии",
        "план счетов": "ПланыСчетов",
        "планы счетов": "ПланыСчетов",
        "план видов характеристик": "ПланыВидовХарактеристик",
        "планы видов характеристик": "ПланыВидовХарактеристик",
        "план видов расчета": "ПланыВидовРасчета",
        "планы видов расчета": "ПланыВидовРасчета",
        "план видов расчёта": "ПланыВидовРасчета",
        "планы видов расчёта": "ПланыВидовРасчета",
        "бизнес-процессы": "БизнесПроцессы",
        "бизнес процессы": "БизнесПроцессы",
        "общий модуль": "ОбщиеМодули",
        "общие модули": "ОбщиеМодули",
        "общие формы": "ОбщиеФормы",
        "общая команда": "ОбщиеКоманды",
        "общие команды": "ОбщиеКоманды",
        "журнал документов": "ЖурналыДокументов",
        "журналы документов": "ЖурналыДокументов",
        "роль": "Роли",
        "роли": "Роли",
        "внешние источники данных": "ВнешниеИсточникиДанных",
        "пакеты xdto": "ПакетыXDTO",
    }
    for k, v in pairs.items():
        SYN_SINGLE[_norm(k)] = v

    # Multi-target generic "регистры" => both kinds
    SYN_MULTI[_norm("регистры")] = ["РегистрыСведений", "РегистрыНакопления"]
    SYN_MULTI[_norm("registers")] = ["РегистрыСведений", "РегистрыНакопления"]

def _load_folder_map_isolated() -> Dict[str, str]:
    """
    Load xml_metadata/folder_map.py directly without triggering xml_metadata/__init__.py
    (which transitively pulls parsers.metadata_parser → config → pydantic). folder_map.py is pure
    Python with no project imports, so spec_from_file_location is safe.
    """
    import importlib.util
    from pathlib import Path
    fmp = Path(__file__).resolve().parent.parent / "xml_metadata" / "folder_map.py"
    if not fmp.is_file():
        return {}
    spec = importlib.util.spec_from_file_location("_isolated_folder_map", fmp)
    if spec is None or spec.loader is None:
        return {}
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(getattr(mod, "FOLDER_TO_RU_CATEGORY", {}) or {})


def _merge_optional_sources() -> None:
    # xml_metadata/folder_map.py is the project-wide source of truth for folder → RU mapping.
    # Loaded in isolation to avoid pulling pydantic-dependent siblings at import time.
    try:
        for en, ru in _load_folder_map_isolated().items():
            SYN_SINGLE[_norm(en)] = ru
            SYN_SINGLE.setdefault(_norm(ru), ru)  # identity round-trip
    except Exception:
        pass
    # xcf_utils.CAT_MAP_EN_TO_RU — legacy local map; folder_map already covers it, kept for
    # backward-compat in case third-party code extends CAT_MAP_EN_TO_RU at runtime.
    try:
        from xcf_utils import CAT_MAP_EN_TO_RU as XCF_CAT
        for k, v in (XCF_CAT or {}).items():
            SYN_SINGLE[_norm(k)] = v
    except Exception:
        pass
    # parsers.role_rights_parser.CATEGORY_MAP
    try:
        from parsers.role_rights_parser import CATEGORY_MAP as RR_CAT
        for k, v in (RR_CAT or {}).items():
            SYN_SINGLE[_norm(k)] = v
    except Exception:
        pass
    # tools_metadata_description.norm_map
    try:
        from mcpsrv.tools_metadata_description import norm_map as MD_NORM
        for k, v in (MD_NORM or {}).items():
            if _norm(v) == _norm("Регистры"):
                SYN_MULTI[_norm(k)] = ["РегистрыСведений", "РегистрыНакопления"]
            else:
                SYN_SINGLE[_norm(k)] = v
    except Exception:
        pass
    # graphdb.types: singular->plural mapping if available
    try:
        from graphdb import types as gtypes
        # Heuristically pick dicts defined there
        for name in dir(gtypes):
            obj = getattr(gtypes, name, None)
            if isinstance(obj, dict):
                sample_keys = {"Справочник", "Документ", "Перечисление", "ОбщийМодуль"}
                if any(k in obj for k in sample_keys):
                    for k, v in obj.items():
                        SYN_SINGLE[_norm(k)] = v
    except Exception:
        pass

_seed_internal()
_merge_optional_sources()

def canon_category(value: str) -> List[str]:
    """
    Return canonical category name(s) for a user-supplied value.
    - Returns [] if cannot determine.
    - Returns a list to support generic inputs like 'Регистры' -> ['РегистрыСведений','РегистрыНакопления'].
    """
    if not value or not str(value).strip():
        return []
    key = _norm(value)
    # direct multi
    if key in SYN_MULTI:
        return SYN_MULTI[key][:]
    # direct single
    if key in SYN_SINGLE:
        v = SYN_SINGLE[key]
        # Normalize 'ё' variants of canonical
        v = v.replace("Ё", "Е").replace("ё", "е")
        # If v equals 'ПланыВидовРасчёта' unify to 'ПланыВидовРасчета'
        if _norm(v) == _norm("ПланыВидовРасчета") or _norm(v) == _norm("ПланыВидовРасчёта"):
            v = "ПланыВидовРасчета"
        return [v]
    # matches canonical with different case/diacritics
    if key in CANON_SET_LOWER:
        return [CANON_LOWER_TO_CASED[key]]
    return []

def canon_categories(categories: Iterable[str]) -> List[str]:
    """
    Canonicalize a list of category inputs to the exact graph category names.
    - Expands generic inputs (e.g., 'Регистры' -> both register categories).
    - Deduplicates while preserving order.
    """
    seen: Set[str] = set()
    out: List[str] = []
    for c in categories or []:
        for v in canon_category(c):
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out

_OWNER_KIND_TO_OWNER_CATEGORY: Dict[str, str] = {
    "ОбщийМодуль": "ОбщиеМодули",
    "ОбщаяФорма": "ОбщиеФормы",
    "ОбщаяКоманда": "ОбщиеКоманды",
}


def owner_kind_to_owner_category(owner_kind: str, owner_label: str) -> str:
    """
    Денормализация owner_kind в plural canonical owner_category, который кладётся на Routine
    как filterable property для prefilter в vec_routine_doc_description.

    - owner_label == "Configuration" → "" (для конфигурационных модулей фильтр идёт через module_type).
    - "ОбщийМодуль"/"ОбщаяФорма"/"ОбщаяКоманда" → fixed plural map.
    - object-owners: scanner уже даёт canonical plural через ru_category_from_folder
      (например, "Справочники", "Документы", "Отчеты", "Обработки", "Константы", "РегистрыБухгалтерии").
      Возвращаем owner_kind AS-IS — без прогона через canon_category, который для части plural-форм
      вернёт пустой список.
    """
    label = (owner_label or "").strip()
    kind = (owner_kind or "").strip()
    if label == "Configuration":
        return ""
    if not kind:
        return ""
    mapped = _OWNER_KIND_TO_OWNER_CATEGORY.get(kind)
    if mapped:
        return mapped
    return kind


__all__ = [
    "canon_category",
    "canon_categories",
    "owner_kind_to_owner_category",
    "CANONICAL_CATEGORIES",
]