"""Compact BSL fact extraction.

Pure regex over `routine.body` — no tree-sitter, no semantic classification.
Behaviour is inferred only from textual evidence (assignments, writes, reads,
external channels), never from procedure-name heuristics.

The output is a single semicolon-separated string of facts that the LLM
treats as behaviour signals. Recognised facts:

  Отказ                  Отказ = Истина
  Сообщить               Сообщить/СообщитьПользователю/СообщениеПользователю
  Исключение             ВызватьИсключение
  Движения.<X>           reference to a register name
  set:<Поле>             field assignment on the object
  Очистить:<Поле>        collection clear on the object field
  Добавить:<Поле>        collection add on the object field
  Записать:<Имя>         object/set/manager Write call
  Записать:Движения.<X>  Write call for a specific register set
  Записывать:Движения.<X> Записывать = Истина switch on a register set
  Провести:<X>           Write with РежимЗаписиДокумента.Проведение
  ОтменитьПроведение:<X> Write with ОтменаПроведения
  Прочитать:<X>          Read / Прочитать
  Удалить:<X>            Delete / Удалить
  ПометкаУдаления:<X>    УстановитьПометкуУдаления
  Загрузить:<X>          Load
  Выгрузить:<X>          Unload
  Найти:<X>              FindByCode/FindByDescription
  Получить:<X>           Get/Retrieve
  Установить:<X>         Set
  НаборЗаписей:<reg>     Регистры*.<X>.СоздатьНаборЗаписей()
  МенеджерЗаписи:<reg>   Регистры*.<X>.СоздатьМенеджерЗаписи()
  Запрос:<источник>      ИЗ/FROM/ПОМЕСТИТЬ/JOIN ... <Категория.Имя>
  >Имя                   prioritised call to another routine
  HTTP / COM / Файл /    external channels (HTTPСоединение, COMОбъект,
  WebСервис              ТекстовыйДокумент, WSОпределения, ...)
"""

from __future__ import annotations

import re
from typing import List, Optional

_BSL_FACT_LIMIT = 6
_BSL_RESULT_CHAR_LIMIT = 260

