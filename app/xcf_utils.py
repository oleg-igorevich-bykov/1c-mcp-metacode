from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Iterable, Tuple, Any
import re
import xml.etree.ElementTree as ET


# Namespaces encountered in 1C Form XCF files
NS: Dict[str, str] = {
    "form": "http://v8.1c.ru/8.3/xcf/logform",
    "xr": "http://v8.1c.ru/8.3/xcf/readable",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "v8ui": "http://v8.1c.ru/8.1/data/ui",
    "lf": "http://v8.1c.ru/8.2/managed-application/logform",
    "xs": "http://www.w3.org/2001/XMLSchema",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
    "style": "http://v8.1c.ru/8.1/data/ui/style",
    "win": "http://v8.1c.ru/8.1/data/ui/colors/windows",
    "web": "http://v8.1c.ru/8.1/data/ui/colors/web",
    "sys": "http://v8.1c.ru/8.1/data/ui/fonts/system",
    "ent": "http://v8.1c.ru/8.1/data/enterprise",
    "dcscor": "http://v8.1c.ru/8.1/data-composition-system/core",
    "dcssch": "http://v8.1c.ru/8.1/data-composition-system/schema",
    "dcsset": "http://v8.1c.ru/8.1/data-composition-system/settings",
}


# Translation EN key -> RU key
KEY_TRANSLATION: Dict[str, str] = {
    "Action": "Действие",
    "AllowGettingCurrentRowURL": "РазрешитьПолучатьНавигационнуюСсылкуТекущейСтроки",
    "AllowRootChoice": "РазрешитьВыборКорня",
    "Attribute": "Атрибут",
    "Attributes": "Атрибуты",
    "AutoAddIncomplete": "АвтоВводНезаполненного",
    "AutoCellHeight": "АвтоВысотаЯчейки",
    "AutoChoiceIncomplete": "АвтоВыборНезаполненного",
    "AutoCommandBar": "АвтоКоманднаяПанель",
    "AutoCorrectionOnTextInput": "АвтоИсправлениеПриВводеТекста",
    "Autofill": "АвтоЗаполнение",
    "AutoFillCheck": "ПроверятьЗаполнениеАвтоматически",
    "AutoInsertNewRow": "АвтоВводНовойСтроки",
    "AutoMarkIncomplete": "АвтоОтметкаНезаполненного",
    "AutoMaxHeight": "АвтоМаксимальнаяВысота",
    "AutoMaxWidth": "АвтоМаксимальнаяШирина",
    "AutoRefresh": "АвтоОбновление",
    "AutoRefreshPeriod": "ПериодАвтоОбновления",
    "AutoSaveDataInSettings": "АвтоматическоеСохранениеДанныхВНастройках",
    "AutoShowOpenButtonMode": "РежимАвтоОтображенияКнопкиОткрытия",
    "AutoShowState": "АвтоОтображениеСостояния",
    "AutoTime": "АвтоВремя",
    "AutoTitle": "АвтоЗаголовок",
    "AutoURL": "АвтоНавигационнаяСсылка",
    "BackColor": "ЦветФона",
    "Behavior": "Поведение",
    "BehaviorOnHorizontalCompression": "ПоведениеПриСжатииПоГоризонтали",
    "CellHyperlink": "ГиперссылкаЯчейки",
    "ChangeRowOrder": "ИзменятьПорядокСтрок",
    "ChangeRowSet": "ИзменятьСоставСтрок",
    "CheckBoxType": "ТипФлажка",
    "ChildItems": "ДочерниеЭлементы",
    "ChildItemsWidth": "ШиринаПодчиненныхЭлементовФормы",
    "ChoiceButton": "КнопкаВыбора",
    "ChoiceButtonRepresentation": "ОтображениеКнопкиВыбора",
    "ChoiceFoldersAndItems": "ВыборГруппИЭлементов",
    "ChoiceForm": "ФормаВыбора",
    "ChoiceHistoryOnInput": "ИсторияВыбораПриВводе",
    "ChoiceListButton": "КнопкаСпискаВыбора",
    "ChoiceListHeight": "ВысотаСпискаВыбора",
    "ChooseType": "ВыбиратьТип",
    "ClearButton": "КнопкаОчистки",
    "CollapseItemsByImportanceVariant": "СворачиваниеЭлементовФормыПоВажности",
    "Column": "Колонка",
    "Columns": "Колонки",
    "ColumnsCount": "КоличествоКолонок",
    "Command": "Команда",
    "CommandBar": "КоманднаяПанель",
    "CommandBarLocation": "ПоложениеКоманднойПанели",
    "CommandInterface": "ИнтерфейсКоманд",
    "CommandName": "ИмяКоманды",
    "Commands": "Команды",
    "CommandSet": "НаборКоманд",
    "ContextMenu": "КонтекстноеМеню",
    "ControlRepresentation": "ОтображениеУправления",
    "ConversationsRepresentation": "ОтображениеОбсуждений",
    "CreateButton": "КнопкаСоздания",
    "CurrentRowUse": "ИспользованиеТекущейСтроки",
    "DataPath": "ПутьКДанным",
    "DateQualifiers": "КвалификаторыДаты",
    "DefaultButton": "КнопкаПоУмолчанию",
    "DefaultItem": "АктивизироватьПоУмолчанию",
    "DetailsData": "ДанныеРасшифровки",
    "DropListButton": "КнопкаВыпадающегоСписка",
    "DropListWidth": "ШиринаВыпадающегоСписка",
    "Edit": "Редактирование",
    "EditMode": "РежимРедактирования",
    "EditTextUpdate": "ОбновлениеТекстаРедактирования",
    "EnableContentChange": "РазрешитьИзменениеСостава",
    "Enabled": "Доступность",
    "EnableDrag": "РазрешитьПеретаскивание",
    "EnableStartDrag": "РазрешитьНачалоПеретаскивания",
    "EnterKeyBehavior": "ПоведениеКлавишиEnter",
    "EqualColumnsWidth": "ОдинаковаяШиринаКолонок",
    "EqualItemsWidth": "ОдинаковаяШиринаЭлементов",
    "Event": "Событие",
    "Events": "События",
    "ExcludedCommand": "ИсключеннаяКоманда",
    "ExtendedEdit": "РасширенноеРедактирование",
    "ExtendedTooltip": "РасширеннаяПодсказка",
    "FileDragMode": "РежимПеретаскиванияФайла",
    "FillCheck": "ПроверкаЗаполнения",
    "FixingInTable": "ФиксацияВТаблице",
    "FooterHeight": "ВысотаПодвала",
    "FooterHorizontalAlign": "ГоризонтальноеПоложениеВПодвале",
    "FunctionalOptions": "ФункциональныеОпции",
    "Group": "Группа",
    "GroupHorizontalAlign": "ГоризонтальноеПоложениеВГруппе",
    "GroupVerticalAlign": "ВертикальноеПоложениеВГруппе",
    "handler": "Обработчик",
    "HeaderHeight": "ВысотаШапки",
    "HeaderHorizontalAlign": "ГоризонтальноеПоложениеВШапке",
    "Height": "Высота",
    "HeightControlVariant": "ВариантУправленияВысотой",
    "HeightInMonths": "ВысотаВМесяцах",
    "HeightInTableRows": "ВысотаВСтрокахТаблицы",
    "Hiperlink": "Гиперссылка",
    "HorizontalAlign": "ГоризонтальноеПоложение",
    "HorizontalLines": "ГоризонтальныеЛинии",
    "HorizontalLocation": "ГоризонтальноеПоложениеЭлемента",
    "HorizontalScrollBar": "ГоризонтальнаяПолосаПрокрутки",
    "HorizontalSpacing": "ГоризонтальныйИнтервал",
    "HorizontalStretch": "ГоризонтальнаяРастяжка",
    "Hyperlink": "Гиперссылка",
    "id": "Идентификатор",
    "IncompleteChoiceMode": "РежимВыбораНезаполненного",
    "InitialListView": "НачальноеОтображениеСписка",
    "InitialTreeView": "НачальноеОтображениеДерева",
    "InputHint": "ПодсказкаВвода",
    "ItemHeight": "ВысотаЭлемента",
    "ItemTitleHeight": "ВысотаЗаголовкаЭлемента",
    "ItemWidth": "ШиринаЭлемента",
    "ListChoiceMode": "РежимВыбораИзСписка",
    "LocationInCommandBar": "ПоложениеВКоманднойПанели",
    "MarkNegatives": "ВыделятьОтрицательные",
    "Mask": "Маска",
    "MaxHeight": "МаксимальнаяВысота",
    "MaxValue": "МаксимальноеЗначение",
    "MaxWidth": "МаксимальнаяШирина",
    "MinValue": "МинимальноеЗначение",
    "MultiLine": "МногострочныйРежим",
    "MultipleChoice": "МножественныйВыбор",
    "name": "Имя",
    "name_attr": "ИмяСобытия",
    "NumberQualifiers": "КвалификаторыЧисла",
    "OpenButton": "КнопкаОткрытия",
    "Order": "Сортировка",
    "Output": "Вывод",
    "PagesRepresentation": "ОтображениеСтраниц",
    "PasswordMode": "РежимПароля",
    "Picture": "Картинка",
    "PictureLocation": "ПоложениеКартинки",
    "PictureSize": "РазмерКартинки",
    "Protection": "Защита",
    "QuickChoice": "БыстрыйВыбор",
    "RadioButtonType": "ВидПереключателя",
    "ReadOnly": "ТолькоЧтение",
    "RefreshRequest": "ЗапросОбновления",
    "ReportFormType": "ТипФормыОтчета",
    "ReportResult": "РезультатОтчета",
    "ReportResultViewMode": "РежимОтображенияРезультатаОтчета",
    "RepostOnWrite": "ПриЗаписиПерепроводить",
    "Representation": "Представление",
    "RestoreCurrentRow": "ВосстанавливатьТекущуюСтроку",
    "RowFilter": "ФильтрСтрок",
    "RowInputMode": "РежимВводаСтрок",
    "RowSelectionMode": "РежимВыделенияСтроки",
    "Save": "Сохранение",
    "SaveDataInSettings": "СохранениеДанныхВНастройках",
    "ScalingMode": "РежимМасштабированияПросмотра",
    "SearchControlAddition": "ДополнениеУправлениеПоиском",
    "SearchControlLocation": "ПоложениеУправленияПоиском",
    "SearchOnInput": "ПоискПриВводе",
    "SearchStringAddition": "ДополнениеСтрокаПоиска",
    "SearchStringLocation": "ПоложениеСтрокиПоиска",
    "SelectionMode": "РежимВыделения",
    "SelectionShowMode": "РежимОтображенияВыделения",
    "SettingsNamedItemDetailedRepresentation": "ПодробноеОтображениеИменованныхЭлементовНастройки",
    "Shape": "Фигура",
    "ShapeRepresentation": "ОтображениеФигуры",
    "Shortcut": "СочетаниеКлавиш",
    "ShowCellNames": "ОтображатьИменаЯчеек",
    "ShowCloseButton": "ОтображатьКнопкуЗакрытия",
    "ShowCurrentDate": "ОтображатьТекущуюДату",
    "ShowGrid": "ОтображатьСетку",
    "ShowGroups": "ОтображатьГруппировки",
    "ShowHeaders": "ОтображатьЗаголовки",
    "ShowInFooter": "ОтображатьВПодвале",
    "ShowInHeader": "ОтображатьВШапке",
    "ShowLeftMargin": "ОтображатьОтступСлева",
    "ShowMonthsPanel": "ОтображатьПанельМесяцев",
    "ShowPercent": "ОтображатьПроценты",
    "ShowRoot": "ОтображатьКорень",
    "ShowRowAndColumnNames": "ОтображатьИменаСтрокИКолонок",
    "ShowTitle": "ПоказатьЗаголовок",
    "SkipOnInput": "ПропускатьПриВводе",
    "SpecialTextInputMode": "СпециальныйРежимВводаТекста",
    "SpellCheckingOnTextInput": "ПроверкаПравописанияПриВводеТекста",
    "SpinButton": "КнопкаРегулирования",
    "StringQualifiers": "КвалификаторыСтроки",
    "TextColor": "ЦветТекста",
    "TextEdit": "РедактированиеТекста",
    "ThreeState": "ТриСостояния",
    "ThroughAlign": "СквозноеВыравнивание",
    "Title": "Заголовок",
    "TitleBackColor": "ЦветФонаЗаголовка",
    "TitleHeight": "ВысотаЗаголовка",
    "TitleLocation": "РасположениеЗаголовка",
    "TitleTextColor": "ЦветТекстаЗаголовка",
    "ToolTip": "Подсказка",
    "ToolTipRepresentation": "ПредставлениеПодсказки",
    "Type": "Тип",
    "TypeDomainEnabled": "РазрешитьСоставнойТип",
    "United": "Объединенная",
    "UpdateOnDataChange": "ОбновлениеПриИзмененииДанных",
    "Use": "Использование",
    "UseAlternationRowColor": "ЧередованиеЦветовСтрок",
    "UseForFoldersAndItems": "ИспользованиеДляГруппИЭлементов",
    "UsePostingMode": "ИспользоватьРежимПроведения",
    "VerticalAlign": "ВертикальноеПоложение",
    "VerticalLines": "ВертикальныеЛинии",
    "VerticalScroll": "ВертикальнаяПрокрутка",
    "VerticalScrollBar": "ВертикальнаяПолосаПрокрутки",
    "VerticalSpacing": "ВертикальныйИнтервал",
    "VerticalStretch": "ВертикальнаяРастяжка",
    "ViewMode": "РежимОтображения",
    "ViewModeApplicationOnSetReportResult": "ПрименениеРежимаОтображенияПриУстановкеРезультатаОтчета",
    "ViewScalingMode": "РежимМасштабированияПросмотра",
    "ViewStatusAddition": "ДополнениеСостояниеПросмотра",
    "ViewStatusLocation": "ПоложениеСостоянияПросмотра",
    "Visible": "Видимость",
    "WarningOnEditRepresentation": "ОтображениеПредупрежденияПриРедактировании",
    "Width": "Ширина",
    "WidthInMonths": "ШиринаВМесяцах",
    "WindowOpeningMode": "РежимОткрытияОкна",
    "Wrap": "АвтоПереносСтрок",
    "Zoomable": "Масштабировать",
}

