"""
TypeRefMixin: normalization and reference extraction for 1C 'Тип' values.

Extracts referenced metadata targets from property values like:
- "Справочники.Номенклатура"
- "СправочникСсылка.Номенклатура"
- Container types like "Массив(СправочникСсылка.Номенклатура)" are unwrapped.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


def normalize_type_for_display(value: Any) -> Optional[str]:
    """Presentation-level normalization of a 1C 'Тип' value as stored in the graph.

    Input (как лежит в графе): None | str | list[str].

    Output: None or a single string. Single 1C type — plain atom string
    ("СправочникСсылка.Должности", "Дата(Дата)"). Composite type — atoms
    joined with "|" without spaces ("Число|СправочникСсылка.Контрагенты").

    Heuristic порт из app/xml_metadata/property_extractor.py:962-963 —
    разделяет «артефакт сплита loader-а внутри одного primitive с
    qualifier-ами» от «настоящий составной тип»:
      - list len == 1 → element as string;
      - list len >= 2 AND first contains '(' AND last endswith ')' →
        склейка через ',' в один атом (это разломанный quantifier
        одного primitive type'а);
      - иначе → каждый элемент — атом, склейка через '|'.

    Separate from TypeRefMixin._normalize_type_value: that one feeds
    USED_IN reference extraction (domain), this one feeds display
    (presentation). They co-exist.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, list):
        items = [str(x).strip() for x in value if str(x).strip()]
        if not items:
            return None
        if len(items) == 1:
            return items[0]
        if "(" in items[0] and items[-1].endswith(")"):
            return ",".join(items)
        return "|".join(items)
    s = str(value).strip()
    return s or None

# Reverse mapping: singular 1C category name as used in "Состав" -> plural category_name used in graph
# Examples:
#   "Справочник" -> "Справочники"
#   "ОбщийМодуль" -> "ОбщиеМодули"
SINGULAR_TO_PLURAL: Dict[str, str] = {
    "ОбщийМодуль": "ОбщиеМодули",
    "Справочник": "Справочники",
    "Документ": "Документы",
    "Перечисление": "Перечисления",
    "Обработка": "Обработки",
    "Отчет": "Отчеты",
    "РегистрСведений": "РегистрыСведений",
    "РегистрНакопления": "РегистрыНакопления",
    "ПланВидовРасчета": "ПланыВидовРасчета",
    "ПланВидовХарактеристик": "ПланыВидовХарактеристик",
    "ПланСчетов": "ПланыСчетов",
    "ОбщаяКоманда": "ОбщиеКоманды",
    "ОбщаяФорма": "ОбщиеФормы",
    "БизнесПроцесс": "БизнесПроцессы",
    "Задача": "Задачи",
}


