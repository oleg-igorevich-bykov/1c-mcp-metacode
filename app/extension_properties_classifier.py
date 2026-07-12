"""
Классификатор свойств элементов расширений 1С.

Анализирует XML-файлы метаданных расширений и для каждого заимствованного элемента
(Attribute, TabularSection, Resource, Dimension) определяет:
- Контролируемые свойства (не изменялись)
- Модифицируемые свойства (изменялись в расширении)

Основан на анализе маркеров расширения в namespace xr (http://v8.1c.ru/8.3/xcf/readable).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Set
from pathlib import Path
import logging

from xml_metadata.property_names import translate_metadata_name

logger = logging.getLogger(__name__)

# Namespaces
NS_XR = "http://v8.1c.ru/8.3/xcf/readable"
NS_V8 = "http://v8.1c.ru/8.1/data/core"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"


# Свойства, которые НЕ анализируются (служебные/идентификаторы и коллекции)
EXCLUDED_PROPERTIES: Set[str] = {
    # Служебные свойства
    "Name", "ObjectBelonging", "ExtendedConfigurationObject",
    "InternalInfo", "Properties", "Comment",

    # Коллекции (не являются свойствами)
    "ChildObjects",
    "Attributes",
    "TabularSections",
    "Resources",
    "Dimensions",
    "Forms",
    "Commands",
    "Templates",
    "RegisterRecords",           # Регистры (движения документа)
    "StandardAttributes",        # Стандартные реквизиты
    "AddressingAttributes",      # Реквизиты адресации
    "Columns",                   # Колонки (для табличных частей)
}

# Свойства объектов метаданных, которые ВСЕГДА модифицируемые если присутствуют в XML
# (не могут быть контролируемыми по логике 1С)
ALWAYS_MODIFIED_OBJECT_PROPERTIES: Set[str] = {
    # Представления и синонимы
    "Synonym",                          # Синоним
    "ListPresentation",                 # Представление списка
    "ObjectPresentation",               # Представление объекта
    "ExtendedObjectPresentation",       # Расширенное представление объекта
    "ExtendedListPresentation",         # Расширенное представление списка
    "Explanation",                      # Пояснение
    "ToolTip",                          # Подсказка

    # Формы представления (отметки в конфигураторе)
    "DefaultObjectForm",                # Основная форма объекта
    "DefaultListForm",                  # Основная форма списка
    "DefaultChoiceForm",                # Основная форма выбора
    "DefaultFolderForm",                # Основная форма группы
    "DefaultFolderChoiceForm",          # Основная форма выбора группы
    "DefaultRecordForm",                # Основная форма записи
    "ObjectForm",                       # Форма объекта
    "ListForm",                         # Форма списка
    "ChoiceForm",                       # Форма выбора
    "FolderForm",                       # Форма группы
    "FolderChoiceForm",                 # Форма выбора группы
    "RecordForm",                       # Форма записи
}


@dataclass
class ElementClassification:
    """Результат классификации одного элемента (Attribute/TabularSection/Resource/Dimension)"""
    element_type: str  # "Attribute", "TabularSection", "Resource", "Dimension"
    element_name: str
    element_uuid: str
    is_adopted: bool  # True = Заимствованный, False = Собственный
    parent_name: Optional[str] = None  # Имя родителя (для реквизитов табличной части)

    # Только для заимствованных элементов:
    controlled_properties: List[str] = None  # Список свойств на русском языке
    modified_properties: List[str] = None    # Список свойств на русском языке

    def __post_init__(self):
        if self.controlled_properties is None:
            self.controlled_properties = []
        if self.modified_properties is None:
            self.modified_properties = []


@dataclass
class ObjectAnalysisResult:
    """Результат анализа одного XML-файла объекта метаданных"""
    object_type: str  # "Document", "Catalog", "InformationRegister", etc.
    object_name: str
    object_uuid: str
    elements: List[ElementClassification]
    xml_path: Optional[Path] = None


class ExtensionPropertiesClassifier:
    """
    Классификатор свойств элементов расширений.

    Анализирует XML-файлы метаданных расширений и определяет для каждого
    заимствованного элемента, какие свойства контролируемые, а какие модифицируемые.
    """

    def __init__(self):
        """Инициализация классификатора"""
        # Регистрируем все используемые namespaces для ElementTree
        self.namespaces = {
            "xr": NS_XR,
            "v8": NS_V8,
            "xsi": NS_XSI,
            "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
            "xs": "http://www.w3.org/2001/XMLSchema",
        }

    def analyze_metadata_xml(self, xml_path: Path) -> Optional[ObjectAnalysisResult]:
        """
        Анализирует один XML-файл метаданных расширения.

        Args:
            xml_path: Путь к XML-файлу (например, .../Documents/БольничныйЛист.xml)

        Returns:
            ObjectAnalysisResult или None в случае ошибки
        """
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()

            # Определяем тип объекта метаданных
            object_elem = self._find_metadata_object(root)
            if object_elem is None:
                # DEBUG уровень - показывается только при enable_debug=True
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

            # Анализируем сам объект метаданных (если он заимствованный)
            elements = []
            object_classification = self._classify_metadata_object(object_elem, object_type, object_name, object_uuid)
            if object_classification:
                elements.append(object_classification)

            # Анализируем ChildObjects
            child_objects = object_elem.find("{*}ChildObjects")

            if child_objects is not None:
                for elem in child_objects:
                    elem_type = self._local_name(elem.tag)

                    # Обрабатываем только нужные типы элементов
                    if elem_type in {
                        "Attribute", "TabularSection", "Resource", "Dimension",
                        "EnumValue", "Column", "URLTemplate", "AddressingAttribute",
                    }:
                        classification = self._classify_element(elem, elem_type, parent_name=None)
                        if classification:
                            elements.append(classification)

                        # Для TabularSection рекурсивно обрабатываем вложенные реквизиты
                        if elem_type == "TabularSection":
                            ts_name = self._get_text(elem.find(".//{*}Properties/{*}Name"))
                            ts_child_objects = elem.find("{*}ChildObjects")

                            if ts_child_objects is not None and ts_name:
                                for child_elem in ts_child_objects:
                                    child_type = self._local_name(child_elem.tag)

                                    if child_type == "Attribute":
                                        child_classification = self._classify_element(
                                            child_elem, child_type, parent_name=ts_name
                                        )
                                        if child_classification:
                                            elements.append(child_classification)

                        # Для URLTemplate рекурсивно обрабатываем вложенные Method
                        if elem_type == "URLTemplate":
                            url_template_name = self._get_text(elem.find(".//{*}Properties/{*}Name"))
                            url_template_children = elem.find("{*}ChildObjects")

                            if url_template_children is not None and url_template_name:
                                for method_elem in url_template_children:
                                    if self._local_name(method_elem.tag) == "Method":
                                        method_classification = self._classify_element(
                                            method_elem, "Method", parent_name=url_template_name
                                        )
                                        if method_classification:
                                            elements.append(method_classification)

            return ObjectAnalysisResult(
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
            logger.error(f"Ошибка анализа {xml_path}: {e}", exc_info=True)
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

    def _classify_metadata_object(
        self,
        object_elem: ET.Element,
        object_type: str,
        object_name: str,
        object_uuid: str
    ) -> Optional[ElementClassification]:
        """
        Классифицирует сам объект метаданных (Document, Catalog, и т.д.).

        Args:
            object_elem: XML-элемент объекта метаданных
            object_type: Тип объекта ("Document", "Catalog", и т.д.)
            object_name: Имя объекта
            object_uuid: UUID объекта

        Returns:
            ElementClassification с типом "MetadataObject" или None
        """
        props = object_elem.find("{*}Properties")
        if props is None:
            return None

        # Определяем: заимствованный или собственный
        obj_belonging = props.find("{*}ObjectBelonging")
        belonging_value = self._get_text(obj_belonging) if obj_belonging is not None else ""
        is_adopted = belonging_value == "Adopted"

        # Для собственных объектов не анализируем свойства
        if not is_adopted:
            return None

        # Для заимствованных объектов - классифицируем свойства
        # Используем InternalInfo/PropertyState для определения модифицированных свойств
        modified_properties_set = self._get_modified_properties_from_internal_info(object_elem)

        controlled = []
        modified = []

        # Анализируем все дочерние элементы Properties
        for prop_elem in props:
            prop_name_en = self._local_name(prop_elem.tag)

            # Пропускаем служебные свойства
            if prop_name_en in EXCLUDED_PROPERTIES:
                continue

            prop_name_ru = translate_metadata_name(prop_name_en, object_type=object_type)

            # Определяем: модифицируемое или контролируемое свойство
            # 1. Свойства из ALWAYS_MODIFIED_OBJECT_PROPERTIES всегда модифицируемые
            # 2. Свойства из InternalInfo/PropertyState всегда модифицируемые
            # 3. Свойства с xr: маркерами всегда модифицируемые
            # 4. Остальные - контролируемые
            if prop_name_en in ALWAYS_MODIFIED_OBJECT_PROPERTIES:
                # Свойство всегда модифицируемое (Synonym, ListPresentation и т.д.)
                modified.append(prop_name_ru)
            elif prop_name_en in modified_properties_set:
                # Свойство модифицировано (указано в InternalInfo)
                modified.append(prop_name_ru)
            elif self._has_xr_markers(prop_elem):
                # Свойство модифицировано (есть xr: маркеры)
                modified.append(prop_name_ru)
            else:
                # Свойство контролируемое
                controlled.append(prop_name_ru)

        # Свойства только в InternalInfo (нет XML-тега в Properties, но есть в xr:PropertyState)
        # Пример: Module у CommonModule, Rights у Role
        seen_prop_names = {self._local_name(p.tag) for p in props}
        for prop_name_en in sorted(modified_properties_set):
            if prop_name_en not in seen_prop_names and prop_name_en not in EXCLUDED_PROPERTIES:
                modified.append(translate_metadata_name(prop_name_en, object_type=object_type))

        return ElementClassification(
            element_type="MetadataObject",
            element_name=object_name,
            element_uuid=object_uuid,
            is_adopted=True,
            parent_name=None,
            controlled_properties=sorted(controlled),
            modified_properties=sorted(modified)
        )

    def _get_modified_properties_from_internal_info(self, object_elem: ET.Element) -> Set[str]:
        """
        Извлекает список модифицированных свойств из InternalInfo/PropertyState.

        Args:
            object_elem: XML-элемент объекта метаданных

        Returns:
            Множество имен модифицированных свойств (на английском)
        """
        modified = set()

        internal_info = object_elem.find("{*}InternalInfo")
        if internal_info is None:
            return modified

        # Ищем все xr:PropertyState
        for prop_state in internal_info.findall(f"{{{NS_XR}}}PropertyState"):
            # Ищем xr:Property и xr:State
            property_elem = prop_state.find(f"{{{NS_XR}}}Property")
            state_elem = prop_state.find(f"{{{NS_XR}}}State")

            if property_elem is not None and state_elem is not None:
                prop_name = self._get_text(property_elem)
                state = self._get_text(state_elem)

                # Если State = "Extended", то свойство модифицировано
                if state == "Extended" and prop_name:
                    modified.add(prop_name)

        return modified

    def _classify_element(self, elem: ET.Element, elem_type: str, parent_name: Optional[str] = None) -> Optional[ElementClassification]:
        """
        Классифицирует один элемент (Attribute/TabularSection/Resource/Dimension).

        Args:
            elem: XML-элемент
            elem_type: Тип элемента ("Attribute", "TabularSection", и т.д.)
            parent_name: Имя родителя (для реквизитов табличной части)

        Returns:
            ElementClassification или None
        """
        props = elem.find("{*}Properties")
        if props is None:
            return None

        elem_name = self._get_text(props.find("{*}Name"))
        elem_uuid = elem.get("uuid", "")

        if not elem_name:
            return None

        # Определяем: заимствованный или собственный
        # Проверяем ObjectBelonging для определения заимствованности
        obj_belonging = props.find("{*}ObjectBelonging")
        belonging_value = self._get_text(obj_belonging) if obj_belonging is not None else ""
        is_adopted = belonging_value == "Adopted"

        # Для собственных элементов не анализируем свойства
        if not is_adopted:
            return ElementClassification(
                element_type=elem_type,
                element_name=elem_name,
                element_uuid=elem_uuid,
                is_adopted=False,
                parent_name=parent_name
            )

        # Для заимствованных элементов - классифицируем свойства
        controlled = []
        modified = []

        # Анализируем все дочерние элементы Properties
        for prop_elem in props:
            prop_name_en = self._local_name(prop_elem.tag)

            # Пропускаем служебные свойства
            if prop_name_en in EXCLUDED_PROPERTIES:
                continue

            prop_name_ru = translate_metadata_name(prop_name_en)
            if self._has_xr_markers(prop_elem):
                modified.append(prop_name_ru)
            else:
                controlled.append(prop_name_ru)

        return ElementClassification(
            element_type=elem_type,
            element_name=elem_name,
            element_uuid=elem_uuid,
            is_adopted=True,
            parent_name=parent_name,
            controlled_properties=sorted(controlled),
            modified_properties=sorted(modified)
        )

    def _has_xr_markers(self, elem: ET.Element) -> bool:
        """
        Проверяет наличие маркеров расширения (xr:*) в элементе.

        Ищет:
        - Атрибуты xsi:type="xr:..."
        - Дочерние элементы из namespace xr (xr:CheckValue, xr:ExtendValue, и т.д.)

        Args:
            elem: XML-элемент для проверки

        Returns:
            True если найдены маркеры расширения
        """
        # Проверяем атрибут xsi:type
        xsi_type = elem.get(f"{{{NS_XSI}}}type", "")
        if xsi_type.startswith("xr:"):
            return True

        # Проверяем все дочерние элементы (включая вложенные)
        for child in elem.iter():
            # Проверяем namespace элемента
            if child.tag.startswith(f"{{{NS_XR}}}"):
                return True

            # Проверяем атрибуты дочерних элементов
            child_xsi_type = child.get(f"{{{NS_XSI}}}type", "")
            if child_xsi_type.startswith("xr:"):
                return True

        return False

    def _is_collection_modified(self, collection_elem: ET.Element) -> bool:
        """
        Проверяет, модифицирована ли коллекция (ChildObjects).

        Коллекция считается модифицированной если:
        - Есть xr:* маркеры на самой коллекции
        - Есть хотя бы один добавленный (не заимствованный) дочерний элемент
        - Хотя бы один дочерний элемент имеет xr:* маркеры

        Args:
            collection_elem: XML-элемент коллекции (ChildObjects)

        Returns:
            True если коллекция модифицирована
        """
        # Проверяем саму коллекцию
        if self._has_xr_markers(collection_elem):
            return True

        # Проверяем дочерние элементы
        for child in collection_elem:
            child_props = child.find("{*}Properties")
            if child_props is None:
                continue

            # Проверяем: заимствованный или добавленный
            ext_config_obj = child_props.find("{*}ExtendedConfigurationObject")
            if ext_config_obj is None:
                # Добавленный элемент - коллекция модифицирована
                return True

            # Проверяем наличие xr:* в свойствах заимствованного элемента
            for prop in child_props:
                if self._has_xr_markers(prop):
                    return True

        return False

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