PROPERTY_TRANSLATION_OVERRIDES: Dict[Tuple[str, str, str], str] = {
    ("FormControl", "", "ChoiceMode"): "СпособВыбора",
    ("Form", "", "ScalingMode"): "ВариантМасштабаФормКлиентскогоПриложения",
}


# Map of 1C Managed Form event names from EN attribute values to RU canonical names
EVENT_NAME_MAP_EN_TO_RU: Dict[str, str] = {
    # Form-level common events
    "OnOpen": "ПриОткрытии",
    "OnClose": "ПриЗакрытии",
    "BeforeClose": "ПередЗакрытием",
    "BeforeWrite": "ПередЗаписью",
    "AfterWrite": "ПослеЗаписи",
    "BeforeWriteAtServer": "ПередЗаписьюНаСервере",
    "AfterWriteAtServer": "ПослеЗаписиНаСервере",
    "OnWriteAtServer": "ПриЗаписиНаСервере",
    "OnCreateAtServer": "ПриСозданииНаСервере",
    "OnReadAtServer": "ПриЧтенииНаСервере",
    "NotificationProcessing": "ОбработкаОповещения",
    "ChoiceProcessing": "ОбработкаВыбора",
    "FillCheckProcessingAtServer": "ОбработкаПроверкиЗаполненияНаСервере",

    # User settings and settings-data events
    "BeforeLoadUserSettingsAtServer": "ПередЗагрузкойПользовательскихНастроекНаСервере",
    "AfterLoadUserSettingsAtServer": "ПослеЗагрузкиПользовательскихНастроекНаСервере",
    "BeforeSaveUserSettingsAtServer": "ПередСохранениемПользовательскихНастроекНаСервере",
    "AfterSaveUserSettingsAtServer": "ПослеСохраненияПользовательскихНастроекНаСервере",
    "OnSaveUserSettingsAtServer": "ПриСохраненииПользовательскихНастроекНаСервере",
    "BeforeLoadDataFromSettingsAtServer": "ПередЗагрузкойДанныхИзНастроекНаСервере",
    "AfterLoadDataFromSettingsAtServer": "ПослеЗагрузкиДанныхИзНастроекНаСервере",
    "OnLoadDataFromSettingsAtServer": "ПриЗагрузкеДанныхИзНастроекНаСервере",
    "OnUpdateUserSettingSetAtServer": "ПриОбновленииНабораПользовательскихНастроекНаСервере",
    "OnUpdateUserSettingsSetAtServer": "ПриОбновленииНабораПользовательскихНастроекНаСервере",

    # Control-level and list/table events
    "OnChange": "ПриИзменении",
    "AutoComplete": "АвтоПодбор",
    "EditTextChange": "ИзменениеТекстаРедактирования",
    "TextEditEnd": "ОкончаниеРедактированияТекста",
    "OnEditEnd": "ПриОкончанииРедактирования",
    "URLProcessing": "ОбработкаНавигационнойСсылки",
    "Click": "Нажатие",
    "Selection": "Выбор",
    "StartChoice": "НачалоВыбора",
    "OnStartEdit": "ПриНачалеРедактирования",
    "BeforeAddRow": "ПередНачаломДобавления",
    "AfterDeleteRow": "ПослеУдаленияСтроки",
    "DragCheck": "ПроверкаПеретаскивания",
    "Drag": "Перетаскивание",
    "OnActivateRow": "ПриАктивизацииСтроки",
    "OnGetDataAtServer": "ПриПолученииДанныхНаСервере",
    "BeforeRowChange": "ПередНачаломИзменения",
    "OnCurrentPageChange": "ПриИзмененииТекущейСтраницы",

    # Object-level events for Event Subscriptions
    "BeforeDelete": "ПередУдалением",
    "FillCheckProcessing": "ОбработкаПроверкиЗаполнения",
    "Filling": "Заполнение",
    "FormGetProcessing": "ОбработкаПолученияФормы",
    "OnCopy": "ПриКопировании",
    "OnReceiveDataFromMaster": "ПриПолученииДанныхОтМастер",
    "OnReceiveDataFromSlave": "ПриПолученииДанныхОтПодчиненного",
    "OnSendDataToMaster": "ПриОтправкеДанныхМастеру",
    "OnSendDataToSlave": "ПриОтправкеДанныхПодчиненному",
    "OnSendNodeDataToSlave": "ПриОтправкеУзлаДанныхПодчиненному",
    "OnSetNewCode": "ПриУстановкеНовогоКода",
    "OnSetNewNumber": "ПриУстановкеНовогоНомера",
    "OnWrite": "ПриЗаписи",
    "Posting": "Проведение",
    "PresentationFieldsGetProcessing": "ОбработкаПолученияПолейПредставления",
    "PresentationGetProcessing": "ОбработкаПолученияПредставления",
    "UndoPosting": "ОтменаПроведения",

    # Additional form-level events
    "OnReopen": "ПриПовторномОткрытии",
    "Opening": "Открытие",
    "ExternalEvent": "ВнешнееСобытие",
    "NavigationProcessing": "ОбработкаНавигации",
    "NewWriteProcessing": "ОбработкаНовойЗаписи",
    "OnMainServerAvailabilityChange": "ПриИзмененииДоступностиОсновногоСервера",

    # Variant/settings events
    "BeforeLoadVariantAtServer": "ПередЗагрузкойВариантаНаСервере",
    "OnLoadVariantAtServer": "ПриЗагрузкеВариантаНаСервере",
    "OnSaveVariantAtServer": "ПриСохраненииВариантаНаСервере",
    "OnLoadUserSettingsAtServer": "ПриЗагрузкеПользовательскихНастроекНаСервере",
    "OnSaveDataInSettingsAtServer": "ПриСохраненииДанныхВНастройкахНаСервере",

    # Tree/list control events
    "BeforeExpand": "ПередРазворачиванием",
    "BeforeCollapse": "ПередСвертыванием",
    "BeforeDeleteRow": "ПередУдалениемСтроки",
    "BeforeEditEnd": "ПередОкончаниемРедактирования",
    "Creating": "Создание",
    "Clearing": "Очистка",
    "Tuning": "Настройка",
    "StartListChoice": "НачалоВыбораИзСписка",
    "ValueChoice": "ОбработкаВыбораЗначения",

    # Drag events
    "DragStart": "НачалоПеретаскивания",
    "DragEnd": "ОкончаниеПеретаскивания",

    # Cell/field activation events
    "OnActivate": "ПриАктивизации",
    "OnActivateCell": "ПриАктивизацииЯчейки",
    "OnActivateField": "ПриАктивизацииПоля",
    "OnClick": "ПриНажатии",
    "OnChangeAreaContent": "ПриИзмененииСодержимогоОбласти",
    "OnPeriodOutput": "ПриВыводеПериода",

    # SpreadsheetDocument/HTMLDocument events
    "DocumentComplete": "ДокументСформирован",

    # URL events
    "URLGetProcessing": "ОбработкаПолученияURLАдреса",
    "URLListGetProcessing": "ОбработкаПолученияСпискаURLАдресов",

    # Drill-down / detail processing
    "DetailProcessing": "ОбработкаРасшифровки",
    "AdditionalDetailProcessing": "ОбработкаДополнительногоРасшифрования",
}

