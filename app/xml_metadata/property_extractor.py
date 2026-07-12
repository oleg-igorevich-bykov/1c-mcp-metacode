from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Tuple

from .rules import (
    CFG_TYPE_PREFIX_TO_RU,
    CFG_TYPE_VALUE_TO_RU,
    EN_SINGULAR_TO_RU_SINGULAR,
    METADATA_SEGMENT_TO_RU,
    STANDARD_ATTR_NAME,
    VALUE_TRANSLATIONS,
    XML_PROP_TO_RU,
)









GUIDISH_RE = re.compile(r"\b[0-9a-fA-F]:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
PLAIN_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


MOBILE_FUNCTIONALITY_TO_RU = {
    "Biometrics": "Биометрия",
    "Location": "Геопозиционирование",
    "BackgroundLocation": "ГеопозиционированиеВФоновомРежиме",
    "BluetoothPrinters": "BluetoothПринтеры",
    "WiFiPrinters": "WiFiПринтеры",
    "Contacts": "Контакты",
    "Calendars": "Календари",
    "PushNotifications": "PushУведомления",
    "LocalNotifications": "ЛокальныеУведомления",
    "InAppPurchases": "ВстроенныеПокупки",
    "PersonalComputerFileExchange": "ОбменФайламиСПерсональнымКомпьютером",
    "Ads": "Реклама",
    "NumberDialing": "НаборНомера",
    "CallProcessing": "ОбработкаЗвонков",
    "CallLog": "ЖурналЗвонков",
    "AutoSendSMS": "АвтоматическаяОтправкаSMS",
    "ReceiveSMS": "ПолучениеSMS",
    "SMSLog": "ЖурналSMS",
    "Camera": "Камера",
    "Microphone": "Микрофон",
    "MusicLibrary": "БиблиотекаМузыки",
    "PictureAndVideoLibraries": "БиблиотекиКартинокИВидео",
    "AudioPlaybackAndVibration": "ВоспроизведениеАудиоИВибрация",
    "BackgroundAudioPlaybackAndVibration": "ВоспроизведениеАудиоИВибрацияВФоновомРежиме",
    "InstallPackages": "УстановкаПриложений",
    "OSBackup": "РезервноеКопированиеСредствамиОС",
    "ApplicationUsageStatistics": "СтатистикаИспользованияПриложения",
    "BarcodeScanning": "СканированиеШтрихКодов",
    "BackgroundAudioRecording": "ЗаписьАудиоВФоновомРежиме",
    "AllFilesAccess": "ДоступКоВсемФайлам",
    "Videoconferences": "Видеоконференции",
    "NFC": "NFC",
    "DocumentScanning": "СканированиеДокументов",
    "SpeechToText": "РаспознаваниеРечи",
    "Geofences": "Геозоны",
    "IncomingShareRequests": "ВходящиеЗапросыПоделиться",
    "AllIncomingShareRequestsTypesProcessing": "ОбработкаВсехТиповВходящихЗапросовПоделиться",
    "TextToSpeech": "СинтезРечи",
}


def local_name(tag: str) -> str:
    if not tag:
        return tag
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def first_child(elem: ET.Element | None, name: str) -> ET.Element | None:
    if elem is None:
        return None
    for child in list(elem):
        if isinstance(child.tag, str) and local_name(child.tag) == name:
            return child
    return None


def children(elem: ET.Element | None, name: str | None = None) -> List[ET.Element]:
    if elem is None:
        return []
    out = []
    for child in list(elem):
        if isinstance(child.tag, str) and (name is None or local_name(child.tag) == name):
            out.append(child)
    return out


def text(elem: ET.Element | None) -> str:
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def attr_by_local_name(elem: ET.Element | None, name: str) -> str:
    if elem is None:
        return ""
    for key, value in elem.attrib.items():
        if key == name or key.endswith("}" + name):
            return value
    return ""


def translate_value(value: str) -> str:
    return VALUE_TRANSLATIONS.get(value or "", value or "")


def local_string(elem: ET.Element) -> str:
    first_content = None
    seen_content = False
    for item in elem.iter():
        if item is elem or not isinstance(item.tag, str):
            continue
        if local_name(item.tag) != "item":
            continue
        lang_val = ""
        content_val = ""
        for sub in list(item):
            if not isinstance(sub.tag, str):
                continue
            ln = local_name(sub.tag)
            if ln == "lang":
                lang_val = (sub.text or "").strip()
            elif ln == "content":
                seen_content = True
                content_val = (sub.text or "").strip("\n\t")
        if not content_val:
            continue
        if lang_val == "ru":
            return content_val
        if first_content is None:
            first_content = content_val
    if first_content is None:
        for child in elem.iter():
            if isinstance(child.tag, str) and local_name(child.tag) == "content":
                seen_content = True
                value = (child.text or "").strip("\n\t")
                if value:
                    first_content = value
                    break
    if first_content is not None:
        return first_content
    if seen_content:
        return ""
    return text(elem)


def raw_text_preserve_spaces(elem: ET.Element | None) -> str | None:
    if elem is None:
        return None
    for child in elem.iter():
        if isinstance(child.tag, str) and local_name(child.tag) == "content":
            value = child.text
            if value is not None:
                return value.strip("\n\t")
    if elem.text is None:
        return None
    return elem.text.strip("\n\t")


def map_cfg_type(value: str) -> str:
    value = (value or "").strip()
    if value in CFG_TYPE_VALUE_TO_RU:
        return CFG_TYPE_VALUE_TO_RU[value]
    if value.startswith("cfg:"):
        value = value[4:]
    if value in CFG_TYPE_VALUE_TO_RU:
        return CFG_TYPE_VALUE_TO_RU[value]
    if value in CFG_TYPE_PREFIX_TO_RU:
        return CFG_TYPE_PREFIX_TO_RU[value]
    if "." not in value:
        return value
    head, tail = value.split(".", 1)
    mapped = CFG_TYPE_PREFIX_TO_RU.get(head)
    return f"{mapped}.{tail}" if mapped else value


def map_register_record(value: str) -> str:
    value = (value or "").strip()
    if "." not in value:
        return value
    head, tail = value.split(".", 1)
    mapped = EN_SINGULAR_TO_RU_SINGULAR.get(head)
    return f"{mapped}.{tail}" if mapped else value


def format_metadata_path(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return ".".join(
        EN_SINGULAR_TO_RU_SINGULAR.get(part, METADATA_SEGMENT_TO_RU.get(part, STANDARD_ATTR_NAME.get(part, translate_value(part))))
        for part in value.split(".")
    )


def last_metadata_part(value: str | None) -> str:
    if not value:
        return ""
    parts = value.split(".")
    if len(parts) >= 2 and parts[-2] == "StandardAttribute":
        return STANDARD_ATTR_NAME.get(parts[-1], translate_value(parts[-1]))
    return translate_value(parts[-1])


def compact_data_path(value: str) -> str:
    value = (value or "").strip().split()[0] if (value or "").strip() else ""
    parts = [part for part in value.split(".") if part]
    if "StandardAttribute" in parts:
        return last_metadata_part(value)
    if "TabularSection" in parts and "Attribute" in parts:
        section_index = parts.index("TabularSection")
        attribute_index = parts.index("Attribute")
        if section_index + 1 < len(parts) and attribute_index + 1 < len(parts):
            return f"{parts[section_index + 1]}.{parts[attribute_index + 1]}"
    for marker in ("Attribute", "Dimension", "Resource"):
        if marker in parts:
            marker_index = parts.index(marker)
            if marker_index + 1 < len(parts):
                return parts[marker_index + 1]
    return format_metadata_path(value)


def child_text(elem: ET.Element | None, child_name: str) -> str:
    return text(first_child(elem, child_name))


def format_type_value(value: str, qualifiers: Dict[str, ET.Element]) -> str:
    base = map_cfg_type(value)
    if value == "xs:string":
        qualifier = qualifiers.get("StringQualifiers")
        length = child_text(qualifier, "Length")
        allowed = child_text(qualifier, "AllowedLength")
        parts = [part for part in (length, translate_value(allowed) if allowed else None) if part]
        return f"{base}({', '.join(parts)})" if parts else base
    if value == "xs:decimal":
        qualifier = qualifiers.get("NumberQualifiers")
        precision = child_text(qualifier, "Digits") or child_text(qualifier, "Precision")
        scale = child_text(qualifier, "FractionDigits") or child_text(qualifier, "Scale")
        sign = child_text(qualifier, "NonNegative") or child_text(qualifier, "AllowedSign")
        parts = [part for part in (precision, scale, translate_value(sign) if sign and sign != "Any" else None) if part]
        return f"{base}({', '.join(parts)})" if parts else base
    if value == "xs:dateTime":
        fraction = child_text(qualifiers.get("DateQualifiers"), "DateFractions")
        return f"{base}({translate_value(fraction)})" if fraction else base
    return base


def type_values(type_elem: ET.Element | None) -> List[str]:
    if type_elem is None:
        return []
    qualifiers: Dict[str, ET.Element] = {}
    for child in type_elem.iter():
        name = local_name(child.tag) if isinstance(child.tag, str) else ""
        if name in {"StringQualifiers", "NumberQualifiers", "DateQualifiers"} and name not in qualifiers:
            qualifiers[name] = child
    values = []
    for child in type_elem.iter():
        if child is type_elem or not isinstance(child.tag, str) or local_name(child.tag) not in {"Type", "TypeSet"}:
            continue
        value = text(child)
        if value:
            values.append(format_type_value(value, qualifiers))
    direct = text(type_elem)
    if not values and direct:
        values.append(format_type_value(direct, qualifiers))
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def prop_value(prop: ET.Element) -> Any:
    name = local_name(prop.tag)
    if name in {"Synonym", "Comment", "ToolTip", "Format", "EditFormat", "Explanation"}:
        return local_string(prop)
    if name == "Mask":
        return text(prop)
    if name == "Type":
        return type_values(prop)
    if name in {"RegisterRecords", "Owners", "RegisteredDocuments", "Documents"}:
        return [map_register_record(text(item)) for item in children(prop, "Item") if text(item)]
    if name in {"DocumentMap", "RegisterRecordsMap"}:
        values = [format_metadata_path(text(item)) for item in prop.iter() if isinstance(item.tag, str) and local_name(item.tag) == "Item" and text(item)]
        if values:
            return values
        return "" if len(list(prop)) == 0 else None
    if name in {"AccountingFlag", "ExtDimensionAccountingFlag"}:
        return format_metadata_path(text(prop))
    if name == "BaseCalculationTypes":
        return [format_metadata_path(text(item)) for item in children(prop, "Item") if text(item)]
    if name == "ChoiceParameterLinks":
        return choice_parameter_links(prop)
    if name == "UsedMobileApplicationFunctionalities":
        return mobile_application_functionalities(prop)
    if name == "ChoiceParameters":
        return choice_parameters(prop)
    if name == "References":
        return metadata_reference_values(prop)
    if name == "XDTOPackages":
        values = []
        for child in prop.iter():
            if child is prop or not isinstance(child.tag, str):
                continue
            if local_name(child.tag) == "Value":
                item_value = text(child)
                if item_value.startswith("XDTOPackage."):
                    item_value = "НеизвестныйОбъект"
                if item_value:
                    values.append(item_value)
        return values
    if name == "Content":
        values = []
        for item in children(prop, "Item"):
            metadata = child_text(item, "Metadata")
            use = child_text(item, "Use")
            if metadata:
                value = format_metadata_path(metadata)
                if use:
                    value = f"{value}, {translate_value(use)}"
            else:
                raw_item = text(item)
                if PLAIN_UUID_RE.fullmatch(raw_item):
                    continue
                value = format_metadata_path(raw_item)
            if value and value not in values:
                values.append(value)
        for obj in children(prop, "Object"):
            value = format_metadata_path(text(obj))
            if value and value not in values:
                values.append(value)
        return values
    if name == "BasedOn":
        return [format_metadata_path(text(item)) for item in children(prop, "Item") if text(item)]
    if name in {"Location", "Addressing", "MainAddressingAttribute", "CurrentPerformer", "DataSeparationValue", "DataSeparationUse", "ConditionalSeparation", "ChartOfAccounts", "ExtDimensionTypes"}:
        return format_metadata_path(text(prop))
    if name == "Handler":
        value = format_metadata_path(text(prop))
        return value.replace("ОбщийМодуль.", "", 1)
    if name == "MethodName":
        value = format_metadata_path(text(prop))
        return value.replace("ОбщийМодуль.", "", 1)
    if name == "Source":
        return type_values(prop)
    if name == "CommandParameterType":
        values = type_values(prop)
        if values:
            return values
        return map_cfg_type(text(prop))
    if name in {"InputByString", "DataLockFields"}:
        values = []
        for child in prop.iter():
            if child is prop or not isinstance(child.tag, str) or local_name(child.tag) != "Field":
                continue
            raw_value = text(child)
            value = "ВедущаяЗадача" if raw_value.endswith(".StandardAttribute.HeadTask") else compact_data_path(raw_value)
            if value and value not in values:
                values.append(value)
        return values
    if name in {
        "DefaultObjectForm",
        "DefaultFolderForm",
        "DefaultListForm",
        "DefaultChoiceForm",
        "DefaultFolderChoiceForm",
        "DefaultForm",
        "DefaultReportForm",
        "DefaultSettingsForm",
        "DefaultSaveForm",
        "DefaultLoadForm",
        "AuxiliarySaveForm",
        "AuxiliaryLoadForm",
        "AuxiliarySettingsForm",
        "DefaultVariantForm",
        "MainDataCompositionSchema",
        "VariantsStorage",
        "SettingsStorage",
        "AuxiliaryForm",
        "Numerator",
        "AuxiliaryObjectForm",
        "AuxiliaryFolderForm",
        "AuxiliaryListForm",
        "AuxiliaryChoiceForm",
        "AuxiliaryFolderChoiceForm",
        "DefaultRecordForm",
        "AuxiliaryRecordForm",
        "Task",
        "Schedule",
        "ScheduleValue",
        "ScheduleDate",
        "ChartOfCalculationTypes",
        "CharacteristicExtValues",
    }:
        return format_metadata_path(text(prop))
    if name == "Group":
        value = text(prop)
        if value.startswith("CommandGroup."):
            return "ГруппаКоманд." + value.split(".", 1)[1]
        return translate_value(value)
    if name in {"ChoiceForm", "LinkByType", "ScheduleLink"}:
        return format_metadata_path(text(prop))
    if name == "Use" and "." in text(prop):
        values = [format_metadata_path(item) for item in text(prop).split() if item.strip()]
        return values[0] if len(values) == 1 else values
    if name == "RestartIntervalOnFailure" and re.fullmatch(r"-?\d+", text(prop) or ""):
        value = text(prop)
        sign = "-" if value.startswith("-") else ""
        digits = value[1:] if sign else value
        groups = []
        while len(digits) > 3:
            groups.insert(0, digits[-3:])
            digits = digits[:-3]
        groups.insert(0, digits)
        return sign + "\u00a0".join(groups)
    if name == "FillValue":
        return fill_value(prop)
    localized = local_string(prop) if list(prop) else ""
    if localized and "\n" not in localized:
        return localized
    if list(prop):
        values = []
        for child in list(prop):
            if isinstance(child.tag, str) and local_name(child.tag) in {"Item", "Type", "Value"}:
                value = text(child) or attr_by_local_name(child, "ref") or attr_by_local_name(child, "value")
                if value:
                    values.append(value)
        if values:
            return values
    return translate_value(text(prop))


def fill_value(prop: ET.Element) -> str:
    if attr_by_local_name(prop, "nil") == "true":
        return None
    value = text(prop)
    value_type = attr_by_local_name(prop, "type")
    if value.endswith(".EmptyRef") or value.endswith(".00000000-0000-0000-0000-000000000000"):
        return ""
    if value_type == "xs:string":
        raw_value = prop.text or ""
        if raw_value == "":
            return ""
        return f"Строка:{raw_value}"
    if value_type == "xs:decimal":
        if value == "0":
            return ""
        if re.fullmatch(r"-?\d+", value or ""):
            sign = "-" if value.startswith("-") else ""
            digits = value[1:] if sign else value
            groups = []
            while len(digits) > 3:
                groups.insert(0, digits[-3:])
                digits = digits[:-3]
            groups.insert(0, digits)
            value = sign + "\u00a0".join(groups)
        return f"Число:{value}"
    if value_type == "xs:boolean":
        if value == "false":
            return ""
        return f"Булево:{translate_value(value)}"
    if value_type == "xs:dateTime":
        if value == "0001-01-01T00:00:00":
            return ""
        if "T" in value:
            date_part, time_part = value.split("T", 1)
            y, m, d = date_part.split("-", 2)
            hh, mm, ss = (time_part.split(":", 2) + ["00", "00"])[:3]
            ss = ss.split(".", 1)[0]
            return f"Дата:{int(d):02d}.{int(m):02d}.{y} {int(hh)}:{int(mm):02d}:{int(ss):02d}"
        return f"Дата:{value}"
    if value_type.endswith("DesignTimeRef"):
        if all(PLAIN_UUID_RE.fullmatch(part) for part in value.split(".") if part):
            return ""
        return format_design_time_ref(value)
    return translate_value(value)


def format_design_time_ref(value: str) -> str:
    parts = [part for part in (value or "").split(".") if part]
    if GUIDISH_RE.search(value or "") or (parts and all(PLAIN_UUID_RE.fullmatch(part) for part in parts)):
        return ""
    if len(parts) >= 4 and parts[2] == "EnumValue":
        return f"ПеречислениеСсылка.{parts[1]}:{parts[3]}"
    if len(parts) >= 3 and parts[2] == "EmptyRef":
        prefix = {
            "Enum": "ПеречислениеСсылка",
            "Catalog": "СправочникСсылка",
            "Document": "ДокументСсылка",
            "ChartOfCharacteristicTypes": "ПланВидовХарактеристикСсылка",
            "ChartOfCalculationTypes": "ПланВидовРасчетаСсылка",
            "ChartOfAccounts": "ПланСчетовСсылка",
        }.get(parts[0])
        if prefix:
            return f"{prefix}.{parts[1]}:"
    if len(parts) >= 3 and parts[0] == "Catalog":
        return f"СправочникСсылка.{parts[1]}:{parts[-1]}"
    if len(parts) >= 3 and parts[0] == "Document":
        return f"ДокументСсылка.{parts[1]}:{parts[-1]}"
    if len(parts) >= 3 and parts[0] == "ChartOfCharacteristicTypes":
        return f"ПланВидовХарактеристикСсылка.{parts[1]}:{parts[-1]}"
    if len(parts) >= 3 and parts[0] == "ChartOfAccounts":
        return f"ПланСчетовСсылка.{parts[1]}:{parts[-1]}"
    return format_metadata_path(value)


def choice_parameter_value(node: ET.Element | None) -> Tuple[str, str]:
    if node is None:
        return "", ""
    if attr_by_local_name(node, "nil") == "true":
        return "Неопределено", ""
    value_type = attr_by_local_name(node, "type")
    value = text(node)
    if value_type.endswith("DesignTimeRef"):
        return "", format_design_time_ref(value)
    if value_type.endswith("FixedArray"):
        return "ФиксированныйМассив", "ФиксированныйМассив"
    return map_cfg_type(value_type), translate_value(value)


def choice_parameters(node: ET.Element | None) -> List[str]:
    result = []
    for item in children(node):
        name = item.attrib.get("name") or item.attrib.get("Name") or attr_by_local_name(item, "name") or child_text(item, "Name")
        value_node = first_child(item, "value")
        type_label, value = choice_parameter_value(value_node)
        value_type = attr_by_local_name(value_node, "type") if value_node is not None else ""
        if name and (value or type_label or value_type.endswith("DesignTimeRef")):
            result.append(f"{name}({type_label}:{value})" if type_label else f"{name}({value})")
    return result


def mobile_application_functionalities(node: ET.Element | None) -> str:
    if node is None:
        return ""
    lines = []
    for item in children(node):
        raw_name = child_text(item, "functionality")
        raw_use = child_text(item, "use")
        if not raw_name:
            continue
        ru_name = MOBILE_FUNCTIONALITY_TO_RU.get(raw_name, raw_name)
        ru_use = translate_value(raw_use) if raw_use else ""
        lines.append(f"{ru_name} = {ru_use}")
    if not lines:
        return ""
    return "Функциональность:\n" + "\n".join(lines)


def choice_parameter_links(node: ET.Element | None) -> Any:
    result = []
    leading_blank_seen = False
    leading_skipped_guid_seen = False
    for link in children(node):
        name = child_text(link, "Name")
        data_path = child_text(link, "DataPath")
        if not name or not data_path:
            if not result:
                leading_blank_seen = True
            continue
        if GUIDISH_RE.search(name) or GUIDISH_RE.search(data_path):
            if not result:
                leading_skipped_guid_seen = True
            continue
        if "." in name and data_path.strip().startswith("Отбор."):
            result.append(f"{data_path}({compact_data_path(name)})")
        else:
            result.append(f"{name}({compact_data_path(data_path)})")
    if (leading_blank_seen or leading_skipped_guid_seen) and result:
        result.insert(0, "")
    if (leading_blank_seen or leading_skipped_guid_seen) and not result:
        return "__EMPTY_CHOICE_LINKS__"
    return result


def metadata_reference_values(node: ET.Element | None) -> List[str]:
    result = []
    for child in node.iter() if node is not None else []:
        if child is node or not isinstance(child.tag, str):
            continue
        if local_name(child.tag) not in {"Type", "TypeSet", "Item", "Value", "Metadata", "Field"}:
            continue
        value = text(child) or attr_by_local_name(child, "ref") or attr_by_local_name(child, "value")
        if value:
            formatted = format_metadata_path(value)
            if formatted not in result:
                result.append(formatted)
    return result


def split_top_level_csv(value: str) -> List[str]:
    parts = []
    current = []
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
            continue
        current.append(char)
    item = "".join(current).strip()
    if item:
        parts.append(item)
    return parts


def properties_ru(props_elem: ET.Element | None) -> Dict[str, Any]:
    props: Dict[str, Any] = {}
    for prop in children(props_elem):
        if attr_by_local_name(prop, "nil") == "true":
            continue
        xml_name = local_name(prop.tag)
        ru_name = XML_PROP_TO_RU.get(xml_name)
        if ru_name:
            value = prop_value(prop)
            if ru_name in {"МинимальноеЗначение", "МаксимальноеЗначение"} and value == "":
                continue
            if ru_name == "ПринадлежностьОбъекта" and value in ("", None):
                value = "Собственный"
            elif ru_name == "ОбъектРасширяемойКонфигурации" and value is None:
                value = ""
            elif ru_name == "ИспользованиеХраненияВХранилищеДвоичныхДанных" and value in ("", None):
                value = "Использовать"
            elif ru_name == "ПолеИспользованияХраненияВХранилищеДвоичныхДанных" and value is None:
                value = ""
            elif ru_name == "ИзмерениеАдресации" and isinstance(value, str):
                value = format_metadata_path(value)
            elif ru_name == "РежимУправленияБлокировкойДанных" and value == "Управляемая":
                value = "Управляемый"
            if value is not None:
                props[ru_name] = value
    return props


def strip_txt_quotes(value: str) -> str:
    return value.strip('"')


def legacy_type_value(value: Any) -> Any:
    if not isinstance(value, list):
        text_value = strip_txt_quotes(str(value)).strip()
        if not text_value:
            return None
        if "," in text_value:
            return split_top_level_csv(" ".join(text_value.split()))
        return [text_value]
    values = [strip_txt_quotes(str(item)).strip() for item in value if str(item).strip()]
    values = [
        "КонстантыНабор" if item == "НаборКонстант" else "Диаграмма" if item == "d7p1:Chart" else item
        for item in values
    ]
    if not values:
        return None
    if len(values) > 1:
        indexed = list(enumerate(values))

        has_primitive = any(
            current.rstrip(",").startswith(("Строка", "Булево", "Дата", "Число"))
            for current in values
        )
        has_specific_ref = any(
            "." in current.rstrip(",") and current.rstrip(",").split(".", 1)[0].endswith("Ссылка")
            for current in values
        )

        def type_order(item: tuple[int, str]) -> tuple[int, int]:
            idx, current = item
            base = current.rstrip(",")
            if has_specific_ref:
                specific_ref_order = {
                    "ПланОбменаСсылка": -900,
                    "БизнесПроцессСсылка": -800,
                    "ДокументСсылка": -790,
                    "ПеречислениеСсылка": -780,
                    "ПланВидовРасчетаСсылка": -770,
                    "ЗадачаСсылка": -760,
                    "ПланВидовХарактеристикСсылка": -750,
                    "ПланСчетовСсылка": -740,
                    "СправочникСсылка": -730,
                }
                return (specific_ref_order.get(base, 0), idx)
            if base == "ФиксированныйМассив":
                return (-950, idx)
            if base == "ЛюбаяСсылка":
                return (-1000, idx)
            if base == "ПланОбменаСсылка":
                return (-900, idx)
            ref_order = {
                "БизнесПроцессСсылка": -800,
                "ДокументСсылка": -790,
                "ПеречислениеСсылка": -780,
                "ПланВидовРасчетаСсылка": -770,
                "ЗадачаСсылка": -760,
                "ПланВидовХарактеристикСсылка": -750,
                "ПланСчетовСсылка": -740,
                "СправочникСсылка": -730,
            }
            if has_primitive and base.startswith(("Строка", "Булево", "Дата", "Число")):
                return (-755, idx)
            if base in ref_order:
                return (ref_order[base], idx)
            return (0, idx)

        values = [item for _, item in sorted(indexed, key=type_order)]
    if len(values) == 1:
        item = values[0]
        if "," in item:
            return [part.strip() for part in " ".join(item.split()).split(",") if part.strip()]
        return values
    return values


def compat_choice_link_item(item: str) -> str:
    if not item.endswith(")") or "(" not in item:
        return item
    open_index = item.find("(")
    left, right = item[:open_index], item[open_index + 1:-1]
    if left.endswith(".StandardAttribute.Owner") and right.startswith("Отбор."):
        return f"{right}(Владелец)"
    if right.startswith(("Строка:", "Булево:", "Число:", "Дата:")):
        return item
    compact_right = compact_data_path(right)
    return f"{left}({compact_right})" if compact_right != right else item


def legacy_comma_multiline_value(value: Any) -> Any:
    if not isinstance(value, list):
        return compat_choice_link_item(strip_txt_quotes(str(value)))
    raw_values = [compat_choice_link_item(strip_txt_quotes(str(item))) for item in value]
    keep_leading_blank = any(str(item).strip() in {"", ","} for item in raw_values)
    values = [item for item in raw_values if item and item != ","]
    if not values:
        return ""
    if keep_leading_blank:
        values.insert(0, ",")
    if len(values) == 1:
        return values[0]
    return [f"{item}," if idx < len(values) - 1 and not item.endswith(",") else item for idx, item in enumerate(values)]


def legacy_single_or_list_value(value: Any) -> Any:
    if not isinstance(value, list):
        return strip_txt_quotes(str(value))
    values = [strip_txt_quotes(str(item)) for item in value if str(item) != ""]
    if not values:
        return ""
    return values[0] if len(values) == 1 else values


def legacy_property_value(name: str, value: Any) -> Any:
    if name == "ТипПараметраКоманды":
        if value in (None, "", []):
            return ""
        normalized = legacy_type_value(value)
        if isinstance(normalized, list) and len(normalized) == 1:
            return normalized[0]
        return normalized
    if name == "Тип":
        return legacy_type_value(value)
    if name == "Источник":
        normalized = legacy_type_value(value)
        suppressed_source_types = {
            "БизнесПроцессОбъект",
            "ПланВидовХарактеристикОбъект",
            "ПланВидовРасчетаОбъект",
            "ЗадачаОбъект",
            "РегистрРасчетаНаборЗаписей",
            "RecalculationRecordSet",
        }
        suppressed_source_prefixes = {
            "ChartOfAccountsManager",
            "BusinessProcessManager",
            "РегистрРасчетаМенеджер",
            "BusinessProcessManager",
            "РегистрБухгалтерииМенеджер",
            "ChartOfCalculationTypesManager",
            "TaskManager",
            "ChartOfCharacteristicTypesManager",
        }
        if isinstance(normalized, list):
            normalized = [
                ("," if str(item).endswith(",") else "")
                if str(item).rstrip(",") in suppressed_source_types
                or str(item).rstrip(",") in suppressed_source_prefixes
                or any(str(item).rstrip(",").startswith(prefix + ".") for prefix in suppressed_source_prefixes)
                else item
                for item in normalized
            ]
            while normalized and normalized[-1] == "":
                normalized.pop()
            if not normalized:
                normalized = ""
            elif not any("," in str(item) for item in normalized) and any(str(item).rstrip(",") == "ПланОбменаОбъект" for item in normalized):
                exchange_items = [item for item in normalized if str(item).rstrip(",") == "ПланОбменаОбъект"]
                other_items = [item for item in normalized if str(item).rstrip(",") != "ПланОбменаОбъект"]
                normalized = other_items[:1] + exchange_items + other_items[1:]
                normalized = [
                    f"{str(item).rstrip(',')}," if idx < len(normalized) - 1 else str(item).rstrip(",")
                    for idx, item in enumerate(normalized)
                ]
        elif str(normalized).rstrip(",") in suppressed_source_types or str(normalized).rstrip(",") in suppressed_source_prefixes or any(
            str(normalized).rstrip(",").startswith(prefix + ".") for prefix in suppressed_source_prefixes
        ):
            normalized = ""
        if isinstance(normalized, list) and len(normalized) == 1:
            return normalized[0]
        return normalized
    if name in {"ПараметрыВыбора", "СвязиПараметровВыбора"}:
        if value == "__EMPTY_CHOICE_LINKS__":
            return ""
        return legacy_comma_multiline_value(value)
    if name == "Ссылки":
        return legacy_single_or_list_value(value)
    if name == "ФормаВыбора" and isinstance(value, str):
        return format_metadata_path(value)
    if name == "НазначенияИспользования":
        if isinstance(value, list):
            return ", ".join(translate_value(str(item)) for item in value)
        if isinstance(value, str):
            return translate_value(value)
    if name in {"ПоляБлокировкиДанных", "ВводПоСтроке"}:
        return legacy_comma_multiline_value(value)
    if name == "ПакетыXDTO":
        if isinstance(value, list):
            return ", ".join(strip_txt_quotes(str(item)) for item in value if str(item).strip())
        return strip_txt_quotes(str(value))
    if name in {"ВводитсяНаОсновании", "Движения", "Состав", "Владельцы", "БазовыеВидыРасчета"}:
        return legacy_single_or_list_value(value)
    if isinstance(value, str):
        text_value = strip_txt_quotes(value).replace(" \n", "\n")
        text_value = text_value.replace('\n"', "\n")
        text_value = re.sub(r" {2,}\n", "\n", text_value)
        text_value = re.sub(r"\n[ \t]{2,}", "\n", text_value)
        return text_value
    if isinstance(value, list):
        return [strip_txt_quotes(item) if isinstance(item, str) else item for item in value]
    return value


def normalize_legacy_tooltip(value: Any, type_key: str | None = None) -> Any:
    if type_key not in {"attribute", "dimension", "resource", "tabular_section_attribute"} or not isinstance(value, str):
        return value
    lines = value.splitlines()
    dropped_password_intro = False
    if len(lines) > 1 and lines[0].startswith((
        "Способ входа в приложение с помощью имени и пароля,",
        "Программа электронной подписи ",
        "Представление пользователя ",
        "Используется при формировании печатных форм",
    )):
        dropped_password_intro = lines[0].startswith("Способ входа в приложение с помощью имени и пароля,")
        lines = lines[1:]
    if lines and lines[0].startswith("- "):
        lines = [line for line in lines if line.strip()]
    normalized_lines = []
    for line in lines:
        if line.startswith('"Нет" - '):
            line = line[1:]
        line = line.replace('коллекция "Справочники"', 'коллекция "Справочники')
        normalized_lines.append(line)
    if dropped_password_intro and len(normalized_lines) > 1:
        return normalized_lines
    return "\n".join(normalized_lines)


def legacy_props_compat(props: Dict[str, Any]) -> Dict[str, Any]:
    keep_empty_choice_links = props.get("СвязиПараметровВыбора") == "__EMPTY_CHOICE_LINKS__"
    out = {name: legacy_property_value(name, value) for name, value in props.items()}
    out.pop("ScheduleLink", None)
    if out.get("Использование") == "Использовать":
        out.pop("Использование", None)
    if out.get("СвязьПоТипу") == "":
        out.pop("СвязьПоТипу", None)
    for list_name in ("ПараметрыВыбора", "СвязиПараметровВыбора"):
        if out.get(list_name) == "" and not (list_name == "СвязиПараметровВыбора" and keep_empty_choice_links):
            out.pop(list_name, None)
    return out


def overlay_raw_properties(elem: ET.Element, props: Dict[str, Any]) -> None:
    props_elem = first_child(elem, "Properties")
    if props_elem is None:
        return
    comment = raw_text_preserve_spaces(first_child(props_elem, "Comment"))
    if comment is not None and "Комментарий" in props:
        props["Комментарий"] = comment
    choice_params = choice_parameters(first_child(props_elem, "ChoiceParameters"))
    if choice_params or "ПараметрыВыбора" in props:
        props["ПараметрыВыбора"] = choice_params
    choice_links = choice_parameter_links(first_child(props_elem, "ChoiceParameterLinks"))
    if choice_links or "СвязиПараметровВыбора" in props:
        props["СвязиПараметровВыбора"] = choice_links
    choice_form = text(first_child(props_elem, "ChoiceForm"))
    if choice_form:
        props["ФормаВыбора"] = format_metadata_path(choice_form)
    link_by_type = text(first_child(props_elem, "LinkByType"))
    if link_by_type:
        props["СвязьПоТипу"] = compact_data_path(link_by_type)
    schedule_link = first_child(props_elem, "ScheduleLink")
    if schedule_link is not None:
        props["СвязьСГрафиком"] = format_metadata_path(text(schedule_link))


def extract_properties(elem: ET.Element, type_key: str | None = None, folder: str | None = None) -> Dict[str, Any]:
    props = properties_ru(first_child(elem, "Properties"))
    if type_key in {
        "form",
        "template",
        "attribute",
        "addressing_attribute",
        "tabular_section",
        "tabular_section_attribute",
        "resource",
        "dimension",
        "enum_value",
        "command",
        "document_journal_column",
        "http_service_url_template",
        "http_service_method",
    }:
        props.setdefault("ПринадлежностьОбъекта", "Собственный")
        props.setdefault("ОбъектРасширяемойКонфигурации", "")
    if folder:
        props.setdefault("ПринадлежностьОбъекта", "Собственный")
        props.setdefault("ОбъектРасширяемойКонфигурации", "")
    if type_key in {"resource"} and folder == "InformationRegisters":
        props.setdefault("ИспользованиеХраненияВХранилищеДвоичныхДанных", "Использовать")
        props.setdefault("ПолеИспользованияХраненияВХранилищеДвоичныхДанных", "")
    if type_key in {"attribute", "addressing_attribute"}:
        props.setdefault("ИспользованиеХраненияВХранилищеДвоичныхДанных", "Использовать")
        props.setdefault("ПолеИспользованияХраненияВХранилищеДвоичныхДанных", "")
    overlay_raw_properties(elem, props)
    if folder == "StyleItems" and "Тип" in props:
        style_type = props.pop("Тип")
        if isinstance(style_type, list) and style_type:
            style_type = style_type[0]
        props["Вид"] = translate_value(str(style_type))
    result = legacy_props_compat(props)
    if type_key == "resource" and isinstance(result.get("Подсказка"), str):
        result["Подсказка"] = "\n".join(line for line in result["Подсказка"].splitlines() if line.strip() != "")
    if "Подсказка" in result:
        result["Подсказка"] = normalize_legacy_tooltip(result["Подсказка"], type_key)
    if folder and type_key is None and isinstance(result.get("Тип"), list):
        type_value = result["Тип"]
        if len(type_value) == 1:
            result["Тип"] = type_value[0]
        elif len(type_value) >= 2 and "(" in str(type_value[0]) and str(type_value[-1]).endswith(")"):
            result["Тип"] = ", ".join(str(item) for item in type_value)
    return result
