"""
Общий механизм перевода EN→RU имён XML-свойств метаданных 1С.

Используется classifier-ом и extractor-ом расширений как единая точка резолва имени
свойства. Источник истины для базовых XML-свойств — XML_PROP_TO_RU из xml_metadata.rules.
"""

from __future__ import annotations

from typing import Optional

from .rules import XML_PROP_TO_RU

# Свойства, которых нет в XML_PROP_TO_RU, но они встречаются в XML расширений
# (например, формы реквизита, дополнительные параметры команд, типы значений).
ADDITIONAL_PROPERTY_TRANSLATIONS: dict[str, str] = {
    "ButtonRepresentation": "ВидКнопки",
    "CommandInterface": "ИнтерфейсКоманд",
    "DefaultValue": "ЗначениеЗаполнения",
    "FilterCriterionType": "ТипКритерияОтбора",
    "FolderChoiceForm": "ФормаВыбораГруппы",
    "FolderForm": "ФормаГруппы",
    "LeadingAttribute": "ОсновнойРеквизитАдресации",
    "ListForm": "ФормаСписка",
    "ModifiesStorageData": "ИзменяетДанные",
    "Module": "Модуль",
    "ObjectForm": "ФормаОбъекта",
    "Order": "Порядок",
    "ParameterType": "ТипПараметра",
    "RecordForm": "ФормаЗаписи",
    "Storage": "Хранилище",
    "ValueType": "ТипЗначения",
    "WSDLLinkAddress": "АдресДляПубликации",
    "XDTOReturningType": "ВозвращаемыйТипXDTO",
}

# Имена разделов / коллекций метаданных (ChildObjects, Attributes, Forms, ...).
METADATA_SECTION_TRANSLATION: dict[str, str] = {
    "Attributes": "Реквизиты",
    "ChildObjects": "ПодчиненныеОбъекты",
    "Commands": "Команды",
    "Dimensions": "Измерения",
    "Forms": "Формы",
    "Methods": "Методы",
    "Operations": "Операции",
    "Parameters": "Параметры",
    "Points": "Точки",
    "Resources": "Ресурсы",
    "Rights": "Права",
    "StandardAttributes": "СтандартныеРеквизиты",
    "TabularSections": "ТабличныеЧасти",
    "Templates": "Макеты",
    "URLTemplates": "ШаблоныURL",
}

# Контекстные переопределения: базовый перевод XML_PROP_TO_RU для отдельного типа
# объекта в 1С называется иначе.
PROPERTY_NAME_OVERRIDES: dict[str, dict[str, str]] = {
    "ScheduledJob": {
        "Schedule": "Расписание",
    },
    "StyleItem": {
        "Type": "Вид",
    },
}


def translate_metadata_name(name: str, object_type: Optional[str] = None) -> str:
    """
    Перевести EN-имя XML-свойства или раздела метаданных в RU.

    Порядок резолва:
    1. PROPERTY_NAME_OVERRIDES[object_type][name] — если задан object_type.
    2. XML_PROP_TO_RU[name].
    3. ADDITIONAL_PROPERTY_TRANSLATIONS[name].
    4. METADATA_SECTION_TRANSLATION[name].
    5. name как есть.
    """
    if object_type is not None:
        override = PROPERTY_NAME_OVERRIDES.get(object_type, {}).get(name)
        if override is not None:
            return override

    base = XML_PROP_TO_RU.get(name)
    if base is not None:
        return base

    additional = ADDITIONAL_PROPERTY_TRANSLATIONS.get(name)
    if additional is not None:
        return additional

    section = METADATA_SECTION_TRANSLATION.get(name)
    if section is not None:
        return section

    return name