# Simple Cyrillic detector to decide if a string is already Russian
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

def _is_russian_string(s: str) -> bool:
    return bool(_CYRILLIC_RE.search(s or ""))

def normalize_event_name(name: str) -> str:
    """
    Convert EN event attribute value (e.g., 'OnOpen') to RU canonical name (e.g., 'ПриОткрытии').
    If the value already contains Cyrillic, return as is. Unknown EN values are returned unchanged.
    """
    n = (name or "").strip()
    if not n:
        return n
    if _is_russian_string(n):
        return n
    return EVENT_NAME_MAP_EN_TO_RU.get(n, n)

# Control tag (element name) -> RU friendly type
CONTROL_TAG_MAP: Dict[str, str] = {
    "UsualGroup": "ОбычнаяГруппа",
    "ButtonGroup": "ГруппаКнопок",
    "Button": "Кнопка",
    "InputField": "ПолеВвода",
    "LabelField": "Надпись",
    "Table": "Таблица",
    "CheckBoxField": "Флажок",
    "RadioButtonField": "Переключатель",
    "AutoCommandBar": "АвтоКоманднаяПанель",
    "PictureDecoration": "ДекорацияКартинки",
    "LabelDecoration": "ДекорацияНадписи",
    "SearchStringAddition": "ДополнениеСтрокаПоиска",
    "ViewStatusAddition": "ДополнениеСостояниеПросмотра",
    "SearchControlAddition": "ДополнениеУправлениеПоиском",
    "Picture": "Картинка",
    "Label": "Надпись",
    "Hyperlink": "Гиперссылка",
    "CommandBarButton": "КнопкаКоманднойПанели",
    "CommandBar": "КоманднаяПанель",
    "Pages": "Страницы",
    "Page": "Страница",
    "ColumnGroup": "ГруппаКолонок",
    "Popup": "ВсплывающееОкно",
    "Tree": "Дерево",
    "TextDocumentField": "ПолеТекстовогоДокумента",
    "SpreadsheetDocumentField": "ПолеТабличногоДокумента",
    "Calendar": "Календарь",
    "Chart": "Диаграмма",
    "Separator": "Разделитель",
    "ProgressBar": "ИндикаторПрогресса",
    "Slider": "Ползунок",
}

