"""LLM contract for object_summary.

The schema enforces a v1.6 payload shape. Section minimums (e.g.
`capabilities >= 3`, `usage_scenarios >= 2`, `phrases >= 5`) are NOT enforced
in the JSON schema — they are soft recommendations in the user prompt. Empty
sections are valid and the renderer skips them, so the model is not forced to
invent weak generalisations for thin profiles (Catalogs, Information Registers,
small Data Processors).

`validate_summary(payload)` returns a normalised dict plus a list of soft
warnings. Hard validation errors raise `ContractValidationError`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .constants import SUMMARY_CONTRACT_NAME

logger = logging.getLogger(__name__)


CONTRACT_SCHEMA_VERSION = "1.6"


class ContractValidationError(ValueError):
    """Raised when the LLM payload cannot be coerced into a v1.6 summary."""


# JSON-Schema for the LLM `response_format` channel (OpenAI-compatible).
# `strict: True` requires `additionalProperties: false` on every nested object.
OBJECT_SUMMARY_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "human_summary",
        "search_terms",
        "confidence",
    ],
    "properties": {
        "schema_version": {"type": "string", "enum": [CONTRACT_SCHEMA_VERSION]},
        "human_summary": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "title",
                "core_idea",
                "data_scope",
                "capabilities",
                "usage_scenarios",
                "effects",
                "uncertainties",
            ],
            "properties": {
                "title": {"type": "string"},
                "core_idea": {"type": "string"},
                "data_scope": {"type": "string"},
                "capabilities": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "description"],
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "usage_scenarios": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "description"],
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "effects": {"type": "string"},
                "uncertainties": {"type": "string"},
            },
        },
        "search_terms": {
            "type": "object",
            "additionalProperties": False,
            "required": ["phrases", "keywords"],
            "properties": {
                "phrases": {
                    "type": "array",
                    "maxItems": 20,
                    "items": {"type": "string"},
                },
                "keywords": {
                    "type": "array",
                    "maxItems": 30,
                    "items": {"type": "string"},
                },
            },
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}


RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "object_summary",
        "strict": True,
        "schema": OBJECT_SUMMARY_JSON_SCHEMA,
    },
}


SYSTEM_PROMPT = (
    "Ты формируешь человекочитаемую сводку по объекту метаданных 1С на основе object_profile.\n"
    "Не выдумывай факты вне profile.\n"
    "Раздели результат на human_summary и search_terms.\n"
    "human_summary — связный текст по разделам.\n"
    "search_terms — отдельные поисковые фразы и ключевые слова.\n"
    "Не перечисляй все реквизиты, процедуры, формы и технические связи.\n"
    "Технические BSL-факты переводи в предметное описание поведения.\n"
    "Если есть extension_context, описывай итоговое поведение объекта как базовая конфигурация + применимые расширения.\n"
    "Верни строго JSON по схеме."
)


USER_PROMPT_TEMPLATE = (
    "Сделай object_summary по object_profile.\n"
    "\n"
    "Рекомендации (мягкие, можно нарушать при бедном профиле):\n"
    "- capabilities: стремись к 3-6, если профиль это подтверждает; для скудного профиля допустимо меньше.\n"
    "- usage_scenarios: 2-4 если есть основание; не выдумывай ради числа.\n"
    "- search_terms.phrases: 5-12 естественных формулировок; для очень узкого объекта меньше.\n"
    "- search_terms.keywords: короткие термины, не дубли фраз.\n"
    "- Если не хватает фактов, объясни ограничение в uncertainties и сократи или оставь пустыми соответствующие списки.\n"
    "\n"
    "Что важно:\n"
    "- core_idea: суть объекта и его роль; 2-3 предложения.\n"
    "- data_scope: какие виды данных хранит/обрабатывает (не перечень полей).\n"
    "- effects: учётные последствия работы объекта; без имён регистров.\n"
    "- BSL code: ; разделяет факты; >Имя — вызов; Записать:/Удалить:/Прочитать: — операции; СформироватьДвижения*/Проверить*/Отказ — сигналы поведения.\n"
    "- Не вставляй имена процедур/relation/HTML/Markdown в текстовые поля.\n"
    "- Если есть extension_context, считай объект результатом базы + расширений; описывай поведение целиком, не отдельной оговоркой.\n"
    "\n"
    "object_profile ({profile_format}):\n"
    "```{profile_format}\n"
    "{object_profile}\n"
    "```\n"
)


def build_user_prompt(object_profile: str, *, profile_format: str = "toon") -> str:
    return USER_PROMPT_TEMPLATE.format(
        profile_format=profile_format,
        object_profile=object_profile,
    )


@dataclass
class ValidatedSummary:
    payload: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return []


def _normalise_titled_list(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for item in _coerce_list(raw):
        if not isinstance(item, dict):
            continue
        title = _coerce_str(item.get("title")).strip()
        desc = _coerce_str(item.get("description")).strip()
        if not title and not desc:
            continue
        out.append({"title": title, "description": desc})
    return out


def _normalise_strings(raw: Any) -> List[str]:
    out: List[str] = []
    for item in _coerce_list(raw):
        s = _coerce_str(item).strip()
        if s:
            out.append(s)
    return out


def validate_summary(raw: Any) -> ValidatedSummary:
    """Coerce the LLM response into a v1.6 payload.

    The schema is enforced softly: missing/empty fields are filled with safe
    defaults and recorded as warnings. The function raises only if the payload
    is so malformed that no `human_summary` and no `search_terms` can be
    salvaged — in that case S1 must skip the object.
    """
    warnings: List[str] = []

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractValidationError(f"LLM returned non-JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ContractValidationError(
            f"Top-level must be an object, got {type(raw).__name__}"
        )

    schema_version = _coerce_str(raw.get("schema_version")).strip()
    if schema_version != CONTRACT_SCHEMA_VERSION:
        warnings.append(
            f"schema_version mismatch: got {schema_version!r}, expected {CONTRACT_SCHEMA_VERSION!r}"
        )

    hs_raw = raw.get("human_summary") or {}
    if not isinstance(hs_raw, dict):
        raise ContractValidationError(
            f"human_summary must be an object, got {type(hs_raw).__name__}"
        )

    st_raw = raw.get("search_terms") or {}
    if not isinstance(st_raw, dict):
        raise ContractValidationError(
            f"search_terms must be an object, got {type(st_raw).__name__}"
        )

    human_summary = {
        "title": _coerce_str(hs_raw.get("title")).strip(),
        "core_idea": _coerce_str(hs_raw.get("core_idea")).strip(),
        "data_scope": _coerce_str(hs_raw.get("data_scope")).strip(),
        "capabilities": _normalise_titled_list(hs_raw.get("capabilities")),
        "usage_scenarios": _normalise_titled_list(hs_raw.get("usage_scenarios")),
        "effects": _coerce_str(hs_raw.get("effects")).strip(),
        "uncertainties": _coerce_str(hs_raw.get("uncertainties")).strip(),
    }

    if not human_summary["title"]:
        warnings.append("human_summary.title is empty")
    if not human_summary["core_idea"]:
        warnings.append("human_summary.core_idea is empty")

    search_terms = {
        "phrases": _normalise_strings(st_raw.get("phrases"))[:20],
        "keywords": _normalise_strings(st_raw.get("keywords"))[:30],
    }

    confidence = _coerce_str(raw.get("confidence")).strip().lower()
    if confidence not in {"high", "medium", "low"}:
        warnings.append(f"confidence has unexpected value {confidence!r}; defaulting to 'medium'")
        confidence = "medium"

    payload = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "human_summary": human_summary,
        "search_terms": search_terms,
        "confidence": confidence,
    }
    return ValidatedSummary(payload=payload, warnings=warnings)


def contract_label() -> str:
    return SUMMARY_CONTRACT_NAME