class TypeRefMixin:
    """Provides helpers to normalize and extract references from 1C type literals."""

    def _normalize_type_value(self, v: Any) -> List[str]:
        """Normalize 1C type value (string or list) into flat list of tokens."""
        tokens: List[str] = []
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("\n", ",").split(",") if p.strip()]
        elif isinstance(v, list):
            parts = []
            for item in v:
                if isinstance(item, str):
                    parts.extend([p.strip() for p in item.replace("\n", ",").split(",") if p.strip()])
                else:
                    try:
                        s = str(item).strip()
                        if s:
                            parts.append(s)
                    except Exception:
                        pass
        else:
            try:
                s = str(v).strip()
                parts = [s] if s else []
            except Exception:
                parts = []
        for p in parts:
            if p:
                tokens.append(p)
        return tokens

    def _unwrap_container_types(self, token: str) -> List[str]:
        """Unwrap container/wrapper types like Массив(Тип), СписокЗначений(Тип) to inner type tokens."""
        s = (token or "").strip().strip('"').strip("'")
        out: List[str] = []

        def _split_top_level_args(inner: str) -> List[str]:
            # Simple splitter for common cases; nested parentheses inside 1C type arguments are unlikely
            return [a.strip() for a in inner.split(",") if a.strip()]

        # Peel nested wrappers, if any
        while True:
            open_idx = s.find("(")
            close_idx = s.rfind(")")
            if open_idx != -1 and close_idx != -1 and close_idx > open_idx:
                inner = s[open_idx + 1 : close_idx]
                parts = _split_top_level_args(inner)
                if not parts:
                    break
                for ip in parts:
                    out.extend(self._unwrap_container_types(ip))
                return out
            break

        out.append(s)
        return out

    def _map_type_token_to_targets(self, token: str) -> List[Tuple[List[str], str]]:
        """
        Map a single type token to (categories, name) targets.
        Supports forms:
          - 'СправочникСсылка.Номенклатура' via prefix mapping
          - 'Справочники.Номенклатура' via direct category
        Primitives are ignored.
        """
        s = (token or "").strip()
        if not s:
            return []

        primitives = {
            "Строка","Число","Булево","Дата","ХранилищеЗначения","Неопределено","Null",
            "UUID","УникальныйИдентификатор","ЧислоНеограниченнойТочности","МоментВремени",
            "ДвоичныеДанные","Текст","ЧислоСФиксТочностью","ЧислоСФиксированнойТочностью"
        }
        if s in primitives:
            return []

        direct_categories = {
            "Справочники","Документы","Перечисления","РегистрыСведений","РегистрыНакопления",
            "ПланыВидовХарактеристик","ПланыСчетов","ПланыВидовРасчета","БизнесПроцессы","Задачи"
        }
        prefix_map: Dict[str, List[str]] = {
            "СправочникСсылка": ["Справочники"],
            "ДокументСсылка": ["Документы"],
            "ПеречислениеСсылка": ["Перечисления"],
            "РегистрСведенийСсылка": ["РегистрыСведений"],
            "РегистрНакопленияСсылка": ["РегистрыНакопления"],
            "ПланВидовХарактеристикСсылка": ["ПланыВидовХарактеристик"],
            "ПланСчетовСсылка": ["ПланыСчетов"],
            "ПланВидовРасчетаСсылка": ["ПланыВидовРасчета"],
            "БизнесПроцессСсылка": ["БизнесПроцессы"],
            "ЗадачаСсылка": ["Задачи"],

            # Объект-варианты для тех же категорий (поддержка cfg:*Object -> <Категория>Объект)
            "СправочникОбъект": ["Справочники"],
            "ДокументОбъект": ["Документы"],
            "БизнесПроцессОбъект": ["БизнесПроцессы"],
            "ЗадачаОбъект": ["Задачи"],
            "ПланВидовХарактеристикОбъект": ["ПланыВидовХарактеристик"],
            "ПланСчетовОбъект": ["ПланыСчетов"],
            "ПланВидовРасчетаОбъект": ["ПланыВидовРасчета"],
        }

        parts = s.split(".", 1)
        targets: List[Tuple[List[str], str]] = []
        if len(parts) == 2:
            head, tail = parts[0].strip(), parts[1].strip()
            if head in direct_categories:
                if tail:
                    targets.append(([head], tail))
            elif head in prefix_map:
                if tail:
                    targets.append((prefix_map[head], tail))
        return targets

    def _extract_type_refs(self, type_value: Any) -> List[Tuple[List[str], str, str]]:
        """
        Extract referenced metadata object targets from a 'Тип' value.
        Returns list of tuples: (categories: List[str], target_name: str, type_literal: str)
        """
        refs: List[Tuple[List[str], str, str]] = []
        tokens = self._normalize_type_value(type_value)
        for tok in tokens:
            for inner in self._unwrap_container_types(tok):
                for cats, name in self._map_type_token_to_targets(inner):
                    if cats and name:
                        refs.append((cats, name, tok))
        # Deduplicate
        dedup: List[Tuple[List[str], str, str]] = []
        seen = set()
        for cats, name, lit in refs:
            key = ("|".join(cats), name, lit)
            if key in seen:
                continue
            seen.add(key)
            dedup.append((cats, name, lit))
        return dedup