# Known enum value mappings for specific RU-normalized keys
ENUM_VALUE_MAPS: Dict[str, Dict[str, str]] = {
    # Orientation of a group
    "Группа": {
        "Horizontal": "Горизонтально",
        "Vertical": "Вертикально",
        "AlwaysHorizontal": "ВсегдаГоризонтально",
    },
    # Behavior of group containers
    "Поведение": {
        "Usual": "Обычное",
        "Collapsible": "Сворачиваемое",
        "None": "Нет",
    },
    # Representation for various controls
    "Представление": {
        "None": "Нет",
        "List": "Список",
        "Picture": "Картинка",
    },
    # Title location
    "РасположениеЗаголовка": {
        "Left": "Слева",
        "Right": "Справа",
        "Top": "Сверху",
        "None": "Нет",
    },
    # Tooltip representation
    "ПредставлениеПодсказки": {
        "None": "Нет",
        "Button": "Кнопка",
    },
    # Picture size mode
    "РазмерКартинки": {
        "AutoSize": "АвтоРазмер",
        "Stretch": "Растянуть",
        "Normal": "Нормальный",
    },
    # Selection mode for tables/lists
    "РежимВыделения": {
        "SingleRow": "ОднаСтрока",
        "MultiRow": "НесколькоСтрок",
    },
    # Through alignment
    "СквозноеВыравнивание": {
        "DontUse": "НеИспользовать",
        "Use": "Использовать",
    },
    # File drag mode
    "РежимПеретаскиванияФайла": {
        "AsFile": "КакФайл",
    },
    # Edit text updates
    "ОбновлениеТекстаРедактирования": {
        "OnValueChange": "ПриИзмененииЗначения",
    },
    # Edit mode
    "РежимРедактирования": {
        "EnterOnInput": "ВводПриВводе",
    },
    # Control representation (explicit key variant sometimes present)
    "ПредставлениеКонтрола": {
        "Picture": "Картинка",
        "None": "Нет",
    },
}

