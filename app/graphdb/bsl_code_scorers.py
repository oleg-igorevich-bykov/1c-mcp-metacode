"""
Tokenization, IDF construction and hybrid scoring primitives for BSL code search.

Implements the validated hybrid scorer used by the vector path: 1c-light
tokenizer + synonym expansion, classic BM25 (k1=1.2, b=0.75), per-field BM25
with custom weights, metadata + fuzzy metadata boosts, quoted-phrase boost,
and min-max normalization. The default weights below are the ones the
reference scorer settled on.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple


WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9_]+")
CAMEL_RE = re.compile(r"(?<=[а-яa-z0-9])(?=[А-ЯA-Z])")
QUOTE_RE = re.compile(r"[\"'«“](.+?)[\"'»”]")
CYRILLIC_RE = re.compile(r"[а-яё]+")

FUZZY_RU_SUFFIXES: Tuple[str, ...] = (
    "иями", "ями", "ами", "ого", "ему", "ыми", "ими",
    "иях", "иям", "ией", "ию", "ия",
    "ые", "ие", "ой", "ий", "ый", "ая", "яя", "ое", "ее", "ую", "юю",
    "ов", "ев", "ам", "ям", "ах", "ях", "ом", "ем", "ей", "ою", "ею",
    "ы", "и", "а", "я", "е", "у", "ю",
)

STOP_WORDS: frozenset = frozenset({
    "как", "где", "что", "чтобы", "если", "или", "для", "при", "это",
    "этот", "эта", "эти", "этой", "этого", "будет", "есть", "нет",
    "надо", "нужно", "который", "которая", "которые", "каких", "каким",
    "какие", "по", "из", "на", "в", "с", "к", "от", "до", "и", "а",
    "же", "ли", "не", "за", "под", "над", "через", "между",
})

STOP_WORDS_1C_LIGHT: frozenset = STOP_WORDS | frozenset({
    "тогда", "иначе", "иначеесли", "конецесли",
    "для", "каждого", "цикл", "конеццикла", "пока",
    "попытка", "исключение", "конецпопытки",
    "возврат", "перем", "знач", "экспорт", "новый",
    "истина", "ложь", "неопределено", "null", "true", "false",
    "procedure", "function", "endprocedure", "endfunction",
    "if", "then", "else", "elsif", "endif",
    "for", "each", "while", "return", "export", "new",
})


# Base 1C-domain synonyms (profile name: "platform").
QUERY_SYNONYMS_1C: Dict[str, List[str]] = {
    "справочник": ["справочники", "catalogs", "catalog"],
    "справочники": ["справочник", "catalogs", "catalog"],
    "документ": ["документы", "documents"],
    "документы": ["документ", "documents"],
    "отчет": ["отчеты", "reports"],
    "отчеты": ["отчет", "reports"],
    "обработка": ["обработки", "dataprocessors"],
    "обработки": ["обработка", "dataprocessors"],
    "регистр": ["регистры", "registers"],
    "регистры": ["регистр", "registers"],
    "сведений": ["информации", "informationregisters"],
    "накопления": ["accumulationregisters"],
    "бухгалтерии": ["accountingregisters"],
    "перечисление": ["перечисления", "enums"],
    "перечисления": ["перечисление", "enums"],
    "план": ["планы", "chart"],
    "планы": ["план", "charts"],
    "константа": ["константы", "constants"],
    "константы": ["константа", "constants"],
    "модуль": ["модули", "module", "modules"],
    "модули": ["модуль", "module", "modules"],
    "общий": ["общие", "common"],
    "общие": ["общий", "common"],
    "процедура": ["процедуры", "procedure"],
    "процедуры": ["процедура", "procedure"],
    "функция": ["функции", "function"],
    "функции": ["функция", "function"],
    "экспорт": ["export"],
    "область": ["области", "region"],
    "области": ["область", "region"],
    "вызов": ["вызовы", "call", "calls"],
    "вызовы": ["вызов", "call", "calls"],
    "форма": ["формы", "form", "forms"],
    "формы": ["форма", "form", "forms"],
    "команда": ["команды", "command", "commands"],
    "команды": ["команда", "command", "commands"],
    "обработчик": ["обработчики", "событие", "event", "handler"],
    "обработчики": ["обработчик", "события", "event", "handler"],
    "событие": ["события", "обработчик", "event"],
    "события": ["событие", "обработчик", "event"],
    "элемент": ["элементы", "element"],
    "элементы": ["элемент", "element"],
    "объект": ["объекты", "object"],
    "объекты": ["объект", "object"],
    "менеджер": ["manager"],
    "клиент": ["клиенте", "client"],
    "клиенте": ["клиент", "client"],
    "сервер": ["сервере", "server"],
    "сервере": ["сервер", "server"],
    "запрос": ["запросы", "query", "выбрать", "поместить"],
    "запросы": ["запрос", "query", "выбрать", "поместить"],
    "таблица": ["таблицы", "table"],
    "таблицы": ["таблица", "table"],
    "временная": ["временные", "таблица"],
    "временные": ["временная", "таблица"],
    "табличная": ["табличные", "часть"],
    "табличные": ["табличная", "часть"],
    "часть": ["части", "табличная"],
    "части": ["часть", "табличная"],
    "макет": ["макеты", "layout"],
    "макеты": ["макет", "layout"],
    "печать": ["макет", "табличный", "документ"],
    "табличный": ["документ", "макет"],
    "проведение": ["движения", "регистры"],
    "движение": ["движения", "регистр"],
    "движения": ["движение", "регистр"],
    "запись": ["записи", "набор"],
    "записи": ["запись", "набор"],
    "набор": ["записи"],
}

# Extended UI-focused synonyms (profile name: "platform-ui").
QUERY_SYNONYMS_PLATFORM_UI_1C: Dict[str, List[str]] = {
    **QUERY_SYNONYMS_1C,
    "клик": ["нажатие", "нажать", "щелчок"],
    "клика": ["нажатие", "нажать", "щелчок"],
    "клике": ["нажатие", "нажать", "щелчок"],
    "щелчок": ["клик", "нажатие", "нажать"],
    "щелчка": ["клик", "нажатие", "нажать"],
    "нажатие": ["нажать", "клик", "щелчок"],
    "нажатия": ["нажать", "клик", "щелчок"],
    "нажать": ["нажатие", "клик", "щелчок"],
    "закрытие": ["закрыть", "закрытия"],
    "закрытия": ["закрытие", "закрыть"],
    "закрыть": ["закрытие", "закрытия"],
    "запрет": ["запретить", "запрете", "запрета"],
    "запрете": ["запрет", "запретить"],
    "запрета": ["запрет", "запретить"],
    "запретить": ["запрет", "запрете"],
    "пересоздать": ["перезаполнить", "обновить", "заполнить"],
    "пересоздается": ["перезаполнить", "обновить", "заполнить"],
    "пересоздание": ["перезаполнить", "обновить", "заполнить"],
    "перезаполнить": ["пересоздать", "обновить", "заполнить"],
}


GENERIC_SYMBOL_NAMES_1C_EVENTS: Tuple[str, ...] = (
    "ПриСозданииНаСервере", "ПриОткрытии", "ПередОткрытием",
    "ПриЗакрытии", "ПередЗакрытием", "ОбработкаКоманды",
    "ОбработкаВыбора", "ОбработкаПолученияФормы", "ПриИзменении",
    "НачалоВыбора", "Очистка", "АвтоПодбор",
    "ПередЗаписью", "ПриЗаписи", "ПослеЗаписи",
    "ОбработкаПроверкиЗаполнения", "ПередУдалением", "ПриУдалении",
    "ПриАктивизацииСтроки", "ПриВыводеСтроки",
    "ПриПолученииДанныхНаСервере", "ПриПолученииДанных",
    "ПередНачаломДобавления", "ПередУдалениемСтроки",
    "ПриНачалеРедактирования", "ПриОкончанииРедактирования",
    "ПередОкончаниемРедактирования", "ПередЗаписьюНаСервере",
    "ПослеЗаписиНаСервере", "ПередЗаписьюВФорме",
)


def stop_words_for_profile(profile: str) -> frozenset:
    if profile == "1c_light":
        return STOP_WORDS_1C_LIGHT
    return STOP_WORDS


def tokenize(text: str, stop_profile: str = "base") -> List[str]:
    prepared = CAMEL_RE.sub(" ", text or "").lower()
    stop_words = stop_words_for_profile(stop_profile)
    return [
        token
        for token in WORD_RE.findall(prepared)
        if len(token) >= 3 and token not in stop_words
    ]


def tokenize_1c_light(text: str) -> List[str]:
    return tokenize(text, "1c_light")


def expand_tokens_with_1c_synonyms(
    query_tokens: Sequence[str],
    weight: int,
    profile: str,
) -> List[str]:
    """
    Expand each query token with its 1c-domain synonyms. Synonyms are
    tokenized with the base stop-word profile (NOT 1c_light), so synonym
    tokens like "возврат"/"экспорт" survive even when the surrounding query
    uses the more aggressive 1c_light filtering.
    """
    if weight <= 0:
        return list(query_tokens)
    if profile == "platform-ui":
        synonyms = QUERY_SYNONYMS_PLATFORM_UI_1C
    else:
        synonyms = QUERY_SYNONYMS_1C
    expanded = list(query_tokens)
    original = set(query_tokens)
    for token in query_tokens:
        for synonym in synonyms.get(token, []):
            for synonym_token in tokenize(synonym):
                if synonym_token not in original:
                    expanded.extend([synonym_token] * weight)
    return expanded


def generic_symbol_keys_set() -> set[tuple[str, ...]]:
    return {
        tuple(tokenize_1c_light(name))
        for name in GENERIC_SYMBOL_NAMES_1C_EVENTS
        if tokenize_1c_light(name)
    }


def build_idf(tokens_by_id: Dict[str, Sequence[str]]) -> Tuple[Dict[str, float], float, int]:
    """Return (idf, avgdl, doc_count) for the given per-document token lists."""
    document_count = len(tokens_by_id)
    df: Counter = Counter()
    total_length = 0
    for tokens in tokens_by_id.values():
        total_length += len(tokens)
        df.update(set(tokens))
    avgdl = total_length / document_count if document_count else 1.0
    idf = {
        token: math.log(1.0 + (document_count - count + 0.5) / (count + 0.5))
        for token, count in df.items()
    }
    return idf, avgdl, document_count


def build_df_only(tokens_by_id: Dict[str, Sequence[str]]) -> Tuple[Dict[str, int], float, int]:
    """Same corpus pass as build_idf, but returns raw df counts. Used for IDF storage."""
    document_count = len(tokens_by_id)
    df: Counter = Counter()
    total_length = 0
    for tokens in tokens_by_id.values():
        total_length += len(tokens)
        df.update(set(tokens))
    avgdl = total_length / document_count if document_count else 1.0
    return dict(df), avgdl, document_count


def idf_from_df(df_by_token: Dict[str, int], doc_count: int) -> Dict[str, float]:
    if doc_count <= 0:
        return {}
    return {
        token: math.log(1.0 + (doc_count - count + 0.5) / (count + 0.5))
        for token, count in df_by_token.items()
    }


def bm25_score(
    query_tokens: Sequence[str],
    document_tokens: Sequence[str],
    idf: Dict[str, float],
    avgdl: float,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    if not query_tokens or not document_tokens:
        return 0.0
    term_frequency = Counter(document_tokens)
    doc_length = len(document_tokens)
    score = 0.0
    for token in query_tokens:
        frequency = term_frequency.get(token, 0)
        if not frequency:
            continue
        score += idf.get(token, 0.0) * (
            frequency * (k1 + 1.0)
        ) / (frequency + k1 * (1.0 - b + b * doc_length / avgdl))
    return score


def field_bm25_score(
    query_tokens: Sequence[str],
    chunk_id: str,
    field_tokens_by_id: Dict[str, Dict[str, List[str]]],
    field_idf: Dict[str, Tuple[Dict[str, float], float]],
    field_weights: Dict[str, float],
    bm25_k1: float = 1.2,
    bm25_b: float = 0.75,
) -> float:
    fields = field_tokens_by_id.get(chunk_id, {})
    total = 0.0
    for field_name, weight in field_weights.items():
        if weight == 0:
            continue
        idf_avgdl = field_idf.get(field_name)
        if idf_avgdl is None:
            continue
        idf, avgdl = idf_avgdl
        total += weight * bm25_score(
            query_tokens,
            fields.get(field_name, []),
            idf,
            avgdl,
            bm25_k1,
            bm25_b,
        )
    return total


def token_overlap_ratio(query_tokens: Sequence[str], document_tokens: Sequence[str]) -> float:
    if not query_tokens or not document_tokens:
        return 0.0
    return len(set(query_tokens) & set(document_tokens)) / len(set(query_tokens))


def metadata_boost_score(
    query_tokens: Sequence[str],
    chunk_id: str,
    field_tokens_by_id: Dict[str, Dict[str, List[str]]],
    symbol_weight: float,
    object_weight: float,
    form_weight: float,
    generic_symbol_keys: Optional[set] = None,
    generic_symbol_penalty: float = 1.0,
) -> float:
    fields = field_tokens_by_id.get(chunk_id, {})
    symbol_tokens = fields.get("symbol", [])
    sw = symbol_weight
    if generic_symbol_keys and tuple(symbol_tokens) in generic_symbol_keys:
        sw = symbol_weight * generic_symbol_penalty
    return (
        sw * token_overlap_ratio(query_tokens, symbol_tokens)
        + object_weight * token_overlap_ratio(query_tokens, fields.get("object", []))
        + form_weight * token_overlap_ratio(query_tokens, fields.get("form", []))
    )


def fuzzy_token_key(token: str) -> str:
    value = token.lower().replace("_", "")
    if not CYRILLIC_RE.fullmatch(value):
        return value
    if len(value) <= 4:
        return value
    for suffix in FUZZY_RU_SUFFIXES:
        if value.endswith(suffix) and len(value) - len(suffix) >= 4:
            return value[: -len(suffix)]
    return value


def fuzzy_token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if left.startswith(right) or right.startswith(left):
        return 0.94
    return SequenceMatcher(None, left, right).ratio()


def fuzzy_metadata_field_score(
    query_tokens: Sequence[str],
    field_tokens: Sequence[str],
    threshold: float,
) -> float:
    if not query_tokens or not field_tokens:
        return 0.0
    query_keys = [fuzzy_token_key(t) for t in query_tokens]
    field_keys = [fuzzy_token_key(t) for t in field_tokens]
    matched = 0.0
    for field_key in field_keys:
        best = max(
            fuzzy_token_similarity(field_key, query_key)
            for query_key in query_keys
        )
        if best >= threshold:
            matched += best
    return matched / len(field_keys)


def fuzzy_metadata_boost_score(
    query_tokens: Sequence[str],
    chunk_id: str,
    field_tokens_by_id: Dict[str, Dict[str, List[str]]],
    symbol_weight: float,
    object_weight: float,
    form_weight: float,
    metadata_type_weight: float,
    threshold: float = 0.88,
) -> float:
    fields = field_tokens_by_id.get(chunk_id, {})
    return (
        symbol_weight * fuzzy_metadata_field_score(query_tokens, fields.get("symbol", []), threshold)
        + object_weight * fuzzy_metadata_field_score(query_tokens, fields.get("object", []), threshold)
        + form_weight * fuzzy_metadata_field_score(query_tokens, fields.get("form", []), threshold)
        + metadata_type_weight * fuzzy_metadata_field_score(
            query_tokens, fields.get("metadata_type", []), threshold,
        )
    )


def quoted_phrase_tokens(query: str) -> List[List[str]]:
    return [
        tokens
        for tokens in (
            tokenize_1c_light(m.group(1))
            for m in QUOTE_RE.finditer(query or "")
        )
        if tokens
    ]


def quoted_symbol_boost_score(
    quoted_phrases: List[List[str]],
    chunk_id: str,
    field_tokens_by_id: Dict[str, Dict[str, List[str]]],
) -> float:
    if not quoted_phrases:
        return 0.0
    symbol_tokens = field_tokens_by_id.get(chunk_id, {}).get("symbol", [])
    if not symbol_tokens:
        return 0.0
    return max(token_overlap_ratio(phrase, symbol_tokens) for phrase in quoted_phrases)


def normalize(values: Sequence[float]) -> List[float]:
    if not values:
        return list(values)
    minimum = min(values)
    maximum = max(values)
    if math.isclose(minimum, maximum):
        return [0.0] * len(values)
    return [(v - minimum) / (maximum - minimum) for v in values]
