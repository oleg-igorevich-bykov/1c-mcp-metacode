from __future__ import annotations

from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import hashlib
import logging
import re

from xcf_utils import ru_category_from_folder, compute_form_qn
from config import settings
from graphdb.category_canon import owner_kind_to_owner_category

_EXT_DECORATOR_RE = re.compile(
    r'&(Перед|После|Вместо|ИзменениеИКонтроль)\("([^"]+)"\)',
    re.IGNORECASE,
)


def parse_extension_decorator(directives: List[str]) -> Optional[Tuple[str, str]]:
    """Возвращает (decorator_type, target_name) из первого найденного декоратора расширения или None."""
    for d in directives:
        m = _EXT_DECORATOR_RE.search(d)
        if m:
            return m.group(1), m.group(2)
    return None

ENCODINGS_TRY = ["utf-16", "utf-16-le", "utf-16-be", "utf-8-sig", "utf-8", "cp1251"]
logger = logging.getLogger(__name__)


def _read_text(path: Path) -> str:
    # Robust decoding with BOM detection and sane order to avoid false-positive utf-16 decodes
    data = path.read_bytes()
    # BOM-based fast paths
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        # Let Python handle endianness via 'utf-16' when BOM present
        return data.decode("utf-16")

    # Try common encodings first; avoid trying utf-16 before UTF-8/CP1251
    for enc in ("utf-8", "cp1251", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            s = data.decode(enc)
            # Heuristic: if the raw data contains CR/LF bytes but decoded text has no line breaks, decoding is likely wrong
            if (b"\r" in data or b"\n" in data) and ("\r" not in s and "\n" not in s):
                continue
            return s
        except UnicodeError:
            continue
    # Fallback (lossy)
    return data.decode("utf-8", errors="ignore")


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _sha256_json(payload: Any) -> str:
    """sha256 hex digest от JSON-сериализованного payload.

    Использует `ensure_ascii=False` + `sort_keys=False` — стабильно для list/dict
    с предсказуемым порядком (мы вызываем только с list-of-primitives в плановых
    хешах).
    """
    import json as _json
    raw = _json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _compute_routine_doc_hash(r: Dict[str, Any]) -> str:
    return _sha256_json([
        r.get("doc_description", "") or "",
        r.get("doc_params_text", "") or "",
        r.get("doc_return_text", "") or "",
    ])


def _compute_routine_doc_description_embedding_hash(r: Dict[str, Any]) -> str:
    """Hash только от doc_description — поле, которое реально эмбеддится.

    Используется для точечной инвалидации doc_description_embedding:
    изменение doc_params_text / doc_return_text / signature / body само по себе
    не должно очищать embedding, потому что RoutineDescriptionIndexer читает
    только Routine.doc_description.
    """
    return _sha256_json([r.get("doc_description", "") or ""])


def _compute_routine_signature_hash(r: Dict[str, Any]) -> str:
    return _sha256_json([
        r.get("routine_type", "") or "",
        r.get("name", "") or "",
        r.get("params_text", "") or "",
        r.get("params_json", []) or [],
        bool(r.get("export", False)),
        list(r.get("directives", []) or []),
        r.get("decorator_type", "") or "",
        r.get("decorator_target", "") or "",
    ])


def _compute_routine_state_hash(r: Dict[str, Any]) -> str:
    return _sha256_json([
        r.get("body_hash", "") or "",
        r.get("doc_hash", "") or "",
        r.get("signature_hash", "") or "",
        int(r.get("line", 0) or 0),
        r.get("file_path", "") or "",
    ])


def _casefold(s: str) -> str:
    # Case-insensitive processing with locale stability
    return (s or "").casefold()


def _is_letter(ch: str) -> bool:
    return ch.isalpha() or ch == "_"


# Invisible/space helpers and line break normalization support
INVISIBLES = {"\uFEFF", "\u200B", "\u00A0"}  # BOM, ZWSP, NBSP

def _is_space_or_invisible(ch: str) -> bool:
    return ch in (" ", "\t") or ch in INVISIBLES

def _is_line_break(ch: str) -> bool:
    # For robustness; we normalize to '\n', but keep helper for future logic
    return ch == "\n" or ch == "\r"


def _split_params(params_slice: str) -> List[str]:
    # Split by commas outside of quotes
    items: List[str] = []
    buf: List[str] = []
    in_string = False
    i = 0
    n = len(params_slice)
    while i < n:
        ch = params_slice[i]
        if ch == '"':
            if in_string:
                # 1C escaping: "" inside string
                if i + 1 < n and params_slice[i + 1] == '"':
                    buf.append('""')
                    i += 2
                    continue
                else:
                    in_string = False
                    buf.append(ch)
                    i += 1
                    continue
            else:
                in_string = True
                buf.append(ch)
                i += 1
                continue
        if not in_string and ch == ",":
            items.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        items.append("".join(buf).strip())
    # Drop empty parameters (robustness)
    return [p for p in items if p]
    

# Helpers for Phase 2 dynamic call parsing
def _read_string_literal_value(s: str, p: int, end: int) -> Tuple[Optional[str], int]:
   """
   Read BSL string literal starting at index p (s[p] must be '"').
   Handles doubled quotes "" inside string. Returns (value, new_pos_after_literal).
   If malformed, returns (None, p).
   """
   if p >= end or s[p] != '"':
       return None, p
   buf: List[str] = []
   i = p + 1
   while i < end:
       ch = s[i]
       if ch == '"':
           # doubled quote -> literal quote
           if i + 1 < end and s[i + 1] == '"':
               buf.append('"')
               i += 2
               continue
           # end of literal
           i += 1
           return "".join(buf), i
       buf.append(ch)
       i += 1
   # no closing quote
   return None, p

def _parse_inner_call_from_string(expr: str) -> Tuple[Optional[str], Optional[str], Optional[int]]:
   """
   Parse simple call expression text from string literal:
     - Ident(args)
     - Ident1.Ident2(args)
   Returns (qualifier, name, args_count) or (None, None, None) if not matched.
   """
   if not isinstance(expr, str):
       return None, None, None
   s = expr.strip()
   if not s:
       return None, None, None
   # find first "(" and the matching last ")"
   try:
       open_idx = s.index("(")
   except ValueError:
       return None, None, None
   head = s[:open_idx].strip()
   # naive close: last ')' (sufficient for simple cases)
   close_idx = s.rfind(")")
   params_slice = s[open_idx + 1 : close_idx] if close_idx > open_idx else ""
   parts = [p.strip() for p in head.split(".", 1)]
   if not parts or not parts[0]:
       return None, None, None
   if len(parts) == 1:
       qualifier = None
       name = parts[0]
   else:
       qualifier, name = parts[0], parts[1]
   # args count with existing splitter
   try:
       args = _split_params(params_slice)
       args_cnt = len(args)
   except Exception:
       args_cnt = None
   return qualifier, name, args_cnt

def _casefold_eq(a: str, b: str) -> bool:
   return _casefold(a) == _casefold(b)

def _is_thisobject_token(tok: str) -> bool:
   low = _casefold(tok)
   return low in ("этотобъект", "thisobject")

def _parse_param_item(raw: str) -> Dict[str, Any]:
    # Detect default and simple markers (НеОбязательный, Знач)
    s = raw.strip()
    default_present = "=" in s
    name_part = s.split("=", 1)[0].strip() if default_present else s
    default_text = s.split("=", 1)[1].strip() if default_present else None

    low = _casefold(s)
    markers: List[str] = []
    # Simple textual presence checks (no evaluation)
    if "необязатель" in low:  # НеОбязательный
        markers.append("НеОбязательный")
    if "знач" == low[:4] or low.startswith("знач "):
        markers.append("Знач")
    return {
        "name": name_part,
        "default_present": default_present,
        "default_text": default_text,
        "markers_raw": markers,
        "raw": raw,
    }


def _extract_name_and_params(header_buf: str) -> Tuple[str, str]:
    # header like: "Процедура Имя(Парам1, ... ) Экспорт"
    # We only need name and content inside first-level parentheses.
    s = header_buf.strip()
    # Find first "(" after keyword
    try:
        open_idx = s.index("(")
        close_idx = s.rfind(")")
        close_idx = close_idx if close_idx > open_idx else -1
    except ValueError:
        open_idx, close_idx = -1, -1
    # Extract name segment between keyword and "("
    # keywords can be "Процедура"/"Функция"/"Procedure"/"Function"
    tokens = s[:open_idx].strip().split()
    name = tokens[-1] if tokens else ""
    params_slice = s[open_idx + 1 : close_idx] if (open_idx != -1 and close_idx != -1) else ""
    return name, params_slice


def _is_keyword_at(text: str, idx: int, kw: str) -> bool:
    # kw must be matched as a word (boundary by non-letter)
    n = len(text)
    m = len(kw)
    if idx + m > n:
        return False
    seg = text[idx: idx + m]
    if _casefold(seg) != _casefold(kw):
        return False
    prev_ok = (idx == 0) or (not _is_letter(text[idx - 1]))
    next_ok = (idx + m == n) or (not _is_letter(text[idx + m]) and not text[idx + m].isdigit())
    return prev_ok and next_ok


def _scanner_posix_file_path(file_path: Path, code_root: Path) -> str:
    p = file_path if isinstance(file_path, Path) else Path(file_path)
    base = Path(settings.data_directory)
    try:
        return str(p.relative_to(base)).replace("\\", "/")
    except ValueError:
        try:
            return str(p.relative_to(code_root)).replace("\\", "/")
        except ValueError:
            return str(p).replace("\\", "/")


def _classify_module(code_root: Path, file_path: Path, project_name: str, config_name: str) -> Optional[Dict[str, Any]]:
    # Compute relative path parts
    try:
        rel = file_path.relative_to(code_root)
    except Exception:
        return None
    parts = list(rel.parts)
    fname = file_path.name

    def module_node_payload(module_type: str, owner_kind: str, owner_name: str, owner_qn: str, owner_label: str, ru_module_name: str, needs_module_node: bool = True) -> Dict[str, Any]:
            # module_id by (project|config|relative path)
            # Сначала заменяем слэши, чтобы избежать ошибки с f-string
            rel_path_posix = str(rel).replace('\\', '/')
            module_id = _sha1(f"{project_name}|{config_name}|{rel_path_posix}")
            return {
                "module_type": module_type,
                "owner_kind": owner_kind,
                "owner_name": owner_name,
                "owner_qn": owner_qn,
                "owner_label": owner_label,
                "ru_module_name": ru_module_name,
                "needs_module_node": needs_module_node,
                "module_id": module_id,
            }

    # CommonModules/<Name>/Ext/Module.bsl
    if "CommonModules" in parts:
        try:
            i = parts.index("CommonModules")
        except ValueError:
            i = -1
        if i != -1 and i + 2 < len(parts) and parts[i + 2] == "Ext" and fname.lower() == "module.bsl":
            cm_name = parts[i + 1]
            owner_qn = f"{project_name}/{config_name}/ОбщиеМодули/{cm_name}"
            # For CommonModules: NO separate Module node; the owner (MetadataObject) is the module
            return module_node_payload("CommonModule", "ОбщийМодуль", cm_name, owner_qn, owner_label="MetadataObject", ru_module_name="Общий модуль", needs_module_node=False)

    # CommonForms/<Form>/Ext/Form/Module.bsl
    # OR ordinary common form: CommonForms/<Form>/Ext/Form.bin
    if "CommonForms" in parts:
        try:
            i = parts.index("CommonForms")
        except ValueError:
            i = -1
        # Expect .../CommonForms/<Form>/Ext/Form/Module.bsl
        if i != -1 and i + 1 < len(parts) and fname.lower() == "module.bsl":
            # Owner is the CommonForm MetadataObject
            form_name = parts[i + 1]
            owner_qn = f"{project_name}/{config_name}/ОбщиеФормы/{form_name}"
            return module_node_payload("CommonFormModule", "ОбщаяФорма", form_name, owner_qn, owner_label="MetadataObject", ru_module_name="Модуль формы", needs_module_node=True)
        # Ordinary common form: .../CommonForms/<Form>/Ext/Form.bin
        if i != -1 and i + 1 < len(parts) and parts[i + 2] == "Ext" and fname.lower() == "form.bin":
            form_name = parts[i + 1]
            owner_qn = f"{project_name}/{config_name}/ОбщиеФормы/{form_name}"
            return module_node_payload("CommonFormModule", "ОбщаяФорма", form_name, owner_qn, owner_label="MetadataObject", ru_module_name="Модуль формы", needs_module_node=True)

    # CommonCommands/<Command>/Ext/CommandModule.bsl
    if "CommonCommands" in parts:
        try:
            i = parts.index("CommonCommands")
        except ValueError:
            i = -1
        if i != -1 and i + 2 < len(parts) and parts[i + 2] == "Ext" and fname == "CommandModule.bsl":
            cmd_name = parts[i + 1]
            owner_qn = f"{project_name}/{config_name}/ОбщиеКоманды/{cmd_name}"
            return module_node_payload(
                "CommandModule",
                "ОбщаяКоманда",
                cmd_name,
                owner_qn,
                owner_label="MetadataObject",
                ru_module_name="Модуль команды",
                needs_module_node=True,
            )

    # Generic modules for ANY metadata categories (object/manager/form)
    # Do not restrict to a fixed folder list; detect by structural patterns.

    # 1) Form module: .../<Category>/<Object>/Forms/<Form>/Ext/Form/Module.bsl
    #    OR ordinary form: .../<Category>/<Object>/Forms/<Form>/Ext/Form.bin
    try:
        idx_forms = parts.index("Forms")
    except ValueError:
        idx_forms = -1
    if (
        idx_forms >= 2
        and idx_forms + 3 < len(parts)
        and parts[idx_forms + 2] == "Ext"
        and parts[idx_forms + 3] == "Form"
        and fname.lower() == "module.bsl"
    ):
        cat_folder = parts[idx_forms - 2]
        obj_name = parts[idx_forms - 1]
        form_name = parts[idx_forms + 1]
        cat_ru = ru_category_from_folder(cat_folder)
        owner_qn = compute_form_qn(project_name, config_name, cat_ru, obj_name, form_name)
        return module_node_payload(
            "FormModule",
            cat_ru,
            f"{obj_name}/{form_name}",
            owner_qn,
            owner_label="Form",
            ru_module_name="Модуль формы",
            needs_module_node=True,
        )
    # 1b) Ordinary form module: .../<Category>/<Object>/Forms/<Form>/Ext/Form.bin
    if (
        idx_forms >= 2
        and idx_forms + 2 < len(parts)
        and parts[idx_forms + 2] == "Ext"
        and fname.lower() == "form.bin"
    ):
        cat_folder = parts[idx_forms - 2]
        obj_name = parts[idx_forms - 1]
        form_name = parts[idx_forms + 1]
        cat_ru = ru_category_from_folder(cat_folder)
        owner_qn = compute_form_qn(project_name, config_name, cat_ru, obj_name, form_name)
        return module_node_payload(
            "FormModule",
            cat_ru,
            f"{obj_name}/{form_name}",
            owner_qn,
            owner_label="Form",
            ru_module_name="Модуль формы",
            needs_module_node=True,
        )

    # 2) Object-level Command module: .../<Category>/<Object>/Commands/<Command>/Ext/CommandModule.bsl
    try:
        idx_cmds = parts.index("Commands")
    except ValueError:
        idx_cmds = -1
    if (
        idx_cmds >= 2
        and idx_cmds + 3 < len(parts)
        and parts[idx_cmds + 2] == "Ext"
        and fname == "CommandModule.bsl"
    ):
        cat_folder = parts[idx_cmds - 2]
        obj_name = parts[idx_cmds - 1]
        command_name = parts[idx_cmds + 1]
        cat_ru = ru_category_from_folder(cat_folder)
        owner_qn = f"{project_name}/{config_name}/{cat_ru}/{obj_name}/Command/{command_name}"
        return module_node_payload(
            "CommandModule",
            cat_ru,
            f"{obj_name}/{command_name}",
            owner_qn,
            owner_label="Command",
            ru_module_name="Модуль команды",
            needs_module_node=True,
        )

    # 3) Object/Manager and additional Ext modules: .../<Category>/<Object>/Ext/(... .bsl)
    try:
        idx_ext = parts.index("Ext")
    except ValueError:
        idx_ext = -1
    if (
        idx_ext >= 2
        and not (idx_ext + 1 < len(parts) and parts[idx_ext + 1] == "Form")  # avoid matching Form's Ext/Form handled above
    ):
        cat_folder = parts[idx_ext - 2]
        obj_name = parts[idx_ext - 1]
        cat_ru = ru_category_from_folder(cat_folder)
        owner_qn = f"{project_name}/{config_name}/{cat_ru}/{obj_name}"
        if fname == "ObjectModule.bsl":
            return module_node_payload(
                "ObjectModule",
                cat_ru,
                obj_name,
                owner_qn,
                owner_label="MetadataObject",
                ru_module_name="Модуль объекта",
                needs_module_node=True,
            )
        if fname == "ManagerModule.bsl":
            return module_node_payload(
                "ManagerModule",
                cat_ru,
                obj_name,
                owner_qn,
                owner_label="MetadataObject",
                ru_module_name="Модуль менеджера",
                needs_module_node=True,
            )
        if fname == "ValueManagerModule.bsl":
            return module_node_payload(
                "ValueManagerModule",
                cat_ru,
                obj_name,
                owner_qn,
                owner_label="MetadataObject",
                ru_module_name="Модуль менеджера значения",
                needs_module_node=True,
            )
        if fname == "RecordSetModule.bsl":
            return module_node_payload(
                "RecordSetModule",
                cat_ru,
                obj_name,
                owner_qn,
                owner_label="MetadataObject",
                ru_module_name="Модуль набора записей",
                needs_module_node=True,
            )
        # Some categories (e.g., Reports, DataProcessors) use single Module.bsl as object module
        if fname.lower() == "module.bsl":
            return module_node_payload(
                "ObjectModule",
                cat_ru,
                obj_name,
                owner_qn,
                owner_label="MetadataObject",
                ru_module_name="Модуль объекта",
                needs_module_node=True,
            )

    # Configuration-level: Ext/<ModuleName>.bsl
    if len(parts) >= 2 and parts[0] == "Ext" and fname.lower().endswith(".bsl"):
        name_wo_ext = fname[:-4]
        owner_qn = f"{project_name}/{config_name}"
        ru_name_map = {
            "ExternalConnectionModule.bsl": "Модуль внешнего соединения",
            "ManagedApplicationModule.bsl": "Модуль управляемого приложения",
            "SessionModule.bsl": "Модуль сеанса",
            "OrdinaryApplicationModule.bsl": "Модуль обычного приложения",
        }
        ru_name = ru_name_map.get(fname, "Модуль конфигурации")
        return module_node_payload("ConfigurationModule", "Конфигурация", name_wo_ext, owner_qn, owner_label="Configuration", ru_module_name=ru_name, needs_module_node=True)

    return None


def scan_bsl_from_form_bin(
    code_text: str,
    file_path: Path,
    code_root: Path,
    project_name: str,
    config_name: str
) -> Optional[Dict[str, Any]]:
    """
    Parse BSL code extracted from Form.bin file.
    This function takes pre-extracted code text and processes it like a .bsl file.

    Args:
        code_text: Extracted BSL code from Form.bin
        file_path: Path to the Form.bin file (for classification and IDs)
        code_root: Root directory of the code
        project_name: Project name
        config_name: Configuration name

    Returns:
        Same structure as scan_bsl_file: {kind, module, routines, declares, common_declares, callsites}
    """
    cls = _classify_module(code_root, file_path, project_name, config_name)
    if not cls:
        return None

    # Use the provided code text instead of reading from file
    text = code_text
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Continue with the same parsing logic as scan_bsl_file
    # (The rest of the function body is identical to scan_bsl_file, starting from line normalization)
    return _scan_bsl_text_impl(text, file_path, code_root, project_name, config_name, cls)


def scan_bsl_file(file_path: Path, code_root: Path, project_name: str, config_name: str) -> Optional[Dict[str, Any]]:
    """
    Read .bsl file fully, run a single-pass state machine to extract routine headers:
      - routine_type: Procedure | Function
      - name
      - export: bool
      - params_text: str
      - params_json: list of {name, default_present, default_text, markers_raw[], raw}
      - directive: str of &Directive immediately preceding the header (e.g., "&НаКлиенте")
      - signature: str full routine signature (e.g., "Процедура ПриОткрытии(Отказ) Экспорт")
      - file_path: original path (posix)
      - line: 1-based line number of header start
    Also classify module and compute:
      - For non-CommonModules: return a Module record and (module_id,routine_id) declares
      - For CommonModules: NO Module record; returns common_declares as (owner_qn,routine_id)
    """
    cls = _classify_module(code_root, file_path, project_name, config_name)
    if not cls:
        return None

    text = _read_text(file_path)
    # Normalize line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    return _scan_bsl_text_impl(text, file_path, code_root, project_name, config_name, cls)


def _decode_bsl_bytes(data: bytes) -> str:
    """Декодировать уже прочитанные bytes как BSL текст (нормализованные строки).

    Логика идентична `_read_text`, но без `path.read_bytes()` — для parse-only
    pipeline, где worker уже прочитал файл для sha256.
    """
    # BOM-based fast paths
    if data.startswith(b"\xef\xbb\xbf"):
        text = data.decode("utf-8-sig")
    elif data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16")
    else:
        text = None
        for enc in ("utf-8", "cp1251", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be"):
            try:
                s = data.decode(enc)
                if (b"\r" in data or b"\n" in data) and ("\r" not in s and "\n" not in s):
                    continue
                text = s
                break
            except UnicodeError:
                continue
        if text is None:
            text = data.decode("utf-8", errors="ignore")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_bsl_from_bytes(
    data: bytes,
    file_path: Path,
    code_root: Path,
    project_name: str,
    config_name: str,
) -> Optional[Dict[str, Any]]:
    """Parse-only entrypoint для incremental: принимает уже прочитанные bytes
    `.bsl` файла, возвращает тот же payload, что и `scan_bsl_file`.

    Используется `parse_bsl_files_parallel` workers: worker один раз читает файл
    (для sha256), затем парсит из тех же bytes, без второго `open(path).read()`.
    """
    cls = _classify_module(code_root, file_path, project_name, config_name)
    if not cls:
        return None
    text = _decode_bsl_bytes(data)
    return _scan_bsl_text_impl(text, file_path, code_root, project_name, config_name, cls)


def _scan_bsl_text_impl(
    text: str,
    file_path: Path,
    code_root: Path,
    project_name: str,
    config_name: str,
    cls: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Internal implementation for scanning BSL text.
    Shared by both scan_bsl_file and scan_bsl_from_form_bin.
    """
    # Normalize line endings to a single '\n' to make line-start/line-end checks robust
    # Handles CRLF and CR-only files. This also stabilizes line counting.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    n = len(text)

    file_path_posix = _scanner_posix_file_path(file_path, code_root)

    # Local helpers for documentation extraction from adjacent comments
    def _lstrip_spaces_invis_line(s: str) -> str:
        if s is None:
            return ""
        i2 = 0
        L = len(s)
        while i2 < L and _is_space_or_invisible(s[i2]):
            i2 += 1
        return s[i2:]

    def _is_attribute_line(s: str) -> bool:
        return _lstrip_spaces_invis_line(s).startswith("&")

    def _extract_adjacent_comment_block(src: str, header_start: int) -> str:
        # НОВЫЙ, НАДЕЖНЫЙ АЛГОРИТМ, проверенный в test.py
        if header_start <= 0:
            return ""
    
        # Разбиваем весь текст на строки один раз. Это надежнее, чем rfind.
        lines = src.split('\n')
    
        # Находим номер строки, где начинается заголовок
        line_num = src.count('\n', 0, header_start)
    
        # Двигаемся вверх от строки с заголовком
        current_line_idx = line_num - 1
    
        # Пропускаем ВСЕ строки с атрибутами (&)
        while current_line_idx >= 0 and _is_attribute_line(lines[current_line_idx]):
            current_line_idx -= 1

        # Собираем строки комментариев
        collected = []
        while current_line_idx >= 0:
            line = lines[current_line_idx]
            stripped_line = _lstrip_spaces_invis_line(line)
        
            # Если строка пустая (не коммментарий), останавливаемся
            if not stripped_line:
                break
            # Если это не комментарий, останавливаемся
            if not stripped_line.startswith("//"):
                break
            
            collected.append(line)
            current_line_idx -= 1
        
        if not collected:
            return ""

        collected.reverse() # Восстанавливаем порядок
    
        # Очищаем собранные комментарии от "//" и лишних пробелов
        cleaned = []
        for raw_line in collected:
            s = _lstrip_spaces_invis_line(raw_line)
            s = s[2:] # Убираем "//"
            if s.startswith(" "):
                s = s[1:]
            cleaned.append(s.rstrip())
        
        return "\n".join(cleaned)

    def _split_doc_sections(comment_text: str) -> Tuple[str, str, str]:
        if not comment_text:
            return "", "", ""
        lines = comment_text.split("\n")
        idx_params = -1
        idx_return = -1
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s == "Параметры:" and idx_params == -1:
                idx_params = i
            elif s == "Возвращаемое значение:" and idx_return == -1:
                idx_return = i
        if idx_params == -1 and idx_return == -1:
            return comment_text.strip(), "", ""
        first_marker = min(x for x in [idx_params, idx_return] if x != -1)
        desc = "\n".join(lines[:first_marker]).strip()
        params_text = ""
        if idx_params != -1:
            end = idx_return if (idx_return != -1 and idx_return > idx_params) else len(lines)
            params_text = "\n".join(lines[idx_params + 1:end]).rstrip()
        return_text = ""
        if idx_return != -1:
            return_text = "\n".join(lines[idx_return + 1:]).rstrip()
        return desc, params_text, return_text

    # State flags
    in_line_comment = False
    in_block_comment = False
    in_string = False

    # Area tracking
    area_stack: List[str] = []  # стек вложенных областей
    current_area_path: Optional[str] = None  # полный путь текущей области

    def newline_count_until(idx: int) -> int:
        # Compute line number cheaply for modest headers (few per file)
        return text.count("\n", 0, idx)

    routines: List[Dict[str, Any]] = []
    hdr_spans: List[Dict[str, Any]] = []
    pending_directives: List[str] = []
    i = 0
    while i < n:
        ch = text[i]

        # Track end of line-comment
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue

        # Track end of block-comment
        if in_block_comment:
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                in_block_comment = False
                i += 2
            else:
                i += 1
            continue

        # Inside string
        if in_string:
            if ch == '"':
                # "" escapes a quote
                if i + 1 < n and text[i + 1] == '"':
                    i += 2
                    continue
                else:
                    in_string = False
                    i += 1
                    continue
            i += 1
            continue

        # DEFAULT state: handle comment starts
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":
                in_line_comment = True
                i += 2
                continue
            if nxt == "*":
                in_block_comment = True
                i += 2
                continue

        # Preprocessor directives: lines beginning with '#'
        # Detect if at line start (previous char is \n or i==0) and optional whitespace before '#'
        if _is_space_or_invisible(ch) and (i == 0 or text[i - 1] == "\n"):
            # skip leading blanks/invisible; peek ahead for '#'
            j = i
            while j < n and _is_space_or_invisible(text[j]):
                j += 1
            if j < n and text[j] == "#":
                # parse directive (support leading spaces)
                j2 = j + 1
                while j2 < n and text[j2] not in ("\n", " "):
                    j2 += 1
                directive = text[j+1:j2].strip()
                if directive == "Область" or directive == "Region":
                    # Extract area name
                    k = j2
                    while k < n and text[k] not in ("\n"):
                        k += 1
                    area_name = text[j2:k].strip()
                    area_stack.append(area_name)
                    current_area_path = ".".join(area_stack)
                    i = k
                    continue
                elif directive == "КонецОбласти" or directive == "EndRegion":
                    if area_stack:
                        area_stack.pop()
                    current_area_path = ".".join(area_stack) if area_stack else None
                    i = j2
                    while i < n and text[i] != "\n":
                        i += 1
                    continue
                else:
                    # skip to end of line
                    while j < n and text[j] != "\n":
                        j += 1
                    i = j
                    continue

        if ch == "#" and (i == 0 or text[i - 1] == "\n"):
            # Check for area directives
            j = i + 1
            while j < n and text[j] not in ("\n", " "):
                j += 1
            
            directive = text[i+1:j].strip()
            if directive == "Область" or directive == "Region":
                # Extract area name
                k = j
                while k < n and text[k] not in ("\n"):
                    k += 1
                area_name = text[j:k].strip()
                area_stack.append(area_name)
                current_area_path = ".".join(area_stack)
                i = k
                continue
            elif directive == "КонецОбласти" or directive == "EndRegion":
                if area_stack:
                    area_stack.pop()
                current_area_path = ".".join(area_stack) if area_stack else None
                i = j
                while i < n and text[i] != "\n":
                    i += 1
                continue
            else:
                # skip other directive lines
                while i < n and text[i] != "\n":
                    i += 1
                continue

        # Attributes: lines starting with '&' (allow leading spaces/tabs)
        if _is_space_or_invisible(ch) and (i == 0 or text[i - 1] == "\n"):
            # possible attribute with leading blanks/invisible
            j = i
            while j < n and _is_space_or_invisible(text[j]):
                j += 1
            if j < n and text[j] == "&":
                # collect the line as attribute
                k = j
                while k < n and text[k] != "\n":
                    k += 1
                raw_attr = text[j:k]
                # Trim trailing inline comments on attribute line
                cpos = raw_attr.find("//")
                if cpos != -1:
                    raw_attr = raw_attr[:cpos]
                bpos = raw_attr.find("/*")
                if bpos != -1:
                    raw_attr = raw_attr[:bpos]
                attr_line = raw_attr.strip()
                if attr_line:
                    pending_directives.append(attr_line)
                i = k
                continue

        if ch == "&" and (i == 0 or text[i - 1] == "\n"):
            # attribute at line start
            k = i
            while k < n and text[k] != "\n":
                k += 1
            raw_attr = text[i:k]
            # Trim trailing inline comments on attribute line
            cpos = raw_attr.find("//")
            if cpos != -1:
                raw_attr = raw_attr[:cpos]
            bpos = raw_attr.find("/*")
            if bpos != -1:
                raw_attr = raw_attr[:bpos]
            attr_line = raw_attr.strip()
            if attr_line:
                pending_directives.append(attr_line)
            i = k
            continue

        # Strings
        if ch == '"':
            in_string = True
            i += 1
            continue

        # Look for routine keywords outside of comments/strings (with safety probe skipping invisible leaders)
        kw_idx = i
        at_line_start = (i == 0) or (text[i - 1] == "\n")
        if at_line_start:
            # skip leading spaces/invisibles on the line
            while kw_idx < n and _is_space_or_invisible(text[kw_idx]):
                kw_idx += 1
        is_proc = False
        is_func = False
        if at_line_start:
            is_proc = _is_keyword_at(text, kw_idx, "Процедура") or _is_keyword_at(text, kw_idx, "Procedure")
            is_func = _is_keyword_at(text, kw_idx, "Функция") or _is_keyword_at(text, kw_idx, "Function")
        if is_proc or is_func:
            routine_type = "Procedure" if is_proc else "Function"
            start_idx = kw_idx
            # Accumulate header buf until ')' and optional 'Экспорт/Export' (outside strings/comments)
            header_buf: List[str] = []
            j = kw_idx
            # Advance to end-of-header
            paren_level = 0
            seen_open = False
            in_hdr_string = False
            in_hdr_line_comment = False
            in_hdr_block_comment = False
            header_export = False
            while j < n:
                c = text[j]
                if in_hdr_line_comment:
                    if c == "\n":
                        in_hdr_line_comment = False
                    header_buf.append(c)
                    j += 1
                    continue
                if in_hdr_block_comment:
                    if c == "*" and j + 1 < n and text[j + 1] == "/":
                        in_hdr_block_comment = False
                        header_buf.append("*/")
                        j += 2
                    else:
                        header_buf.append(c)
                        j += 1
                    continue
                if in_hdr_string:
                    if c == '"':
                        if j + 1 < n and text[j + 1] == '"':
                            header_buf.append('""')
                            j += 2
                            continue
                        else:
                            in_hdr_string = False
                            header_buf.append(c)
                            j += 1
                            continue
                    header_buf.append(c)
                    j += 1
                    continue

                # header default state
                if c == "/" and j + 1 < n:
                    nxt = text[j + 1]
                    if nxt == "/":
                        in_hdr_line_comment = True
                        header_buf.append("//")
                        j += 2
                        continue
                    if nxt == "*":
                        in_hdr_block_comment = True
                        header_buf.append("/*")
                        j += 2
                        continue

                if c == '"':
                    in_hdr_string = True
                    header_buf.append(c)
                    j += 1
                    continue

                if c == "(":
                    paren_level += 1
                    seen_open = True
                    header_buf.append(c)
                    j += 1
                    continue
                if c == ")":
                    if paren_level > 0:
                        paren_level -= 1
                    header_buf.append(c)
                    j += 1
                    # If we closed the first (only) level, look ahead for optional Export/Экспорт and then stop at eol
                    if seen_open and paren_level == 0:
                        # Consume spaces
                        k = j
                        while k < n and text[k] in (" ", "\t"):
                            header_buf.append(text[k])
                            k += 1
                        # optional Export token
                        if k < n:
                            if _is_keyword_at(text, k, "Экспорт"):
                                header_buf.append("Экспорт")
                                k += len("Экспорт")
                                header_export = True
                            elif _is_keyword_at(text, k, "Export"):
                                header_buf.append("Export")
                                k += len("Export")
                                header_export = True
                        # Consume until end of line (do not include body)
                        while k < n and text[k] != "\n":
                            # but stop if another header begins (unlikely on same line)
                            header_buf.append(text[k])
                            k += 1
                        j = k
                        break
                    continue

                header_buf.append(c)
                j += 1

            header_str = "".join(header_buf)
            name, params_slice = _extract_name_and_params(header_str)
            params_items = _split_params(params_slice)
            params_json = [_parse_param_item(p) for p in params_items]
            params_text = params_slice.strip()
            # export captured during header scan
            export = bool(header_export)
            # line number
            start_line = newline_count_until(start_idx) + 1

            # Extract adjacent documentation and split by fixed markers
            try:
                comment_block = _extract_adjacent_comment_block(text, start_idx)
            except Exception:
                comment_block = ""
            try:
                doc_description, doc_params_text, doc_return_text = _split_doc_sections(comment_block)
            except Exception:
                doc_description, doc_params_text, doc_return_text = "", "", ""
            if routine_type == "Procedure":
                doc_return_text = ""

            # Build routine
            _dec = parse_extension_decorator(pending_directives)
            routine: Dict[str, Any] = {
                "routine_type": routine_type,
                "name": name,
                "export": bool(export),
                "params_text": params_text,
                "params_json": params_json,
                "directives": pending_directives.copy(),
                "decorator_type":   _dec[0] if _dec else "",
                "decorator_target": _dec[1] if _dec else "",
                "signature": header_str,
                "file_path": file_path_posix,
                "line": start_line,
                "project_name": project_name,
                "config_name": config_name,
                "area_path": current_area_path,  # Добавляем путь области
                "doc_description": doc_description,
                "doc_params_text": doc_params_text,
                "doc_return_text": doc_return_text,
                # body will be filled later after all headers are collected
                "body": "",
                # body_hash is recomputed once body is set (sha256 hex of the body string)
                "body_hash": "",
            }
            routines.append(routine)
            try:
                hdr_spans.append({
                    "routine_type": routine_type,
                    "name": name,
                    "params_text": params_text,
                    "header_start": start_idx,
                    "header_end": j,
                    "line": start_line,
                })
            except Exception:
                pass
            # Directives consumed
            pending_directives = []
            # Continue from j (end-of-header)
            i = j
            continue

        # If a non-attribute, non-comment, non-directive content line intervenes before a header,
        # clear any pending_attrs (they belong only to nearest following header block).
        if (i == 0 or text[i - 1] == "\n"):
            # Skip blanks/invisibles to inspect the first significant char of the line
            k = i
            while k < n and _is_space_or_invisible(text[k]):
                k += 1
            if k < n and text[k] not in ("\n", "&", "#", "/"):
                # keep directives if the line actually starts with a header keyword
                if not (_is_keyword_at(text, k, "Процедура") or _is_keyword_at(text, k, "Procedure") or
                        _is_keyword_at(text, k, "Функция") or _is_keyword_at(text, k, "Function")):
                    pending_directives = []

        i += 1

    # Extract full body text for each routine (from header_start to КонецПроцедуры/КонецФункции)
    # Use state machine to skip comments and strings
    try:
        if hdr_spans and routines:
            sorted_spans = sorted(hdr_spans, key=lambda s: s.get("header_start", 0))
            for idx, sp in enumerate(sorted_spans):
                # Find corresponding routine by name and params
                routine_key = (sp.get("routine_type", ""), sp.get("name", ""), sp.get("params_text", "") or "")
                matching_routine = None
                for r in routines:
                    r_key = (r.get("routine_type", ""), r.get("name", ""), r.get("params_text", "") or "")
                    if r_key == routine_key:
                        matching_routine = r
                        break

                if matching_routine:
                    body_start = int(sp.get("header_start", 0) or 0)
                    routine_type = sp.get("routine_type", "Procedure")

                    # Find the matching end keyword: КонецПроцедуры/EndProcedure or КонецФункции/EndFunction
                    end_keywords = []
                    if routine_type == "Procedure":
                        end_keywords = ["КонецПроцедуры", "EndProcedure"]
                    else:
                        end_keywords = ["КонецФункции", "EndFunction"]

                    # Search for end keyword starting from header_end, respecting comment/string context
                    header_end_pos = int(sp.get("header_end", body_start))
                    body_end = len(text)  # default: end of file

                    # Limit search to next routine start if available
                    search_limit = int(sorted_spans[idx + 1].get("header_start", len(text))) if (idx + 1) < len(sorted_spans) else len(text)

                    # State machine to skip comments and strings
                    pos = header_end_pos
                    in_line_comment = False
                    in_block_comment = False
                    in_string = False

                    while pos < search_limit and body_end == len(text):
                        ch = text[pos]

                        # Track line comment state
                        if in_line_comment:
                            if ch == "\n":
                                in_line_comment = False
                            pos += 1
                            continue

                        # Track block comment state
                        if in_block_comment:
                            if ch == "*" and pos + 1 < search_limit and text[pos + 1] == "/":
                                in_block_comment = False
                                pos += 2
                            else:
                                pos += 1
                            continue

                        # Track string state
                        if in_string:
                            if ch == '"':
                                if pos + 1 < search_limit and text[pos + 1] == '"':
                                    pos += 2
                                    continue
                                else:
                                    in_string = False
                                    pos += 1
                                    continue
                            pos += 1
                            continue

                        # DEFAULT state: detect comment/string starts
                        if ch == "/" and pos + 1 < search_limit:
                            nxt = text[pos + 1]
                            if nxt == "/":
                                in_line_comment = True
                                pos += 2
                                continue
                            if nxt == "*":
                                in_block_comment = True
                                pos += 2
                                continue

                        if ch == '"':
                            in_string = True
                            pos += 1
                            continue

                        # Check for end keywords (only in default state, outside comments/strings)
                        for end_kw in end_keywords:
                            if _is_keyword_at(text, pos, end_kw):
                                # Found the end keyword; include it in the body
                                body_end = pos + len(end_kw)
                                break

                        if body_end != len(text):
                            break

                        pos += 1

                    if body_start < body_end:
                        body_text = text[body_start:body_end].strip()
                        matching_routine["body"] = body_text
                        # sha256 hex digest is used by the BSL code search sidecar
                        # to detect stale fragments (Routine.body changed without reindex).
                        # Empty body -> empty hash so loaders can write '' unconditionally.
                        matching_routine["body_hash"] = (
                            hashlib.sha256(body_text.encode("utf-8")).hexdigest()
                            if body_text
                            else ""
                        )
    except Exception as e:
        logger.warning("Failed to extract routine bodies: %s", e)

    # Per-routine state hashes: doc_hash, signature_hash, routine_state_hash.
    # Используются routine-level incremental diff (см. bsl_routine_delta) для
    # выявления того, что именно изменилось в процедуре — тело, doc-комментарий
    # или сигнатура. routine_state_hash включает line/file_path, поэтому
    # line-only сдвиг тоже отлавливается без false-negatives, но не влияет на
    # body/doc/signature ветки classification.
    for r in routines:
        r["doc_hash"] = _compute_routine_doc_hash(r)
        r["doc_description_embedding_hash"] = _compute_routine_doc_description_embedding_hash(r)
        r["signature_hash"] = _compute_routine_signature_hash(r)
        r["routine_state_hash"] = _compute_routine_state_hash(r)

    # IDs and declares
    module_path_posix = file_path_posix

    res_module: Optional[Dict[str, Any]] = None
    declares: List[Dict[str, Any]] = []
    common_declares: List[Dict[str, Any]] = []

    if cls["needs_module_node"]:
        module_id = cls["module_id"]
        res_module = {
            "id": module_id,
            "path": module_path_posix,
            "module_type": cls["module_type"],
            "owner_kind": cls["owner_kind"],
            "owner_name": cls["owner_name"],
            "owner_qn": cls["owner_qn"],
            "owner_label": cls["owner_label"],  # Form | MetadataObject | Configuration
            "name": cls.get("ru_module_name", ""),
            "project_name": project_name,
            "config_name": config_name,
        }
        # compute routine ids using module_id
        for r in routines:
            rid = _sha1(f"{module_id}|{r['routine_type']}|{r['name']}|{r['params_text']}")
            r["id"] = rid
            r["module_id"] = module_id
            r["owner_qn"] = cls["owner_qn"]
            r["module_type"] = cls["module_type"]
            r["owner_category"] = owner_kind_to_owner_category(cls.get("owner_kind", ""), cls.get("owner_label", ""))
            declares.append({"module_id": module_id, "routine_id": rid})
    else:
        # CommonModule: owner is the module; DECLARES from owner to routine
        for r in routines:
            rid = _sha1(f"{cls['owner_qn']}|{r['routine_type']}|{r['name']}|{r['params_text']}")
            r["id"] = rid
            r["module_id"] = None
            r["owner_qn"] = cls["owner_qn"]
            r["module_type"] = cls["module_type"]
            r["owner_category"] = owner_kind_to_owner_category(cls.get("owner_kind", ""), cls.get("owner_label", ""))
            common_declares.append({"owner_qn": cls["owner_qn"], "routine_id": rid})

    # Diagnostics: if this is a FormModule and no routines were parsed, info message for visibility
    try:
        if cls and cls.get("needs_module_node") and cls.get("module_type") == "FormModule" and not routines:
            logger.info("FormModule with zero routines parsed: %s", module_path_posix)
    except Exception:
        pass

    # Phase 1: collect call-sites inside routine bodies (direct + qualified)
    # Build (routine_type, name, params_text) -> rid map
    rid_by_key: Dict[Tuple[str, str, str], str] = {}
    try:
        for rr in routines or []:
            k = (rr.get("routine_type",""), rr.get("name",""), rr.get("params_text","") or "")
            rid_by_key[k] = rr.get("id")
    except Exception:
        pass

    callsites: List[Dict[str, Any]] = []

    def _read_ident_at(p: int, end: int) -> Tuple[str, int]:
        j = p
        while j < end:
            ch = text[j]
            if ch.isalpha() or ch == "_" or ch.isdigit():
                j += 1
                continue
            break
        return (text[p:j], j)

    def _skip_ws_invis(p: int, end: int) -> int:
        j = p
        while j < end and _is_space_or_invisible(text[j]):
            j += 1
        return j

    def _line_no_at(p: int) -> int:
        try:
            return text.count("\n", 0, p) + 1
        except Exception:
            return 0

    # Construct body segments between header_end and next header_start
    try:
        spans = sorted(hdr_spans or [], key=lambda s: s.get("header_start", 0))
        for idx, sp in enumerate(spans):
            key = (sp.get("routine_type",""), sp.get("name",""), sp.get("params_text","") or "")
            caller_rid = rid_by_key.get(key)
            if not caller_rid:
                continue
            seg_start = int(sp.get("header_end", 0) or 0)
            seg_end = int(spans[idx + 1].get("header_start", len(text))) if (idx + 1) < len(spans) else len(text)
            if seg_start >= seg_end:
                continue

            in_line_comment = False
            in_block_comment = False
            in_string = False

            i2 = seg_start
            while i2 < seg_end:
                ch = text[i2]

                # End of line comment
                if in_line_comment:
                    if ch == "\n":
                        in_line_comment = False
                    i2 += 1
                    continue

                # End of block comment
                if in_block_comment:
                    if ch == "*" and i2 + 1 < seg_end and text[i2 + 1] == "/":
                        in_block_comment = False
                        i2 += 2
                    else:
                        i2 += 1
                    continue

                # Inside string
                if in_string:
                    if ch == '"':
                        if i2 + 1 < seg_end and text[i2 + 1] == '"':
                            i2 += 2
                            continue
                        else:
                            in_string = False
                            i2 += 1
                            continue
                    i2 += 1
                    continue

                # Possible comment starts
                if ch == "/" and i2 + 1 < seg_end:
                    nxt = text[i2 + 1]
                    if nxt == "/":
                        in_line_comment = True
                        i2 += 2
                        continue
                    if nxt == "*":
                        in_block_comment = True
                        i2 += 2
                        continue

                # String start
                if ch == '"':
                    in_string = True
                    i2 += 1
                    continue

                # Identifier: possible call
                if _is_letter(ch):
                    ident1, j2 = _read_ident_at(i2, seg_end)
                    low_ident1 = _casefold(ident1)
                    k2 = _skip_ws_invis(j2, seg_end)

                    # Phase 2: dynamic via Выполнить/Вычислить (and ENG synonyms Execute/Evaluate)
                    if low_ident1 in ("выполнить", "вычислить", "execute", "evaluate"):
                        if k2 < seg_end and text[k2] == "(":
                            k3 = _skip_ws_invis(k2 + 1, seg_end)
                            if k3 < seg_end and k3 < seg_end and text[k3] == '"':
                                lit, after_lit = _read_string_literal_value(text, k3, seg_end)
                                if lit is not None:
                                    q, nm, ac = _parse_inner_call_from_string(lit)
                                    if nm:
                                        callsites.append({
                                            "caller_id": caller_rid,
                                            "kind": "dynamic",
                                            "qualifier": q,
                                            "name_literal": nm,
                                            "args_count": ac,
                                            "line": _line_no_at(i2),
                                            "source": ident1,
                                        })
                                    # advance to after literal to avoid rescanning inside
                                    i2 = after_lit
                                    continue
                        # fallthrough to generic handling if pattern not matched

                    # Phase 2: notification via ОписаниеОповещения / NotificationDescription
                    if low_ident1 in ("описаниеоповещения", "notificationdescription"):
                        if k2 < seg_end and text[k2] == "(":
                            # first argument should be a string literal with method name
                            k3 = _skip_ws_invis(k2 + 1, seg_end)
                            if k3 < seg_end and text[k3] == '"':
                                lit, after_lit = _read_string_literal_value(text, k3, seg_end)
                                # probe for second argument == ЭтотОбъект/ThisObject
                                is_thisobj = False
                                j3 = _skip_ws_invis(after_lit, seg_end)
                                if j3 < seg_end and text[j3] == ",":
                                    j3 = _skip_ws_invis(j3 + 1, seg_end)
                                    # read simple identifier token
                                    if j3 < seg_end and _is_letter(text[j3]):
                                        tok, j4 = _read_ident_at(j3, seg_end)
                                        if _is_thisobject_token(tok):
                                            is_thisobj = True
                                if lit:
                                    callsites.append({
                                        "caller_id": caller_rid,
                                        "kind": "notification",
                                        "qualifier": "ЭтотОбъект" if is_thisobj else None,
                                        "name_literal": lit,
                                        "args_count": None,
                                        "line": _line_no_at(i2),
                                        "source": ident1,
                                    })
                                    i2 = after_lit
                                    continue
                        # fallthrough

                    # Qualified call: chain Ident1.Ident2[.IdentN]* (
                    if k2 < seg_end and text[k2] == ".":
                        peek = _skip_ws_invis(k2 + 1, seg_end)
                        if peek < seg_end and _is_letter(text[peek]):
                            chain_parts: List[str] = [ident1]
                            chain_end = j2  # end-of-last-ident position
                            p = k2
                            max_chain = 8
                            while len(chain_parts) < max_chain:
                                if p >= seg_end or text[p] != ".":
                                    break
                                p2 = _skip_ws_invis(p + 1, seg_end)
                                if p2 >= seg_end or not _is_letter(text[p2]):
                                    break
                                ident_seg, after_ident = _read_ident_at(p2, seg_end)
                                chain_parts.append(ident_seg)
                                chain_end = after_ident
                                p = _skip_ws_invis(after_ident, seg_end)
                            if p < seg_end and text[p] == "(":
                                qualifier_parts = chain_parts[:-1]
                                callsites.append({
                                    "caller_id": caller_rid,
                                    "kind": "qualified",
                                    "qualifier": ".".join(qualifier_parts),
                                    "qualifier_parts": qualifier_parts,
                                    "name": chain_parts[-1],
                                    "line": _line_no_at(i2),
                                })
                                i2 = p + 1
                                continue
                            # No '(' at the end — skip past the whole chain so we
                            # don't generate a phantom inner B.C qualified call on
                            # subsequent iterations.
                            i2 = chain_end
                            continue
                    # Direct call: Ident1 (
                    if k2 < seg_end and text[k2] == "(":
                        callsites.append({
                            "caller_id": caller_rid,
                            "kind": "direct",
                            "qualifier": None,
                            "name": ident1,
                            "line": _line_no_at(i2),
                        })
                        i2 = k2 + 1
                        continue

                    i2 = j2
                    continue

                i2 += 1
    except Exception as _scan_e:
        # Be conservative: skip callsite extraction errors
        pass

    return {
        "kind": "bsl",
        "module": res_module,
        "routines": routines,
        "declares": declares,
        "common_declares": common_declares,
        "callsites": callsites,
    }