VALUE_TRANSLATION_DEFAULTS: Dict[str, str] = {
    "AfterCurrentRow": "ПослеТекущейСтроки",
    "All": "Все",
    "Always": "Всегда",
    "Balloon": "Всплывающая",
    "Beginning": "Начало",
    "Bottom": "Низ",
    "ByFontSize": "ПоРазмеруШрифта",
    "CalendarField": "ПолеКалендаря",
    "Center": "Центр",
    "ChartField": "ПолеДиаграммы",
    "CheckBox": "Флажок",
    "Choice": "Выбор",
    "CommandBar": "КоманднаяПанель",
    "CurrentOrLast": "ТекущееИлиПоследним",
    "DefaultButton": "КнопкаПоУмолчанию",
    "Digits": "Цифры",
    "Directly": "Непосредственно",
    "Disable": "Запретить",
    "DontShow": "НеОтображать",
    "DontUse": "НеИспользовать",
    "Double": "Двойной",
    "Enable": "Разрешить",
    "End": "Конец",
    "Equal": "Одинаковая",
    "ExpandAllLevels": "РаскрыватьВсеУровни",
    "ExpandTopLevel": "РаскрыватьВерхнийУровень",
    "Folders": "Группы",
    "FoldersAndItems": "ГруппыИЭлементы",
    "Form": "Форма",
    "FormattedDocumentField": "ПолеФорматированногоДокумента",
    "GraphicalSchemaField": "ПолеГрафическойСхемы",
    "Half": "Половинный",
    "HorizontalIfPossible": "ГоризонтальнаяЕслиВозможно",
    "HTMLDocumentField": "ПолеHTMLДокумента",
    "InAdditionalSubmenu": "ВДополнительномПодменю",
    "InCell": "ВЯчейке",
    "InCommandBar": "ВКоманднойПанели",
    "InCommandBarAndInAdditionalSubmenu": "ВКоманднойПанелиИВДополнительномПодменю",
    "Items": "Элементы",
    "ItemsLeftTitlesLeft": "ЭлементыЛевоЗаголовкиЛево",
    "ItemsLeftTitlesRight": "ЭлементыЛевоЗаголовкиПраво",
    "Left": "Лево",
    "LeftNarrow": "ЛевыйУзкий",
    "LeftNarrowest": "ЛевыйОченьУзкий",
    "LeftWide": "ЛевыйШирокий",
    "LeftWidest": "ЛевыйОченьШирокий",
    "LockOwnerWindow": "БлокироватьОкноВладельца",
    "LockWholeInterface": "БлокироватьВесьИнтерфейс",
    "Main": "Основная",
    "MoveItemsByImportance": "ПереноситьЭлементыПоВажности",
    "None": "Нет",
    "Normal": "Обычный",
    "NormalSeparation": "ОбычноеВыделение",
    "OnActivate": "ПриАктивизации",
    "OneAndHalf": "Полуторный",
    "Oval": "Овал",
    "PDFDocumentField": "ПолеPDFДокумента",
    "PhoneNumber": "НомерТелефона",
    "Picture": "Картинка",
    "PictureAndText": "КартинкаИТекст",
    "PictureField": "ПолеКартинки",
    "PopUp": "Всплывающая",
    "ProgressBarField": "ПолеИндикатора",
    "Proportionally": "Пропорционально",
    "PullFromTop": "ПотянутьСверху",
    "RadioButtons": "Переключатель",
    "RealSizeIgnoreScale": "РеальныйРазмерБезУчетаМасштаба",
    "Right": "Право",
    "Row": "Строка",
    "SelectionPresentation": "ОтображениеВыделения",
    "SelectionPresentationAndChoice": "ОтображениеВыделенияИВыбор",
    "Settings": "Настройка",
    "Show": "Отображать",
    "ShowAuto": "ОтображатьАвто",
    "ShowBottom": "ОтображатьСнизу",
    "ShowError": "ВыдаватьОшибку",
    "ShowInDropList": "ОтображатьВВыпадающемСписке",
    "ShowInDropListAndInInputField": "ОтображатьВВыпадающемСпискеИВПолеВвода",
    "ShowInInputField": "ОтображатьВПолеВвода",
    "ShowLeft": "ОтображатьСлева",
    "ShowRight": "ОтображатьСправа",
    "ShowTop": "ОтображатьСверху",
    "Single": "Одинарный",
    "SpreadSheetDocumentField": "ПолеТабличногоДокумента",
    "StrongSeparation": "СильноеВыделение",
    "Switcher": "Выключатель",
    "TabsOnBottom": "ЗакладкиСнизу",
    "TabsOnLeftHorizontal": "ЗакладкиСлеваГоризонтально",
    "TabsOnTop": "ЗакладкиСверху",
    "Text": "Текст",
    "Top": "Верх",
    "TrackBarField": "ПолеПолосыРегулирования",
    "Tree": "Дерево",
    "Tumbler": "Тумблер",
    "Use": "Использовать",
    "UseAlways": "ИспользоватьВсегда",
    "UseContentHeight": "ПоСодержимому",
    "UseHeightInFormRows": "ВСтрокахФормы",
    "UseHeightInTableRows": "ВСтрокахТаблицы",
    "useIfNecessary": "ИспользоватьПриНеобходимости",
    "UseList": "ИспользоватьСписок",
    "useWithoutStretch": "ИспользоватьБезРастягивания",
    "WhenActive": "ПриАктивности",
    "WhenMultipleCellsSelected": "ПриВыделенииНесколькихЯчеек",
}

VALUE_TRANSLATION_OVERRIDES: Dict[Tuple[str, str, str, str], str] = {
    ("Form", "", "ВариантМасштабаФормКлиентскогоПриложения", "Compact"): "Компактный",
    ("FormControl", "ГруппаКнопок", "Представление", "Compact"): "Компактное",
    ("FormControl", "Кнопка", "Фигура", "Usual"): "Обычная",
    ("FormControl", "ГруппаКнопок", "Представление", "Usual"): "Обычное",
}

DEFAULT_VALUE_TRANSLATION_EXCLUDED_KEYS = {
    "Имя",
    "Идентификатор",
    "Заголовок",
    "Синоним",
    "Комментарий",
    "Подсказка",
    "РасширеннаяПодсказка",
    "Действие",
    "ПутьКДанным",
    "ПутьКДанным_RAW",
    "Тип",
    "config_name",
    "project_name",
    "content_hash",
    "ext_source",
    "modified_properties",
    "ctrl_id",
    "name_path",
    "event_name",
    "handler_name",
    "call_type",
    "call_type_ru",
}

_BOOL_MAP = {
    "true": "Истина",
    "false": "Ложь",
    "1": "Истина",
    "0": "Ложь",
}

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _can_apply_default_value_translation(key_ru: str, value: str) -> bool:
    if not key_ru or key_ru in DEFAULT_VALUE_TRANSLATION_EXCLUDED_KEYS or "_" in key_ru:
        return False
    v = (value or "").strip()
    if not v:
        return False
    low = v.lower()
    if low.startswith(("style:", "web:", "win:")):
        return False
    if _GUID_RE.match(v):
        return False
    if any(sep in v for sep in (".", "/", "\\")):
        return False
    return True

def ru_control_type(tag: str) -> str:
    return CONTROL_TAG_MAP.get(tag, tag)

def _context_lookup(mapping: Dict[Tuple[str, str, str, str], str], node_label: str, control_type: str, key_ru: str, value: str) -> Optional[str]:
    node = node_label or ""
    ctrl = control_type or ""
    for candidate in (
        (node, ctrl, key_ru, value),
        (node, "", key_ru, value),
        ("", ctrl, key_ru, value),
    ):
        found = mapping.get(candidate)
        if found is not None:
            return found
    return None


