"""Constants for the object_summary feature.

Versioning rules:
  * source versions (`SUMMARY_SCHEMA_VERSION`, `PROFILE_SCHEMA_VERSION`) describe
    what the LLM produced and what evidence shape the profile is built from.
    Bumping any of them invalidates `summary.json` entirely and triggers a
    full S1 regeneration.
  * derived versions (`EMBEDDING_DOCUMENT_BUILDER_VERSION`,
    `SEARCH_TEXT_BUILDER_VERSION`) describe local builders that read
    `summary.json` and produce indexable text. Bumping them triggers a cheap
    local rebuild (no LLM call): embedding-only via S2, or `search_text` only.

All versions are plain integers — string comparison breaks at "1.9" < "1.10".
The human-facing contract name "object_summary_v1.6" is kept separately for
logs and diagnostics; it is not used for comparisons.
"""

from __future__ import annotations

from typing import Dict, Tuple

SUMMARY_CONTRACT_NAME = "object_summary_v1.6"

SUMMARY_SCHEMA_VERSION: int = 1
PROFILE_SCHEMA_VERSION: int = 2
EMBEDDING_DOCUMENT_BUILDER_VERSION: int = 1
SEARCH_TEXT_BUILDER_VERSION: int = 1


SUPPORTED_CATEGORIES: Tuple[str, ...] = (
    "Справочники",
    "Документы",
    "РегистрыСведений",
    "РегистрыНакопления",
    "Обработки",
    "HTTPСервисы",
    "БизнесПроцессы",
    "Задачи",
)


TYPE_ALIASES: Dict[str, str] = {
    "Число": "Ч",
    "Строка": "С",
    "Дата": "Д",
    "Булево": "Б",
    "СправочникСсылка": "СС",
    "ДокументСсылка": "ДС",
    "ПеречислениеСсылка": "ПС",
    "ПланВидовРасчетаСсылка": "ПВРС",
    "ПланВидовХарактеристикСсылка": "ПВХС",
    "ОпределяемыйТип": "ОТ",
}


SIZE_POLICIES: Dict[str, Dict[str, int]] = {
    "small": {
        "max_forms": 20,
        "max_commands": 20,
        "max_relationships_total": 60,
        "max_bsl_routines": 12,
    },
    "medium": {
        "max_forms": 40,
        "max_commands": 40,
        "max_relationships_total": 120,
        "max_bsl_routines": 30,
    },
    "large": {
        "max_forms": 80,
        "max_commands": 80,
        "max_relationships_total": 250,
        "max_bsl_routines": 40,
    },
}


def get_size_policy(name: str) -> Dict[str, int]:
    return SIZE_POLICIES.get((name or "").strip().lower(), SIZE_POLICIES["medium"])


def filter_supported_categories(categories: list[str]) -> list[str]:
    """Intersect a user-provided list with `SUPPORTED_CATEGORIES`."""
    allowed = {c for c in SUPPORTED_CATEGORIES}
    return [c for c in categories if c in allowed]
