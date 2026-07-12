"""
Mapping of 1C XML dump folder names to Russian category names used in the graph.
Single source of truth shared by XmlMetadataParser, CodeFileIndexer classification,
and any downstream code that needs to translate between the two namespaces.
"""

from __future__ import annotations


FOLDER_TO_RU_CATEGORY: dict[str, str] = {
    "Languages": "Языки",
    "Subsystems": "Подсистемы",
    "Styles": "Стили",
    "StyleItems": "ЭлементыСтиля",
    "CommonPictures": "ОбщиеКартинки",
    "Interfaces": "Интерфейсы",
    "SessionParameters": "ПараметрыСеанса",
    "Roles": "Роли",
    "CommonTemplates": "ОбщиеМакеты",
    "FilterCriteria": "КритерииОтбора",
    "CommonModules": "ОбщиеМодули",
    "CommonAttributes": "ОбщиеРеквизиты",
    "ExchangePlans": "ПланыОбмена",
    "XDTOPackages": "ПакетыXDTO",
    "WebServices": "WebСервисы",
    "HTTPServices": "HTTPСервисы",
    "WSReferences": "WSСсылки",
    "WebSocketClients": "WebSocket-клиенты",
    "EventSubscriptions": "ПодпискиНаСобытия",
    "ScheduledJobs": "РегламентныеЗадания",
    "Bots": "Боты",
    "SettingsStorages": "ХранилищаНастроек",
    "FunctionalOptions": "ФункциональныеОпции",
    "FunctionalOptionsParameters": "ПараметрыФункциональныхОпций",
    "DefinedTypes": "ОпределяемыеТипы",
    "CommonCommands": "ОбщиеКоманды",
    "CommandGroups": "ГруппыКоманд",
    "Constants": "Константы",
    "CommonForms": "ОбщиеФормы",
    "Catalogs": "Справочники",
    "Documents": "Документы",
    "DocumentNumerators": "НумераторыДокументов",
    "DocumentJournals": "ЖурналыДокументов",
    "Enums": "Перечисления",
    "Reports": "Отчеты",
    "DataProcessors": "Обработки",
    "InformationRegisters": "РегистрыСведений",
    "AccumulationRegisters": "РегистрыНакопления",
    "AccountingRegisters": "РегистрыБухгалтерии",
    "Sequences": "Последовательности",
    "ChartsOfCharacteristicTypes": "ПланыВидовХарактеристик",
    "ChartsOfCalculationTypes": "ПланыВидовРасчета",
    "ChartsOfAccounts": "ПланыСчетов",
    "CalculationRegisters": "РегистрыРасчета",
    "BusinessProcesses": "БизнесПроцессы",
    "Tasks": "Задачи",
    "IntegrationServices": "СервисыИнтеграции",
    "ExternalDataSources": "ВнешниеИсточникиДанных",
}


RU_CATEGORY_TO_FOLDER: dict[str, str] = {ru: en for en, ru in FOLDER_TO_RU_CATEGORY.items()}

FOLDER_NAMES: frozenset[str] = frozenset(FOLDER_TO_RU_CATEGORY.keys())