def normalize_value(key_ru: str, value: Any, node_label: str = "", control_type: str = "") -> Any:
    """
    Normalize property values:
    - Convert booleans True/False and strings 'true'/'false' (and 1/0) to 'Истина'/'Ложь'
    - Map known enumeration tokens to RU equivalents based on the key
    - Translate control type values if key is 'ТипКонтрола'
    - Convert 'Тип' literals (xs:*, cfg:*, v8ui:*) to 1C type tokens
    - Apply recursively to lists
    """
    try:
        # Recursive normalization for lists
        if isinstance(value, list):
            return [normalize_value(key_ru, v, node_label=node_label, control_type=control_type) for v in value]

        # Python booleans
        if isinstance(value, bool):
            return "Истина" if value else "Ложь"

        # String-based normalization
        if isinstance(value, str):
            v = value.strip()
            low = v.lower()
            if low in _BOOL_MAP:
                return _BOOL_MAP[low]
            if low == "auto":
                return "Авто"

            # DataPath normalization (ПутьКДанным / legacy ПутьДанным)
            if key_ru in ("ПутьКДанным", "ПутьДанным"):
                return normalize_data_path(v)
 
            # Form attribute type normalization to 1C tokens
            if key_ru == "Тип":
                return convert_form_type_to_1c(v)

            # Per-key enum mapping
            override = _context_lookup(VALUE_TRANSLATION_OVERRIDES, node_label, control_type, key_ru, v)
            if override is not None:
                return override
            m = ENUM_VALUE_MAPS.get(key_ru)
            if m:
                mapped = m.get(v)
                if mapped is not None:
                    return mapped
            # Control type value
            if key_ru == "ТипКонтрола":
                return ru_control_type(v)
            if _can_apply_default_value_translation(key_ru, v):
                mapped = VALUE_TRANSLATION_DEFAULTS.get(v)
                if mapped is not None:
                    return mapped

        return value
    except Exception:
        return value

def normalize_properties_values(raw: Dict[str, Any], node_label: str = "", control_type: str = "") -> Dict[str, Any]:
    """
    Apply value normalization to a RU-keyed properties map.
    """
    out: Dict[str, Any] = {}
    for k, v in (raw or {}).items():
        out[k] = normalize_value(k, v, node_label=node_label, control_type=control_type)
    return out

# Mapping for segments used in data paths (e.g., 'Список.Ref' -> 'Список.Ссылка')
DATA_PATH_SEGMENT_MAP: Dict[str, str] = {
    # English -> Russian
    "List": "Список",
    "Ref": "Ссылка",
    "Description": "Наименование",
    "Code": "Код",
    "LineNumber": "НомерСтроки",
    "RowNumber": "НомерСтроки",
    "Recorder": "Регистратор",
    "Number": "Номер",
    "Date": "Дата",
    "Posted": "Проведен",
    "DeletionMark": "ПометкаУдаления",
    "IsFolder": "ЭтоГруппа",
    "Predefined": "Предопределенный",
    "Comment": "Комментарий",
    # Common alternates
    "RecordSet": "НаборЗаписей",
    "CurrentData": "ТекущиеДанные",
    "Owner": "Владелец",
    "Parent": "Родитель",
    "Presentation": "Представление",
    "Selection": "Выбор",
}

def normalize_data_path(path: str) -> str:
    """
    Normalize DataPath value segments to Russian equivalents.
    Example: 'Список.Ref' -> 'Список.Ссылка', 'List.Description' -> 'Список.Наименование'
    """
    if not isinstance(path, str) or not path:
        return path
    parts = [p.strip() for p in path.split(".")]
    out: list[str] = []
    for p in parts:
        if not p:
            out.append(p)
            continue
        # Map exact segment when known; preserve already-Russian tokens
        mapped = DATA_PATH_SEGMENT_MAP.get(p, p)
        out.append(mapped)
    return ".".join(out)

# Map primitive XSD and v8ui types to 1C types
XSD_TO_1C: Dict[str, str] = {
    "boolean": "Булево",
    "string": "Строка",
    "decimal": "Число",
    "integer": "Число",
    "int": "Число",
    "double": "Число",
    "dateTime": "Дата",
    "date": "Дата",
}

V8UI_TO_1C: Dict[str, str] = {
    "FormattedString": "ФорматированнаяСтрока",
    "Color": "Цвет",
}

# Map v8:* types to 1C types
V8_TO_1C: Dict[str, str] = {
    # Collections / compound values
    "ValueListType": "СписокЗначений",
    "ValueTable": "ТаблицаЗначений",
    "ValueTree": "ДеревоЗначений",
    "Array": "Массив",
    "Structure": "Структура",
    "Map": "Соответствие",
    "ValueMap": "Соответствие",

    # Primitives / simple values
    "UUID": "УникальныйИдентификатор",
    "BinaryData": "ДвоичныеДанные",
    "Text": "Текст",
    "ValueStorage": "ХранилищеЗначения",
    "Undefined": "Неопределено",
    "Null": "Null",
    "Date": "Дата",
    "DateTime": "МоментВремени",
    "Number": "Число",
    "Numeric": "Число",
    "Decimal": "Число",
    "BigNumber": "ЧислоНеограниченнойТочности",
    "FixedDecimal": "ЧислоСФиксированнойТочностью",
    "FixedPrecisionNumber": "ЧислоСФиксированнойТочностью",
    "Boolean": "Булево",
    "String": "Строка",

    # Fallback/general
    "Any": "Произвольный",
}

# Map cfg:* non-ref/simple kinds to 1C types (no object name suffix)
CFG_TO_1C_NONREF: Dict[str, str] = {
    "DynamicList": "ДинамическийСписок",
}

# Map cfg:* kind to 1C reference type prefixes
CFG_KIND_TO_1C_PREFIX: Dict[str, str] = {
    "CatalogRef": "СправочникСсылка",
    "CatalogObject": "СправочникОбъект",
    "DocumentRef": "ДокументСсылка",
    "DocumentObject": "ДокументОбъект",
    "EnumRef": "ПеречислениеСсылка",
    "InformationRegisterRef": "РегистрСведенийСсылка",
    "AccumulationRegisterRef": "РегистрНакопленияСсылка",
    "BusinessProcessRef": "БизнесПроцессСсылка",
    "BusinessProcessObject": "БизнесПроцессОбъект",
    "TaskRef": "ЗадачаСсылка",
    "TaskObject": "ЗадачаОбъект",
    "ChartOfAccountsRef": "ПланСчетовСсылка",
    "ChartOfCharacteristicTypesRef": "ПланВидовХарактеристикСсылка",
    "ChartOfCalculationTypesRef": "ПланВидовРасчетаСсылка",

    # Additional cfg kinds used in Form attributes
    "InformationRegisterRecordManager": "РегистрСведенийМенеджерЗаписи",
    "InformationRegisterRecordSet": "РегистрСведенийНаборЗаписей",
}

