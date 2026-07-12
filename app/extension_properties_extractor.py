"""
Извлечение значений свойств из XML файлов расширений 1С.

Дополняет данные из TXT метаданных, где значения могут отсутствовать
для заимствованных объектов.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# Namespaces
NS_XR = "http://v8.1c.ru/8.3/xcf/readable"
NS_V8 = "http://v8.1c.ru/8.1/data/core"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
NS_CFG = "http://v8.1c.ru/8.1/data/enterprise/current-config"
NS_XS = "http://www.w3.org/2001/XMLSchema"

from extension_properties_classifier import EXCLUDED_PROPERTIES
from xml_metadata.property_names import translate_metadata_name

# Свойства, которые НЕ нужно извлекать для конкретных типов объектов.
# Значения этих свойств уже корректно загружены из TXT, а XML-значение
# хранит данные в другом формате (английский QN, и т.д.).
SKIP_VALUE_EXTRACTION: Dict[str, Set[str]] = {
    "FunctionalOption": {"Location"},  # XML: "InformationRegister.X.Resource.Y", TXT: "РегистрСведений.X.Ресурс.Y"
}

# Словарь для перевода значений свойств из XML (EN -> RU)
# Используется для перевода значений перечислений 1С
PROPERTY_VALUES_TRANSLATION = {
    # Длина кода/строки
    "Variable": "Переменная",
    "Fixed": "Фиксированная",

    # Тип кода
    "String": "Строка",
    "Number": "Число",

    # Знак числа
    "Nonnegative": "Неотрицательное",
    "Any": "Любое",

    # Булевы значения
    "true": "Истина",
    "false": "Ложь",

    # Проверка заполнения
    "DontCheck": "НеПроверять",
    "ShowError": "ВыдаватьОшибку",

    # Использование
    "Use": "Использовать",
    "DontUse": "НеИспользовать",

    # Индексирование
    "Index": "Индексировать",
    "DontIndex": "НеИндексировать",
    "IndexWithAdditionalOrder": "ИндексироватьСДополнительнымПорядком",

    # Способ выбора
    "BothWays": "ОбаСпособа",
    "QuickChoice": "БыстрыйВыбор",
    "InputByString": "ВводПоСтроке",

    # Выбор групп и элементов
    "Items": "Элементы",
    "Folders": "Группы",
    "FoldersAndItems": "ГруппыИЭлементы",

    # Полнотекстовый поиск
    "DontUse": "НеИспользовать",
    "Allow": "Разрешить",

    # Создание при вводе
    "Auto": "Авто",

    # Другие часто используемые значения
    "Yes": "Да",
    "No": "Нет",

    # CommonCommand.Group / CommandGroup.Category — группы команд форм (с префиксом Form и без)
    "FormNavigationPanelGoTo": "ПанельНавигацииФормыПерейти",
    "FormNavigationPanelOrdinary": "ПанельНавигацииОбычное",
    "FormCommandBar": "КоманднаяПанельФормы",
    "FormCommandBarImportant": "КоманднаяПанельФормыВажное",
    "FormCommandBarSeeAlso": "КоманднаяПанельФормыСмотриТакже",
    "FormCommandBarMoreActions": "КоманднаяПанельФормыЕщё",
    # Варианты без префикса Form (встречаются в CommonCommand.Group)
    "NavigationPanelOrdinary": "ПанельНавигацииОбычное",
    "NavigationPanelGoTo": "ПанельНавигацииФормыПерейти",
    "CommandBar": "КоманднаяПанельФормы",
    "CommandBarImportant": "КоманднаяПанельФормыВажное",
    "CommandBarSeeAlso": "КоманднаяПанельФормыСмотриТакже",
    "CommandBarMoreActions": "КоманднаяПанельФормыЕщё",

    # StyleItem.Type (Вид)
    "Color": "Цвет",
    "Font": "Шрифт",
    "SpreadsheetDocument": "ТабличныйДокумент",

    # CommonModule.ReturnValuesReuse
    "DuringRequest": "ВовремяВызова",
    "DuringSession": "ВовремяСеанса",

    # FunctionalOption.Location (Хранение) — значение уже в виде QN, перевод не нужен
}


@dataclass
class ElementPropertyValues:
    """Значения свойств одного элемента (Attribute/Resource/Dimension/TabularPart)"""
    element_type: str  # "Attribute", "Resource", "Dimension", "TabularSection"
    element_name: str
    element_uuid: str
    is_adopted: bool
    parent_name: Optional[str] = None  # Для реквизитов табличной части

    # Значения свойств из XML (на русском языке)
    property_values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ObjectPropertyValues:
    """Значения свойств объекта метаданных и всех его элементов"""
    object_type: str  # "Document", "Catalog", etc.
    object_name: str
    object_uuid: str
    elements: List[ElementPropertyValues] = field(default_factory=list)
    xml_path: Optional[Path] = None


class ExtensionPropertiesExtractor:
    """
    Извлечение значений свойств из XML файлов расширений 1С.

    Дополняет данные из TXT метаданных, где значения могут отсутствовать
    для заимствованных объектов.
    """

    def __init__(self):
        """Инициализация экстрактора"""
        self.namespaces = {
            "xr": NS_XR,
            "v8": NS_V8,
            "xsi": NS_XSI,
            "cfg": NS_CFG,
            "xs": NS_XS,
        }

    def extract_from_xml(self, xml_path: Path) -> Optional[ObjectPropertyValues]:
        """
        Извлекает значения свойств из XML файла объекта метаданных.

        Args:
            xml_path: Путь к XML файлу (например, Documents/БольничныйЛист.xml)

        Returns:
            ObjectPropertyValues или None в случае ошибки
        """
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            # Определяем тип объекта метаданных
            object_elem = self._find_metadata_object(root)
            if object_elem is None:
                logger.debug(f"Пропущен файл (не содержит анализируемых объектов метаданных): {xml_path}")
                return None

            object_type = self._local_name(object_elem.tag)
            object_uuid = object_elem.get("uuid", "")

            # Ищем имя объекта в Properties/Name
            props = object_elem.find(".//{*}Properties")
            if props is None:
                logger.warning(f"Не найден элемент Properties в {xml_path}")
                return None

            object_name = self._get_text(props.find("{*}Name"))
            if not object_name:
                logger.warning(f"Не найдено имя объекта в {xml_path}")
                return None

            # Извлекаем значения свойств для самого объекта метаданных
            elements = []
            object_extraction = self._extract_metadata_object_properties(
                object_elem, object_type, object_name, object_uuid
            )
            if object_extraction:
                elements.append(object_extraction)

            # Извлекаем значения свойств для всех дочерних элементов
            child_objects = object_elem.find("{*}ChildObjects")

            if child_objects is not None:
                for elem in child_objects:
                    elem_type = self._local_name(elem.tag)

                    # Обрабатываем только нужные типы элементов
                    if elem_type in {
                        "Attribute", "TabularSection", "Resource", "Dimension",
                        "EnumValue", "Column", "URLTemplate", "AddressingAttribute",
                    }:
                        extraction = self._extract_element_properties(elem, elem_type, parent_name=None)
                        if extraction:
                            elements.append(extraction)

                        # Для TabularSection рекурсивно обрабатываем вложенные реквизиты
                        if elem_type == "TabularSection":
                            ts_name = self._get_text(elem.find(".//{*}Properties/{*}Name"))
                            ts_child_objects = elem.find("{*}ChildObjects")

                            if ts_child_objects is not None and ts_name:
                                for child_elem in ts_child_objects:
                                    child_type = self._local_name(child_elem.tag)

                                    if child_type == "Attribute":
                                        child_extraction = self._extract_element_properties(
                                            child_elem, child_type, parent_name=ts_name
                                        )
                                        if child_extraction:
                                            elements.append(child_extraction)

                        # Для URLTemplate рекурсивно обрабатываем вложенные Method
                        if elem_type == "URLTemplate":
                            url_template_name = self._get_text(elem.find(".//{*}Properties/{*}Name"))
                            url_template_children = elem.find("{*}ChildObjects")

                            if url_template_children is not None and url_template_name:
                                for method_elem in url_template_children:
                                    if self._local_name(method_elem.tag) == "Method":
                                        method_extraction = self._extract_element_properties(
                                            method_elem, "Method", parent_name=url_template_name
                                        )
                                        if method_extraction:
                                            elements.append(method_extraction)

            return ObjectPropertyValues(
                object_type=object_type,
                object_name=object_name,
                object_uuid=object_uuid,
                elements=elements,
                xml_path=xml_path
            )

        except ET.ParseError as e:
            logger.error(f"Ошибка парсинга XML {xml_path}: {e}")
            return None
        except Exception as e:
            logger.error(f"Ошибка извлечения свойств из {xml_path}: {e}", exc_info=True)
            return None

    def _find_metadata_object(self, root: ET.Element) -> Optional[ET.Element]:
        """Находит корневой элемент объекта метаданных (Document, Catalog, и т.д.)"""
        # Типы объектов метаданных 1С
        object_types = {
            "Document", "Catalog", "ChartOfAccounts", "ChartOfCharacteristicTypes",
            "ChartOfCalculationTypes", "InformationRegister", "AccumulationRegister",
            "AccountingRegister", "CalculationRegister", "BusinessProcess", "Task",
            "ExchangePlan", "Enum", "DataProcessor", "Report",
            "CommonModule", "CommonCommand", "Constant", "DocumentJournal",
            "HTTPService", "WebService", "XDTOPackage",
            "CommonTemplate", "CommonPicture", "StyleItem",
            "FunctionalOption", "FunctionalOptionsParameter",
            "DefinedType", "Language", "Role", "Subsystem",
            "CommandGroup", "CommonAttribute", "FilterCriterion",
            "ScheduledJob", "SessionParameter", "SettingsStorage",
        }

        for child in root:
            local_name = self._local_name(child.tag)
            if local_name in object_types:
                return child

        return None

    def _extract_metadata_object_properties(
        self,
        object_elem: ET.Element,
        object_type: str,
        object_name: str,
        object_uuid: str
    ) -> Optional[ElementPropertyValues]:
        """
        Извлекает значения свойств для самого объекта метаданных.

        Args:
            object_elem: XML-элемент объекта метаданных
            object_type: Тип объекта ("Document", "Catalog", и т.д.)
            object_name: Имя объекта
            object_uuid: UUID объекта

        Returns:
            ElementPropertyValues с типом "MetadataObject" или None
        """
        props = object_elem.find("{*}Properties")
        if props is None:
            return None

        # Определяем: заимствованный или собственный
        obj_belonging = props.find("{*}ObjectBelonging")
        belonging_value = self._get_text(obj_belonging) if obj_belonging is not None else ""
        is_adopted = belonging_value == "Adopted"

        # Для собственных объектов не извлекаем свойства
        if not is_adopted:
            return None

        # Для заимствованных объектов - извлекаем значения свойств
        property_values = {}

        # Анализируем все дочерние элементы Properties
        skip_for_type = SKIP_VALUE_EXTRACTION.get(object_type, set())

        for prop_elem in props:
            prop_name_en = self._local_name(prop_elem.tag)

            # Пропускаем служебные свойства
            if prop_name_en in EXCLUDED_PROPERTIES:
                continue

            # Пропускаем свойства с несовместимым XML-форматом значения
            if prop_name_en in skip_for_type:
                continue

            # Извлекаем значение свойства
            prop_value = self._extract_property_value(prop_elem, prop_name_en)

            if prop_value is not None:
                prop_name_ru = translate_metadata_name(prop_name_en, object_type=object_type)
                # Переводим значение свойства на русский
                prop_value_ru = self._translate_property_value(prop_value)
                property_values[prop_name_ru] = prop_value_ru

        return ElementPropertyValues(
            element_type="MetadataObject",
            element_name=object_name,
            element_uuid=object_uuid,
            is_adopted=True,
            parent_name=None,
            property_values=property_values
        )

    def _extract_element_properties(
        self,
        elem: ET.Element,
        elem_type: str,
        parent_name: Optional[str] = None
    ) -> Optional[ElementPropertyValues]:
        """
        Извлекает значения свойств для одного элемента.

        Args:
            elem: XML-элемент
            elem_type: Тип элемента ("Attribute", "TabularSection", и т.д.)
            parent_name: Имя родителя (для реквизитов табличной части)

        Returns:
            ElementPropertyValues или None
        """
        props = elem.find("{*}Properties")
        if props is None:
            return None

        elem_name = self._get_text(props.find("{*}Name"))
        elem_uuid = elem.get("uuid", "")

        if not elem_name:
            return None

        # Определяем: заимствованный или собственный
        obj_belonging = props.find("{*}ObjectBelonging")
        belonging_value = self._get_text(obj_belonging) if obj_belonging is not None else ""
        is_adopted = belonging_value == "Adopted"

        # Для собственных элементов не извлекаем свойства (они уже есть в TXT)
        if not is_adopted:
            return ElementPropertyValues(
                element_type=elem_type,
                element_name=elem_name,
                element_uuid=elem_uuid,
                is_adopted=False,
                parent_name=parent_name
            )

        # Для заимствованных элементов - извлекаем значения свойств
        property_values = {}

        # Анализируем все дочерние элементы Properties
        for prop_elem in props:
            prop_name_en = self._local_name(prop_elem.tag)

            # Пропускаем служебные свойства
            if prop_name_en in EXCLUDED_PROPERTIES:
                continue

            # Извлекаем значение свойства
            prop_value = self._extract_property_value(prop_elem, prop_name_en)

            if prop_value is not None:
                prop_name_ru = translate_metadata_name(prop_name_en)
                # Переводим значение свойства на русский
                prop_value_ru = self._translate_property_value(prop_value)
                property_values[prop_name_ru] = prop_value_ru

        return ElementPropertyValues(
            element_type=elem_type,
            element_name=elem_name,
            element_uuid=elem_uuid,
            is_adopted=True,
            parent_name=parent_name,
            property_values=property_values
        )

    def _extract_property_value(self, prop_elem: ET.Element, prop_name: str) -> Any:
        """
        Извлекает значение свойства из XML элемента.

        Args:
            prop_elem: XML элемент свойства
            prop_name: Имя свойства (на английском)

        Returns:
            Значение свойства (строка, список, число и т.д.) или None
        """
        # Специальная обработка для свойства Type
        if prop_name == "Type":
            return self._extract_type_property(prop_elem)

        # Специальная обработка для свойства Synonym (многоязычный)
        if prop_name == "Synonym":
            return self._extract_synonym_property(prop_elem)

        # Простое текстовое свойство
        text = self._get_text(prop_elem)
        if text:
            return text

        return None

    def _extract_type_property(self, type_elem: ET.Element) -> Any:
        """
        Извлекает значение свойства Type.

        Обрабатывает:
        - Простые типы: <v8:Type>xs:string</v8:Type>
        - Составные типы: xr:ExtendedProperty с CheckValue и ExtendValue
        - Квалификаторы: NumberQualifiers, StringQualifiers

        Returns:
            Список строк (всегда список для унификации с txt-парсером)
        """
        # Проверяем: это ExtendedProperty (составной тип)?
        xsi_type = type_elem.get(f"{{{NS_XSI}}}type", "")

        if xsi_type == "xr:ExtendedProperty":
            # Составной тип: собираем типы из CheckValue и ExtendValue
            types = []

            # CheckValue
            check_value = type_elem.find(f"{{{NS_XR}}}CheckValue")
            if check_value is not None:
                check_types = check_value.findall(f"{{{NS_V8}}}Type")
                for t in check_types:
                    type_str = self._normalize_type_string(self._get_text(t))
                    if type_str:
                        types.append(type_str)

            # ExtendValue
            extend_value = type_elem.find(f"{{{NS_XR}}}ExtendValue")
            if extend_value is not None:
                extend_types = extend_value.findall(f"{{{NS_V8}}}Type")
                for t in extend_types:
                    type_str = self._normalize_type_string(self._get_text(t))
                    if type_str:
                        types.append(type_str)

            # Возвращаем список уникальных типов
            return list(dict.fromkeys(types)) if types else None

        else:
            # Простой тип
            type_elements = type_elem.findall(f"{{{NS_V8}}}Type")

            # Обработка v8:TypeSet (коллекция типов, например cfg:AnyIBRef)
            type_set_elements = type_elem.findall(f"{{{NS_V8}}}TypeSet")
            if type_set_elements and not type_elements:
                types = []
                for ts in type_set_elements:
                    type_str = self._normalize_type_string(self._get_text(ts))
                    if type_str:
                        types.append(type_str)
                return list(dict.fromkeys(types)) if types else None

            if not type_elements:
                # Fallback: plain-text Type (например, StyleItem <Type>Color</Type>)
                plain_text = self._get_text(type_elem)
                return plain_text if plain_text else None

            # Берем первый тип
            type_str = self._normalize_type_string(self._get_text(type_elements[0]))

            if not type_str:
                return None

            # Проверяем наличие квалификаторов
            # NumberQualifiers
            num_quals = type_elem.find(f"{{{NS_V8}}}NumberQualifiers")
            if num_quals is not None:
                # Возвращаем как список (унификация)
                return [self._format_number_type_with_qualifiers(num_quals)]

            # StringQualifiers
            str_quals = type_elem.find(f"{{{NS_V8}}}StringQualifiers")
            if str_quals is not None:
                # Возвращаем как список (унификация)
                return [self._format_string_type_with_qualifiers(str_quals)]

            # Тип без квалификаторов - возвращаем как список (унификация)
            return [type_str]

    def _normalize_type_string(self, type_str: str) -> str:
        """
        Нормализует строку типа из XML в формат 1С.

        Примеры:
        - "xs:string" -> "Строка"
        - "xs:decimal" -> "Число"
        - "cfg:CatalogRef.Пользователи" -> "СправочникСсылка.Пользователи"
        """
        if not type_str:
            return ""

        # Убираем префиксы namespace
        if ":" in type_str:
            prefix, local = type_str.split(":", 1)

            # XML Schema типы
            if prefix == "xs":
                type_mapping = {
                    "string": "Строка",
                    "decimal": "Число",
                    "boolean": "Булево",
                    "dateTime": "Дата",
                }
                return type_mapping.get(local, local)

            # Платформенные типы v8 (объекты встроенного языка)
            if prefix == "v8":
                v8_type_mapping = {
                    "FixedStructure": "ФиксированнаяСтруктура",
                    "FixedArray": "ФиксированныйМассив",
                    "FixedMap": "ФиксированноеСоответствие",
                    "Structure": "Структура",
                    "Array": "Массив",
                    "Map": "Соответствие",
                    "ValueList": "СписокЗначений",
                    "ValueTable": "ТаблицаЗначений",
                    "TypeDescription": "ОписаниеТипов",
                    "AnyRef": "ЛюбаяСсылка",
                    "AnyIBRef": "ЛюбаяСсылка",
                    "UUID": "УникальныйИдентификатор",
                    "BinaryData": "ДвоичныеДанные",
                    "Undefined": "Неопределено",
                }
                return v8_type_mapping.get(local, local)

            # Типы конфигурации
            if prefix == "cfg":
                # CatalogRef.XXX -> СправочникСсылка.XXX
                type_mapping = {
                    "CatalogRef": "СправочникСсылка",
                    "DocumentRef": "ДокументСсылка",
                    "EnumRef": "ПеречислениеСсылка",
                    "ChartOfAccountsRef": "ПланСчетовСсылка",
                    "ChartOfCharacteristicTypesRef": "ПланВидовХарактеристикСсылка",
                    "ChartOfCalculationTypesRef": "ПланВидовРасчетаСсылка",
                    "BusinessProcessRef": "БизнесПроцессСсылка",
                    "TaskRef": "ЗадачаСсылка",
                    "ExchangePlanRef": "ПланОбменаСсылка",
                }

                for en, ru in type_mapping.items():
                    if local.startswith(en + "."):
                        return local.replace(en, ru)

                # Bare cfg: типы (без точки)
                cfg_bare_mapping = {
                    "AnyRef": "ЛюбаяСсылка",
                    "AnyIBRef": "ЛюбаяСсылка",
                }
                if local in cfg_bare_mapping:
                    return cfg_bare_mapping[local]

                return local

        return type_str

    def _format_number_type_with_qualifiers(self, num_quals_elem: ET.Element) -> str:
        """
        Форматирует числовой тип с квалификаторами.

        Пример: "Число(3, 0, Неотрицательный)"
        """
        digits = self._get_text(num_quals_elem.find(f"{{{NS_V8}}}Digits")) or "10"
        fraction = self._get_text(num_quals_elem.find(f"{{{NS_V8}}}FractionDigits")) or "0"
        allowed_sign = self._get_text(num_quals_elem.find(f"{{{NS_V8}}}AllowedSign"))

        # Перевод AllowedSign
        sign_mapping = {
            "Nonnegative": "Неотрицательный",
            "Any": "Любой",
        }
        sign_ru = sign_mapping.get(allowed_sign, allowed_sign) if allowed_sign else None

        if sign_ru:
            return f"Число({digits}, {fraction}, {sign_ru})"
        else:
            return f"Число({digits}, {fraction})"

    def _format_string_type_with_qualifiers(self, str_quals_elem: ET.Element) -> str:
        """
        Форматирует строковый тип с квалификаторами.

        Пример: "Строка(50, Переменная)"
        """
        length = self._get_text(str_quals_elem.find(f"{{{NS_V8}}}Length")) or "10"
        allowed_length = self._get_text(str_quals_elem.find(f"{{{NS_V8}}}AllowedLength"))

        # Перевод AllowedLength
        length_mapping = {
            "Variable": "Переменная",
            "Fixed": "Фиксированная",
        }
        length_ru = length_mapping.get(allowed_length, allowed_length) if allowed_length else None

        if length_ru:
            return f"Строка({length}, {length_ru})"
        else:
            return f"Строка({length})"

    def _extract_synonym_property(self, synonym_elem: ET.Element) -> str:
        """
        Извлекает многоязычный синоним.

        Берет значение для языка 'ru' или первое доступное.
        """
        # Ищем v8:item с v8:lang = 'ru'
        items = synonym_elem.findall(f"{{{NS_V8}}}item")

        for item in items:
            lang = self._get_text(item.find(f"{{{NS_V8}}}lang"))
            content = self._get_text(item.find(f"{{{NS_V8}}}content"))

            if lang == "ru" and content:
                return content

        # Если не нашли ru, берем первый доступный
        if items:
            content = self._get_text(items[0].find(f"{{{NS_V8}}}content"))
            if content:
                return content

        return ""

    def _translate_property_value(self, value: Any) -> Any:
        """
        Переводит значение свойства с английского на русский.

        Args:
            value: Значение (может быть строка, список, число и т.д.)

        Returns:
            Переведенное значение (если это строка из словаря) или исходное значение
        """
        # Если это строка - пытаемся перевести
        if isinstance(value, str):
            return PROPERTY_VALUES_TRANSLATION.get(value, value)

        # Если это список - переводим каждый элемент
        if isinstance(value, list):
            return [self._translate_property_value(item) for item in value]

        # Для остальных типов - возвращаем как есть
        return value

    @staticmethod
    def _local_name(tag: str) -> str:
        """Извлекает локальное имя тега без namespace"""
        if tag.startswith("{"):
            return tag.split("}", 1)[1]
        return tag

    @staticmethod
    def _get_text(elem: Optional[ET.Element]) -> str:
        """Извлекает текст из элемента или возвращает пустую строку"""
        if elem is None:
            return ""
        text = (elem.text or "").strip()
        return text
