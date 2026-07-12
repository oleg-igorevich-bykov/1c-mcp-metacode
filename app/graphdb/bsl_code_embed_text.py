"""
Build the text that goes into a BSL code embedding (raw default mode).

Metadata-context format:

    Object routine:
        //<metadata_type_ru>.<object_name>
        //<symbol_name>
        <raw unit body>

    Form routine:
        //<metadata_type_ru>.<object_name>.<form_name>
        //<symbol_name>
        <raw unit body>

The compressed pipeline uses a different prefix ("//Объект: ", "//Форма: ",
"//Процедура: ", "//Функция: ") — see bsl_code_compress.py. Raw text always
remains the source of truth for BM25/RLM/field-scoring/fragment slicing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY


_PROCEDURE_LABEL_RE = re.compile(
    r"^\s*(?:Процедура|Функция|Procedure|Function)\s+", re.IGNORECASE
)


# owner_qn segments that come from CommonModules/CommonForms/CommonCommands
# and the synthetic configuration-module category.
_COMMON_CATEGORIES: frozenset[str] = frozenset({
    "ОбщиеМодули",
    "ОбщиеФормы",
    "ОбщиеКоманды",
    "Конфигурация",
})

# Allowed values for metadata_type_ru in object/form owner_qns.
# Source of truth = FOLDER_TO_RU_CATEGORY.values(); singular forms ("Справочник",
# "Документ") are intentionally rejected — scanner only writes plural.
_ALLOWED_METADATA_TYPES: frozenset[str] = frozenset(FOLDER_TO_RU_CATEGORY.values()) | _COMMON_CATEGORIES


@dataclass(frozen=True)
class UnitContext:
    metadata_type_ru: str
    object_name: str
    form_name: str
    symbol_name: str
    routine_type: str  # "procedure" or "function"


def _starts_with_symbol_signature(body: str, symbol_name: str) -> bool:
    if not body or not symbol_name:
        return False
    m = _PROCEDURE_LABEL_RE.match(body)
    if not m:
        return False
    rest = body[m.end():]
    end = 0
    while end < len(rest) and (rest[end].isalnum() or rest[end] == "_"):
        end += 1
    return rest[:end].casefold() == symbol_name.casefold()


def _already_has_metadata_prefix(body: str) -> bool:
    stripped = body.lstrip()
    if not stripped:
        return False
    first_line = stripped.splitlines()[0]
    if not first_line.startswith("//"):
        return False
    return not first_line.startswith(
        ("//ПроцедураФункция:", "//Объект:", "//Форма:", "//Процедура:", "//Функция:")
    )


def metadata_context_prefix(body: str, ctx: UnitContext) -> str:
    if _already_has_metadata_prefix(body):
        return ""

    parts = []
    metadata_type_ru = (ctx.metadata_type_ru or "").strip()
    object_name = (ctx.object_name or "").strip()
    form_name = (ctx.form_name or "").strip()
    symbol_name = (ctx.symbol_name or "").strip()

    if metadata_type_ru and object_name:
        object_ref = f"{metadata_type_ru}.{object_name}"
        if form_name:
            parts.append(f"//{object_ref}.{form_name}")
        else:
            parts.append(f"//{object_ref}")

    if symbol_name and not _starts_with_symbol_signature(body, symbol_name):
        parts.append(f"//{symbol_name}")

    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def build_raw_embedding_text(body: str, ctx: UnitContext) -> str:
    return metadata_context_prefix(body, ctx) + (body or "")


def parse_owner_qn(owner_qn: Optional[str]) -> Tuple[str, str, str]:
    """
    Parse a slash-based owner_qn (as written by bsl_signature_scanner) into
    (metadata_type_ru, object_name, form_name).

    Forms accepted (after dropping the first two segments project_name/config_name):
        []                                             -> ("Конфигурация", "", "")
        ["ОбщиеМодули", <name>]                        -> ("ОбщиеМодули", <name>, "")
        ["ОбщиеФормы", <name>]                         -> ("ОбщиеФормы", <name>, "")
        ["ОбщиеКоманды", <name>]                       -> ("ОбщиеКоманды", <name>, "")
        [<cat>, <obj>]                                 -> (<cat>, <obj>, "")
        [<cat>, <obj>, "Form", <form>]                 -> (<cat>, <obj>, <form>)
        [<cat>, <obj>, "Command", <cmd>]               -> (<cat>, <obj>, "")
                                                          # command name comes via symbol_name

    Empty input or category not in the allow-list returns ("", "", "").
    The configuration form returns object_name="" — the caller is expected to
    fill it with the actual module file stem (ManagedApplicationModule, ...).
    """
    if not owner_qn:
        return "", "", ""
    parts = [p for p in owner_qn.split("/") if p]
    # First two segments are project_name and config_name; drop them.
    if len(parts) < 2:
        return "", "", ""
    tail = parts[2:]

    if not tail:
        return "Конфигурация", "", ""

    head = tail[0]
    if head not in _ALLOWED_METADATA_TYPES:
        return "", "", ""

    if len(tail) == 1:
        return head, "", ""

    if head in {"ОбщиеМодули", "ОбщиеФормы", "ОбщиеКоманды"}:
        return head, tail[1], ""

    if len(tail) == 2:
        return head, tail[1], ""

    # [<cat>, <obj>, "Form"|"Command", <name>]
    if len(tail) >= 4 and tail[2] == "Form":
        return head, tail[1], tail[3]
    if len(tail) >= 4 and tail[2] == "Command":
        return head, tail[1], ""

    return head, tail[1], ""