def convert_form_type_to_1c(literal: str) -> str:
    """
    Convert Form.xml attribute type literals (e.g. 'xs:boolean', 'cfg:CatalogRef.Номенклатура',
    'v8ui:FormattedString') into 1C type tokens like:
      - Булево, Строка, Число, Дата
      - <Категория>Ссылка.<Имя>  (e.g., СправочникСсылка.Номенклатура)
    Unknown values are returned unchanged.

    For cfg:* kinds ending with 'Object', known kinds map to '<Категория>Объект'.
    Unknown kinds fallback to the corresponding '*Ref' to preserve linking (e.g., UnknownObject -> UnknownRef).
    """
    s = (literal or "").strip()
    if not s:
        return s
    # Collapse excessive whitespace/newlines
    s = " ".join(s.split())

    if ":" not in s:
        return s

    ns, rest = s.split(":", 1)
    ns = ns.strip()
    rest = rest.strip()

    if ns == "xs":
        return XSD_TO_1C.get(rest, s)

    if ns == "v8":
        return V8_TO_1C.get(rest, s)

    if ns == "v8ui":
        return V8UI_TO_1C.get(rest, s)

    if ns == "cfg":
        # Non-ref/simple kinds without dot (e.g., DynamicList)
        if "." not in rest:
            nonref = CFG_TO_1C_NONREF.get(rest)
            if nonref:
                return nonref
        # Expect patterns like 'CatalogRef.Номенклатура'
        if "." in rest:
            kind, name = rest.split(".", 1)
            kind = kind.strip()
            name = (name or "").strip()
        else:
            kind, name = rest.strip(), ""

        # Direct mapping
        prefix = CFG_KIND_TO_1C_PREFIX.get(kind)

        # Fallback: map ...Object -> ...Ref to prefer link types for relationship building
        if not prefix and kind.endswith("Object"):
            alt_kind = kind[:-6] + "Ref"
            prefix = CFG_KIND_TO_1C_PREFIX.get(alt_kind)

        if prefix and name:
            return f"{prefix}.{name}"
        if prefix:
            return prefix
        return s

    return s

CAT_MAP_EN_TO_RU: Dict[str, str] = {
    "Catalogs": "Справочники",
    "Documents": "Документы",
    "Reports": "Отчеты",
    "DataProcessors": "Обработки",
    "InformationRegisters": "РегистрыСведений",
    "AccumulationRegisters": "РегистрыНакопления",
    "ChartsOfAccounts": "ПланыСчетов",
    "ChartsOfCharacteristicTypes": "ПланыВидовХарактеристик",
    "ChartsOfCalculationTypes": "ПланыВидовРасчета",
    "BusinessProcesses": "БизнесПроцессы",
    "Tasks": "Задачи",
    "DocumentJournals": "ЖурналыДокументов",
    "HTTPServices": "HTTPСервисы",
    "CommonForms": "ОбщиеФормы",
    # Added categories
    "CommonCommands": "ОбщиеКоманды",
    "Constants": "Константы",
    "AccountingRegisters": "РегистрыБухгалтерии",
}


def local_name(tag: str) -> str:
    """
    Extract local tag name from a namespaced tag like '{ns}TagName' -> 'TagName'
    """
    if not tag:
        return tag
    if tag[0] == "{":
        return tag.split("}", 1)[1]
    return tag


_WORD_SPLIT_RE = re.compile(r"[ \-\t_/.:;,&]+", re.UNICODE)


def _pascal_case_ru(s: str) -> str:
    """
    Convert to PascalCase when separators are present.
    Preserve internal capitalization for already PascalCase identifiers (RU/EN).
    """
    s = (s or "").strip()
    if not s:
        return s
    # Normalize 'ё'/'Ё' for stability but do not alter existing capitalization otherwise
    s_norm = s.replace("ё", "е").replace("Ё", "Е")
    # If no separators, assume already a single token (possibly PascalCase) and keep as-is
    if not _WORD_SPLIT_RE.search(s_norm):
        return s_norm
    parts = [p for p in _WORD_SPLIT_RE.split(s_norm) if p]
    # Title-case each token unless it already has internal capitals (Camel/Pascal)
    def _to_pascal(token: str) -> str:
        if len(token) > 1 and any(ch.isupper() for ch in token[1:]):
            return token
        return token[:1].upper() + token[1:].lower()
    return "".join(_to_pascal(p) for p in parts)


def normalize_key(key: str, node_label: str = "", control_type: str = "") -> str:
    """
    Translate a key from EN -> RU using KEY_TRANSLATION if present,
    then normalize to Russian PascalCase and remove spaces/hyphens.
    For already-Russian keys, only normalize.
    """
    if not key:
        return key
    node = node_label or ""
    ctrl = control_type or ""
    base = (
        PROPERTY_TRANSLATION_OVERRIDES.get((node, ctrl, key))
        or PROPERTY_TRANSLATION_OVERRIDES.get((node, "", key))
        or KEY_TRANSLATION.get(key, key)
    )
    # Remove stray quotes/backticks just in case
    base = base.replace("`", "").strip()
    return _pascal_case_ru(base)


def get_text(elem: Optional[ET.Element]) -> Optional[str]:
    """
    Get trimmed text of an element. Returns None if missing/empty.
    """
    if elem is None:
        return None
    txt = (elem.text or "").strip()
    return txt if txt != "" else None


def get_localized_text(block: Optional[ET.Element]) -> Optional[str]:
    """
    Extract text from a localized 1C block:
      <Title><v8:item><v8:lang>ru</v8:lang><v8:content>Текст</v8:content></v8:item>...</Title>
    Prefer 'ru', fallback to the first available item.
    """
    if block is None:
        return None
    # Try RU first
    for it in block.findall(".//v8:item", NS):
        lang = get_text(it.find("v8:lang", NS))
        if lang and lang.lower() == "ru":
            content = it.find("v8:content", NS)
            val = get_text(content)
            if val:
                return val
    # Fallback to first v8:item content
    it = block.find(".//v8:item", NS)
    if it is not None:
        content = it.find("v8:content", NS)
        val = get_text(content)
        if val:
            return val
    return None


def ru_category_from_folder(folder_name: str) -> str:
    """
    Map code folder name (often EN like 'Catalogs') to RU category name used in the graph.

    Consults local CAT_MAP_EN_TO_RU first, then falls back to xml_metadata.folder_map
    (the project-wide single source of truth) so categories absent here (e.g. ExchangePlans,
    CalculationRegisters, WebServices) still get the canonical RU name.

    Returns the original folder name if unknown in both maps.
    """
    mapped = CAT_MAP_EN_TO_RU.get(folder_name)
    if mapped:
        return mapped
    try:
        from xml_metadata.folder_map import FOLDER_TO_RU_CATEGORY
        return FOLDER_TO_RU_CATEGORY.get(folder_name, folder_name)
    except Exception:
        return folder_name


def compute_obj_qn(project_name: str, config_name: str, category_ru: str, object_name: str) -> str:
    return f"{project_name}/{config_name}/{category_ru}/{object_name}"


def compute_form_qn(project_name: str, config_name: str, category_ru: str, object_name: str, form_name: str) -> str:
    return f"{project_name}/{config_name}/{category_ru}/{object_name}/Form/{form_name}"


def safe_attr(elem: ET.Element, attr: str) -> Optional[str]:
    v = elem.get(attr)
    return v.strip() if isinstance(v, str) and v.strip() else None


def control_display_name(elem: ET.Element) -> str:
    """
    Compose a readable control name from 'name' and 'id' attributes if present.
    """
    nm = safe_attr(elem, "name")
    cid = safe_attr(elem, "id")
    if nm and cid:
        return f"{nm}#{cid}"
    return nm or cid or local_name(elem.tag)