_RE_OBJECT_ROOT = r"(?:ЭтотОбъект|ДокументОбъект|ТекущийОбъект|Объект|СправочникОбъект|РегистрСведенийНаборЗаписей|РегистрНакопленияНаборЗаписей)"
_RE_RECEIVER = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*(?:\.[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)?"
_RE_IDENT = r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*"

_IMPORTANT_RECEIVER_RE = re.compile(
    r"(Документы|Справочники|Регистры|Перечисления|БизнесПроцессы|Задачи|"
    r"Зарплата|Расчет|Проведение|Учет|"
    r"ОбщегоНазначения|Исправление|Перерасчет|ПрямыеВыплаты|ДатыЗапрета)",
    re.IGNORECASE,
)

_SKIPPED_RECEIVERS = {
    "ЭтотОбъект",
    "ДокументОбъект",
    "ТекущийОбъект",
    "Объект",
    "Строка",
    "Описание",
    "Поля",
    "Массив",
    "ПроверяемыеПоля",
    "Истина",
    "Ложь",
    "Неопределено",
}

_EXTERNAL_PATTERNS = (
    ("HTTP", re.compile(r"\bHTTP(?:Соединение|Запрос|Ответ)\b", re.IGNORECASE)),
    ("Файл", re.compile(r"\b(ТекстовыйДокумент|Файл|ЧтениеТекста|ЗаписьТекста|ЧтениеXML|ЗаписьXML|XMLЧтение|XMLЗапись)\b", re.IGNORECASE)),
    ("COM", re.compile(r"\bCOMОбъект\b", re.IGNORECASE)),
    ("WebСервис", re.compile(r"\b(WS(?:Определения|Прокси)|WebСервис|HTTPСервис)\b", re.IGNORECASE)),
)


def _short_call(call: str) -> str:
    """Compact dotted call to the last meaningful segment(s)."""
    parts = [p.strip() for p in call.split(".") if p.strip()]
    if not parts:
        return ""
    if len(parts) >= 3 and parts[0] in {"Документы", "Справочники", "Регистры", "Перечисления"}:
        return f"{parts[-2]}.{parts[-1]}"
    return parts[-1]


def _call_priority(call: str) -> int:
    name = _short_call(call).lower()
    if "сформироватьдвижения" in name:
        return 0
    if "провестипоучетам" in name:
        return 1
    if "подготовитьнаборызаписей" in name:
        return 2
    if "восстановитьперерасчеты" in name or "перерасчет" in name:
        return 3
    if name.startswith("проверить"):
        return 4
    if name.startswith("заполнить"):
        return 5
    return 9


def extract_bsl_facts(body: Optional[str]) -> str:
    """Return a `;`-joined fact string for a BSL routine body, or empty string."""
    if not body:
        return ""
    text = str(body)
    if not text.strip():
        return ""

    facts: List[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        if not value or value in seen:
            return
        seen.add(value)
        facts.append(value)

    # Отказ / Сообщить / Исключение
    if re.search(r"\bОтказ\s*=\s*Истина\b", text):
        add("Отказ")
    elif re.search(r"\bОтказ\s*=", text):
        add("Отказ")
    if re.search(r"\b(СообщитьПользователю|СообщениеПользователю|Сообщить)\b", text):
        add("Сообщить")
    if re.search(r"\b(ВызватьИсключение|Исключение)\b", text):
        add("Исключение")

    # Движения.<X>
    for name in re.findall(r"\bДвижения\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)", text):
        add(f"Движения.{name}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Прямые присваивания полям объекта: Объект.<Поле> = ...
    for match in re.finditer(
        rf"\b{_RE_OBJECT_ROOT}\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\s*=",
        text,
    ):
        add(f"set:{match.group(1)}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Очистить/Добавить на поле объекта
    for match in re.finditer(
        rf"\b{_RE_OBJECT_ROOT}\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\.(Очистить|Добавить)\(",
        text,
    ):
        add(f"{match.group(2)}:{match.group(1)}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Движения.<X>.Записать()
    for match in re.finditer(
        r"\bДвижения\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\.Записать\(",
        text,
    ):
        add(f"Записать:Движения.{match.group(1)}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Движения.<X>.Записывать = Истина
    for match in re.finditer(
        r"\bДвижения\.([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\.Записывать\s*=\s*Истина\b",
        text,
    ):
        add(f"Записывать:Движения.{match.group(1)}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # <Получатель>.Записать(аргументы) с разделением проведения/отмены
    for match in re.finditer(
        rf"\b({_RE_RECEIVER})\.Записать\(([^)]*)\)",
        text,
    ):
        receiver = match.group(1)
        args = match.group(2) or ""
        if "РежимЗаписиДокумента.Проведение" in args:
            add(f"Провести:{receiver}")
        elif "РежимЗаписиДокумента.ОтменаПроведения" in args:
            add(f"ОтменитьПроведение:{receiver}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # <Получатель>.Записать(...)
    for match in re.finditer(
        rf"\b({_RE_RECEIVER})\.Записать\(",
        text,
    ):
        receiver = match.group(1)
        if receiver in {"ЭтотОбъект", "ДокументОбъект", "ТекущийОбъект", "Объект"}:
            add(f"Записать:{receiver}")
        elif receiver.startswith("Движения."):
            add(f"Записать:{receiver}")
        elif re.search(r"(Набор|Запис|Регистр|Движени)", receiver, flags=re.IGNORECASE):
            add(f"Записать:{receiver}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Прочитать / Удалить / ПометкаУдаления / Загрузить / Выгрузить / Найти / Получить / Установить
    for method, label in (
        ("Прочитать", "Прочитать"),
        ("Удалить", "Удалить"),
        ("УстановитьПометкуУдаления", "ПометкаУдаления"),
        ("Загрузить", "Загрузить"),
        ("Выгрузить", "Выгрузить"),
        ("НайтиПоКоду", "Найти"),
        ("НайтиПоНаименованию", "Найти"),
        ("НайтиПоРеквизиту", "Найти"),
        ("ПолучитьОбъект", "Получить"),
        ("УстановитьЗначение", "Установить"),
    ):
        for match in re.finditer(
            rf"\b({_RE_RECEIVER})\.{method}\(",
            text,
        ):
            receiver = match.group(1)
            if (
                receiver in {"ЭтотОбъект", "ДокументОбъект", "ТекущийОбъект", "Объект"}
                or re.search(r"(Набор|Запис|Регистр|Движени|Справочник|Документ|Менеджер)", receiver, flags=re.IGNORECASE)
            ):
                add(f"{label}:{receiver}")
            if len(facts) >= _BSL_FACT_LIMIT:
                break
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # НаборЗаписей / МенеджерЗаписи
    for match in re.finditer(
        rf"\b(Регистры(?:Сведений|Накопления|Бухгалтерии|Расчета)\.{_RE_IDENT})\.(СоздатьНаборЗаписей|СоздатьМенеджерЗаписи)\(",
        text,
    ):
        label = "НаборЗаписей" if match.group(2) == "СоздатьНаборЗаписей" else "МенеджерЗаписи"
        add(f"{label}:{match.group(1)}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Запросы: ИЗ / FROM / ПОМЕСТИТЬ / JOIN / СОЕДИНЕНИЕ ... <Категория.Имя>
    for source in re.findall(
        rf"\b(?:ИЗ|FROM|ПОМЕСТИТЬ|JOIN|СОЕДИНЕНИЕ)\s+({_RE_IDENT}\.{_RE_IDENT})",
        text,
        flags=re.IGNORECASE,
    ):
        add(f"Запрос:{source}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    # Внешние каналы
    for label, pattern in _EXTERNAL_PATTERNS:
        if pattern.search(text):
            add(label)
            if len(facts) >= _BSL_FACT_LIMIT:
                break

    # >Имя вызовов — отбираются по важным receiver-ам, сортируются по приоритету
    call_candidates: List[str] = []
    for call in re.findall(
        rf"\b({_RE_IDENT}\.{_RE_IDENT}\.{_RE_IDENT})\s*\(",
        text,
    ):
        if _IMPORTANT_RECEIVER_RE.search(call):
            call_candidates.append(call)
    for call in re.findall(
        rf"\b({_RE_IDENT}\.{_RE_IDENT})\s*\(",
        text,
    ):
        receiver = call.split(".", 1)[0]
        if receiver in _SKIPPED_RECEIVERS:
            continue
        if _IMPORTANT_RECEIVER_RE.search(receiver):
            call_candidates.append(call)

    # Direct unprefixed calls to local procedures whose name starts with a
    # known behaviour verb (СформироватьДвижения..., Проверить..., ВосстановитьПерерасчеты...,
    # ПодготовитьНаборыЗаписей..., ПровестиПоУчетам..., Заполнить...). Without
    # this branch a standard `ОбработкаПроведения` body like
    # `СформироватьДвижения(Отказ); ПроверитьЗаполнение(Отказ);` would yield
    # no `code` at all and the routine would be dropped from bsl_profile.
    #
    # Negative lookbehind on `.` is intentional: `Документы.X.СформироватьДвижения()`
    # is already captured by the dotted-call branch above and must not produce
    # a second `>СформироватьДвижения` fact (the dedup `seen` set works on the
    # post-`_short_call` form and would otherwise treat the two as different).
    for name in re.findall(
        r"(?<![A-Za-zА-Яа-яЁё0-9_.])"
        r"(СформироватьДвижения\w*|ПровестиПоУчетам\w*|ПодготовитьНаборыЗаписей\w*"
        r"|ВосстановитьПерерасчеты\w*|Перерасчет\w*|Проверить\w*|Заполнить\w*)\s*\(",
        text,
    ):
        call_candidates.append(name)

    for call in sorted(call_candidates, key=lambda item: (_call_priority(item), _short_call(item).lower())):
        short = _short_call(call)
        if not short:
            continue
        add(f">{short}")
        if len(facts) >= _BSL_FACT_LIMIT:
            break

    result = ";".join(facts)
    if len(result) > _BSL_RESULT_CHAR_LIMIT:
        return result[:_BSL_RESULT_CHAR_LIMIT].rstrip(";")
    return result