def make_control_qn(form_qn: str, hierarchical_path: Iterable[str]) -> str:
    """
    Build a qualified name for a control based on hierarchical path segments.
    """
    path = "/".join([p for p in hierarchical_path if p])
    return f"{form_qn}/Control/{path}" if path else f"{form_qn}/Control"


def make_event_qn(form_qn: str, target_path: str, ev_name: str) -> str:
    tail = f"{target_path}/{ev_name}" if target_path else ev_name
    return f"{form_qn}/Event/{tail}"


def make_form_attr_qn(form_qn: str, attr_name: str) -> str:
    return f"{form_qn}/FormAttribute/{attr_name}"


def flatten_simple_children(
    elem: ET.Element,
    extra_extract: Optional[Dict[str, Any]] = None,
    node_label: str = "",
    control_type: str = "",
) -> Dict[str, Any]:
    """
    Heuristically extract simple scalar children into a dict of RU-normalized keys.
    - For elements like Title/ToolTip/ExtendedTooltip, use get_localized_text.
    - For nested complex blocks, skip or perform best-effort extraction.
    """
    props: Dict[str, Any] = {}
    for ch in list(elem):
        lname = local_name(ch.tag)
        # Skip known container blocks handled elsewhere
        if lname in {"Events", "ChildItems", "Picture", "CommandSet"}:
            continue
        if lname in {"Title", "ToolTip"}:
            val = get_localized_text(ch)
            if val is not None:
                props[normalize_key(lname, node_label=node_label, control_type=control_type)] = val
            continue
        if lname == "ExtendedTooltip":
            # ExtendedTooltip may have nested Title formatted="..."
            title = ch.find("Title", NS) or ch.find(".//Title", NS)
            txt = get_localized_text(title) if title is not None else None
            if txt is not None:
                props[normalize_key(lname, node_label=node_label, control_type=control_type)] = txt
            continue
        # Simple scalar
        txt = get_text(ch)
        if txt is not None:
            props[normalize_key(lname, node_label=node_label, control_type=control_type)] = txt
    # Include provided extras (already RU-keyed)
    if extra_extract:
        props.update(extra_extract)
    return props


def normalize_properties_map(raw: Dict[str, Any], node_label: str = "", control_type: str = "") -> Dict[str, Any]:
    """
    Apply EN->RU key translation and PascalCase normalization to a properties dict.
    Non-string keys are stringified first.
    """
    out: Dict[str, Any] = {}
    for k, v in (raw or {}).items():
        rk = normalize_key(str(k), node_label=node_label, control_type=control_type)
        out[rk] = v
    return out


def parse_path_triplet(code_dir: Path, file_path: Path) -> Optional[Tuple[str, str, str]]:
    """
    From .../code/<CategoryFolder>/<ObjectName>/Forms/<FormName>/Ext/Form.xml
    extract (category_folder, object_name, form_name).
    """
    try:
        rel = file_path.relative_to(code_dir)
    except Exception:
        return None
    parts = list(rel.parts)
    # Support two layouts:
    # 1) .../<cat>/<obj>/Forms/<form>/Ext/Form.xml
    # 2) .../CommonForms/<common_form>/Ext/Form.xml (managed form usually named 'Форма')
    try:
        idx_forms = parts.index("Forms")
    except ValueError:
        idx_forms = -1
    if idx_forms != -1:
        if idx_forms >= 2 and idx_forms + 2 < len(parts):
            category_folder = parts[idx_forms - 2]
            object_name = parts[idx_forms - 1]
            form_name = parts[idx_forms + 1]
            return category_folder, object_name, form_name
    # CommonForms path handling
    try:
        idx_common = parts.index("CommonForms")
    except ValueError:
        idx_common = -1
    if idx_common != -1:
        # Expect: CommonForms/<Name>/Ext/Form.xml
        if idx_common + 2 < len(parts):
            category_folder = "CommonForms"
            object_name = parts[idx_common + 1]
            form_name = "Форма"
            return category_folder, object_name, form_name
    return None

# ---- XCF canonical name helpers for GUID mapping (ConfigDumpInfo.xml) ----

# RU category -> XCF English prefix used in ConfigDumpInfo.xml
RU_TO_XCF_PREFIX: Dict[str, str] = {
    "Справочники": "Catalog",
    "Документы": "Document",
    "Перечисления": "Enum",
    "РегистрыСведений": "InformationRegister",
    "РегистрыНакопления": "AccumulationRegister",
    "ПланыСчетов": "ChartOfAccounts",
    "ПланыВидовХарактеристик": "ChartOfCharacteristicTypes",
    "ПланыВидовРасчета": "ChartOfCalculationTypes",
    "БизнесПроцессы": "BusinessProcess",
    "Задачи": "Task",
    "ЖурналыДокументов": "DocumentJournal",
    # Add more if needed
}

def xcf_prefix_from_ru_category(category_ru: str) -> Optional[str]:
    """
    Map RU category name used in parsed metadata to XCF prefix used in ConfigDumpInfo.xml.
    """
    return RU_TO_XCF_PREFIX.get(category_ru)

def _join_xcf(*parts: str) -> Optional[str]:
    try:
        segs = [p for p in parts if isinstance(p, str) and p.strip()]
        if not segs:
            return None
        return ".".join(segs)
    except Exception:
        return None

def xcf_name_object(category_ru: str, object_name: str) -> Optional[str]:
    pref = xcf_prefix_from_ru_category(category_ru)
    return _join_xcf(pref, object_name)

def xcf_name_attribute(category_ru: str, object_name: str, attr_name: str) -> Optional[str]:
    base = xcf_name_object(category_ru, object_name)
    return _join_xcf(base, "Attribute", attr_name)

def xcf_name_tabular_part(category_ru: str, object_name: str, tabular_name: str) -> Optional[str]:
    base = xcf_name_object(category_ru, object_name)
    # XCF uses "TabularSection" for tabular parts
    return _join_xcf(base, "TabularSection", tabular_name)

def xcf_name_tabular_attribute(category_ru: str, object_name: str, tabular_name: str, attr_name: str) -> Optional[str]:
    ts = xcf_name_tabular_part(category_ru, object_name, tabular_name)
    return _join_xcf(ts, "Attribute", attr_name)

def xcf_name_resource(category_ru: str, object_name: str, res_name: str) -> Optional[str]:
    base = xcf_name_object(category_ru, object_name)
    return _join_xcf(base, "Resource", res_name)

def xcf_name_dimension(category_ru: str, object_name: str, dim_name: str) -> Optional[str]:
    base = xcf_name_object(category_ru, object_name)
    return _join_xcf(base, "Dimension", dim_name)

def xcf_name_form(category_ru: str, object_name: str, form_name: str) -> Optional[str]:
    base = xcf_name_object(category_ru, object_name)
    return _join_xcf(base, "Form", form_name)
