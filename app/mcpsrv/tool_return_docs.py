"""Human documentation for MCP tool return payloads.

The contracts below describe the logical JSON payload before response
formatting, compact_refs/TOON serialization, and MCP text-block wrapping.

Examples are intentionally synthetic. Do not put project/customer object names
from real repositories here.
"""

from __future__ import annotations

from typing import Any, Dict, List


def field(name: str, type_: str, description: str, when: str = "Всегда") -> Dict[str, str]:
    return {"name": name, "type": type_, "description": description, "when": when}


def adoption_fields(prefix: str = "adoption", subject: str = "узла") -> List[Dict[str, str]]:
    return [
        field(
            prefix,
            "object",
            f"Информация о том, как {subject} связан с расширениями или базовой конфигурацией.",
            "Если в проекте есть расширения и этот tool возвращает adoption.",
        ),
        field(
            f"{prefix}.role",
            "string",
            'Роль в механизме расширений: "base" — базовый объект заимствован в расширениях; '
            '"extension" — объект находится в расширении и связан с базовым; "none" — связи с расширением нет.',
            "Если adoption возвращен.",
        ),
        field(
            f"{prefix}.extension_config_names",
            "array<string>",
            "Имена расширений, которые заимствуют этот базовый объект/элемент.",
            'Только если adoption.role="base".',
        ),
        field(
            f"{prefix}.base_config_name",
            "string",
            "Имя базовой конфигурации, с объектом/элементом которой связан объект/элемент расширения.",
            'Только если adoption.role="extension".',
        ),
    ]


OBJECT_FIELDS = [
    field("config_name", "string", "Имя конфигурации, в которой найден объект."),
    field("category", "string", "Категория объекта метаданных: Документы, Справочники, РегистрыСведений и т.п."),
    field("name", "string", "Имя объекта метаданных без пути."),
    field("qualified_name", "string", "Полный путь объекта в проекте и конфигурации."),
    *adoption_fields("adoption", "объект"),
]

ROUTINE_FIELDS = [
    field("id", "string", "Стабильный идентификатор процедуры/функции."),
    field("name", "string", "Имя процедуры или функции."),
    field("routine_type", "string", "Тип routine: Procedure или Function."),
    field("export", "boolean", "Признак Экспорт."),
    field("directives", "array<string>", "Директивы компиляции BSL."),
    field("config_name", "string", "Имя конфигурации."),
    field("owner_qualified_name", "string", "Полный путь владельца модуля."),
    field("module_type", "string", "Тип модуля: ObjectModule, ManagerModule, FormModule, CommonModule и т.п."),
    field("file_path", "string", "Путь к файлу модуля внутри выгрузки."),
    field("line", "integer", "Номер строки начала routine."),
]


# search_bsl_routines paged object-shape: fields common to all dialect-cases.
_SEARCH_BSL_COMMON_FIELDS = [
    field("context", "object", "Метаданные запроса: режим и источник строк."),
    field("context.mode", "string", "Режим запроса (echo входного mode)."),
    field("context.dialect", "string", 'Источник строк: "description_service" | "routine_fields". Определяет набор полей ответа.'),
    field("page", "object", "Метаданные страницы."),
    field("page.limit", "integer", "Запрошенный размер страницы."),
    field("page.offset", "integer", "Смещение страницы."),
    field("page.returned", "integer", "Число routines в ответе."),
    field("page.has_more", "boolean", "Есть ли следующая страница."),
    field("page.next_offset", "integer", "Offset следующей страницы.", "Только при has_more=true."),
    field("module_contexts", "array<object>", "Контексты модуля/файла, общие для routines. Response-local, без реального Module.id."),
    field("module_contexts[].module_key", "string", "Локальный ключ ответа (module1, module2, ...). НЕ передавать в get_bsl_modules как module_ref."),
    field("module_contexts[].config_name", "string", "Имя конфигурации."),
    field("module_contexts[].owner_qn", "string", "Полный qualified_name владельца модуля."),
    field("module_contexts[].owner_category", "string", "Категория владельца."),
    field("module_contexts[].module_type", "string", "Тип модуля."),
    field("routines", "array<object>", "Найденные routines."),
    field("routines[].module_key", "string", "Ссылка на module_contexts[].module_key."),
    field("routines[].id", "string", "Стабильный идентификатор routine."),
    field("routines[].name", "string", "Имя routine."),
    field("routines[].directives", "array<string>", "Директивы компиляции BSL."),
]

# search_bsl_routines: optional side tables shared by all dialect-cases.
_SEARCH_BSL_SIDE_FIELDS = [
    field("interceptions", "array<object>", "Связи routine с расширениями.", "Только если есть непустой interception."),
    field("interceptions[].routine_id", "string", "routines[].id."),
    field("interceptions[].role", "string", '"base" или "extension".'),
    field("interceptions[].base_config_name", "string", "Имя базовой конфигурации.", 'Только при role="extension".'),
    field("interceptions[].decorator", "string", "Decorator routine расширения.", 'Только при role="extension".'),
    field("interceptions[].base_routine_name", "string", "Имя routine в базе.", 'Только при role="extension".'),
    field("interceptions[].extension_config_names", "array<string>", "Имена расширений, перекрывающих routine.", 'Только при role="base".'),
    field("interceptions[].extension_decorators", "array<string>", "Декораторы routine в расширениях (порядок согласован).", 'Только при role="base".'),
    field("interceptions[].extension_routine_names", "array<string>", "Имена routine в расширениях (порядок согласован).", 'Только при role="base".'),
    field("callees", "array<object>", "Исходящие вызовы найденных routines.", "Если call_context_mode запрашивает исходящие вызовы."),
    field("callees[].routine_id", "string", "routines[].id, из которой идёт вызов."),
    field("callees[].callee_id", "string", "id вызываемой routine."),
    field("callees[].callee", "string", "Имя вызываемой routine."),
    field("callees[].callee_owner_qn", "string", "qualified_name владельца вызываемой routine."),
    field("callers", "array<object>", "Входящие вызовы найденных routines.", "Если call_context_mode запрашивает входящие вызовы."),
    field("callers[].routine_id", "string", "routines[].id, которую вызывают."),
    field("callers[].caller_id", "string", "id вызывающей routine."),
    field("callers[].caller", "string", "Имя вызывающей routine."),
    field("callers[].caller_owner_qn", "string", "qualified_name владельца вызывающей routine."),
]


TOOL_RETURN_DOCS: Dict[str, Dict[str, Any]] = {
    "get_metadata": {
        "summary": "Возвращает нормализованный обзор конфигураций, категорий или объектов метаданных.",
        "returns": [
            {
                "case": 'mode="summary"',
                "shape": "object{page, configurations, category_counts}",
                "fields": [
                    field("page", "object", "Метаданные ответа: {returned, has_more, truncated}."),
                    field("page.returned", "integer", "Количество строк в category_counts."),
                    field("page.has_more", "boolean", "Всегда false в summary (режим без пагинации)."),
                    field("page.truncated", "boolean", "true если ответ обрезан по query_max_results."),
                    field("configurations", "array<object>", "Уникальные конфигурации в ответе."),
                    field("configurations[].config_id", "string", "Локальный идентификатор cfgN — действителен только внутри одного ответа."),
                    field("configurations[].config_name", "string", "Имя конфигурации."),
                    field("configurations[].qualified_name", "string", "Полный путь конфигурации."),
                    field("configurations[].is_extension", "boolean", "Является ли конфигурация расширением."),
                    field("category_counts", "array<object>", "Количества объектов по конфигурации и категории."),
                    field("category_counts[].config_id", "string", "Ссылка на configurations[].config_id."),
                    field("category_counts[].category", "string", "Категория метаданных."),
                    field("category_counts[].object_count", "integer", "Количество объектов в категории конфигурации."),
                ],
            },
            {
                "case": 'mode="configurations"',
                "shape": "object{page, configurations}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}."),
                    field("configurations", "array<object>", "Базовая конфигурация и её расширения."),
                ],
            },
            {
                "case": 'mode="categories"',
                "shape": "object{page, configurations, category_groups}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. returned считает категории, а не группы."),
                    field("configurations", "array<object>", "Конфигурации, упомянутые в category_groups."),
                    field("category_groups", "array<object>", "Группы категорий с общим config_id/QN-префиксом."),
                    field("category_groups[].config_id", "string", "Ссылка на configurations[].config_id."),
                    field("category_groups[].qualified_name_prefix", "string", "Общий QN-префикс; полный путь категории = qualified_name_prefix + \"/\" + categories[].category."),
                    field("category_groups[].categories", "array<object>", "Категории метаданных группы."),
                    field("category_groups[].categories[].category", "string", "Категория метаданных."),
                ],
            },
            {
                "case": 'mode="objects"',
                "shape": "object{page, configurations, object_groups}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. returned считает объекты, а не группы."),
                    field("configurations", "array<object>", "Конфигурации, упомянутые в object_groups."),
                    field("object_groups", "array<object>", "Группы объектов с общим config_id/category/QN-префиксом."),
                    field("object_groups[].config_id", "string", "Ссылка на configurations[].config_id."),
                    field("object_groups[].category", "string", "Категория объектов группы."),
                    field("object_groups[].qualified_name_prefix", "string", "Общий QN-префикс; полный путь объекта = qualified_name_prefix + \"/\" + objects[].name."),
                    field("object_groups[].objects", "array<object>", "Объекты метаданных группы."),
                    field("object_groups[].objects[].name", "string", "Имя объекта."),
                    *adoption_fields("object_groups[].objects[].adoption", "объект"),
                ],
            },
        ],
        "examples": [
            {
                "case": 'mode="summary"',
                "json": {
                    "page": {"returned": 1, "has_more": False, "truncated": False},
                    "configurations": [
                        {
                            "config_id": "cfg1",
                            "config_name": "КонфигурацияЗУП",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП",
                            "is_extension": False,
                        }
                    ],
                    "category_counts": [
                        {"config_id": "cfg1", "category": "Документы", "object_count": 42}
                    ],
                },
            },
            {
                "case": 'mode="categories"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 2, "has_more": False},
                    "configurations": [
                        {
                            "config_id": "cfg1",
                            "config_name": "КонфигурацияЗУП",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП",
                            "is_extension": False,
                        }
                    ],
                    "category_groups": [
                        {
                            "config_id": "cfg1",
                            "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП",
                            "categories": [
                                {"category": "Документы"},
                                {"category": "Справочники"},
                            ],
                        }
                    ],
                },
            },
            {
                "case": 'mode="objects"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "configurations": [
                        {
                            "config_id": "cfg1",
                            "config_name": "КонфигурацияЗУП",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП",
                            "is_extension": False,
                        }
                    ],
                    "object_groups": [
                        {
                            "config_id": "cfg1",
                            "category": "Документы",
                            "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                            "objects": [
                                {
                                    "name": "ДокументРасчета",
                                    "adoption": {"role": "none"},
                                }
                            ],
                        }
                    ],
                },
            },
        ],
        "notes": [
            "config_id (cfgN) — локальный идентификатор только внутри одного ответа; не сопоставляйте между страницами.",
            "В mode=\"summary\" page.truncated=true означает, что ответ обрезан по query_max_results.",
        ],
    },
    "find_metadata_objects": {
        "summary": "Ищет объекты метаданных по описанию, дочерним элементам, формам, командам, макетам или предопределённым.",
        "returns": [
            {
                "case": "Все режимы search_by",
                "shape": "object{page, configurations, object_groups}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. returned считает объекты, а не группы."),
                    field("configurations", "array<object>", "Конфигурации, упомянутые в object_groups."),
                    field("configurations[].config_id", "string", "Локальный идентификатор cfgN — действителен только внутри одного ответа."),
                    field("configurations[].config_name", "string", "Имя конфигурации."),
                    field("configurations[].qualified_name", "string", "Полный путь конфигурации."),
                    field("configurations[].is_extension", "boolean", "Является ли конфигурация расширением."),
                    field("object_groups", "array<object>", "Группы объектов с общим config_id/category/QN-префиксом."),
                    field("object_groups[].config_id", "string", "Ссылка на configurations[].config_id."),
                    field("object_groups[].category", "string", "Категория объектов группы."),
                    field("object_groups[].qualified_name_prefix", "string", "Общий QN-префикс; полный путь объекта = qualified_name_prefix + \"/\" + objects[].name."),
                    field("object_groups[].objects", "array<object>", "Объекты метаданных группы."),
                    field("object_groups[].objects[].name", "string", "Имя объекта."),
                    *adoption_fields("object_groups[].objects[].adoption", "объект"),
                    field("object_groups[].objects[].synonym", "string", "Синоним объекта.", 'При search_by="description".'),
                    field("object_groups[].objects[].comment", "string", "Комментарий объекта.", 'При search_by="description".'),
                    field("object_groups[].objects[].explanation", "string", "Пояснение/назначение объекта.", 'При search_by="description".'),
                    field("object_groups[].objects[].score", "number|null", "Итоговая оценка релевантности.", 'При search_by="description".'),
                    field("object_groups[].objects[].similarity", "number|null", "Векторная близость.", 'При search_by="description"; null, если неприменимо.'),
                    field("object_groups[].objects[].fulltext_score", "number|null", "Нормированный fulltext-скор.", 'При search_by="description"; null, если неприменимо.'),
                    field("object_groups[].objects[].vector_score", "number|null", "Нормированный векторный скор.", 'При search_by="description"; null, если неприменимо.'),
                    field("object_groups[].objects[].hybrid_score", "number|null", "Гибридный скор (blend/RRF).", 'При search_by="description"; null, если неприменимо.'),
                    field("object_groups[].objects[].help_text", "string", "Полный текст справки объекта.", 'При search_by="description" и include_help_text=true.'),
                    field("object_groups[].objects[].form_qn", "string", "Путь формы, в которой найдено совпадение.", 'При search_by="form_control".'),
                ],
            }
        ],
        "examples": [
            {
                "case": 'search_by="description"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "configurations": [
                        {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False}
                    ],
                    "object_groups": [
                        {
                            "config_id": "cfg1",
                            "category": "Документы",
                            "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                            "objects": [
                                {
                                    "name": "ДокументРасчета",
                                    "adoption": {"role": "none"},
                                    "synonym": "Синоним",
                                    "comment": "",
                                    "explanation": "",
                                    "score": 0.7821,
                                    "similarity": 0.81,
                                    "fulltext_score": 0.42,
                                    "vector_score": 0.81,
                                    "hybrid_score": 0.7821,
                                }
                            ],
                        }
                    ],
                },
            }
        ],
        "notes": [
            "Состав дополнительных полей в objects[] зависит от search_by.",
            "score-поля для description стабильны: отсутствующие значения возвращаются как null.",
            "help_text добавляется только при include_help_text=true; по умолчанию ответ компактный.",
            "config_id (cfgN) — локальный идентификатор только внутри одного ответа; не сопоставляйте между страницами.",
        ],
    },
    "get_metadata_object_structure": {
        "summary": "Возвращает структуру и дочерние элементы выбранного объекта метаданных.",
        "returns": [
            {
                "case": 'sections содержит "overview"',
                "shape": "array<object>",
                "fields": [
                    field("config_name", "string", "Имя конфигурации."),
                    field("qualified_name", "string", "Полный путь объекта."),
                    field("object", "string", "Имя объекта-владельца."),
                    field("attributes", "array<string>", "Имена реквизитов объекта."),
                    field("resources", "array<string>", "Имена ресурсов регистра."),
                    field("dimensions", "array<string>", "Имена измерений регистра."),
                    field("tabularParts", "array<object>", "Табличные части с кратким составом реквизитов."),
                    field("tabularParts[].name", "string", "Имя табличной части."),
                    field("tabularParts[].attributes", "array<string>", "Имена реквизитов табличной части."),
                    *adoption_fields("adoption", "объект"),
                ],
            },
            {
                "case": 'sections содержит "attributes", "tabular_parts", "tabular_attributes", "resources", "dimensions", "commands", "layouts", "enum_values" или "journal_graphs"',
                "shape": "array<object> или object с массивом по имени section",
                "fields": [
                    field("<section_name>", "array<object>", "Ключ с именем section.", "Если запрошено несколько sections."),
                    field("[].name", "string", "Имя дочернего элемента."),
                    field("[].qualified_name", "string", "Полный путь дочернего элемента."),
                    field("[].config_name", "string", "Имя конфигурации объекта-владельца."),
                    field("[].owner_qn", "string", "Полный путь владельца. Для tabular_attributes это путь табличной части."),
                    *adoption_fields("[].adoption", "элемент"),
                ],
            },
            {
                "case": 'sections содержит "forms"',
                "shape": "array<object> или object.forms",
                "fields": [
                    field("[].name", "string", "Имя формы."),
                    field("[].qualified_name", "string", "Полный путь формы."),
                    field("[].config_name", "string", "Имя конфигурации."),
                    field("[].owner_qn", "string", "Полный путь объекта-владельца."),
                    field("[].role", "string", "Роль формы из связи HAS_FORM."),
                    field("[].is_default", "boolean", "Признак формы по умолчанию."),
                    *adoption_fields("[].adoption", "форма"),
                ],
            },
            {
                "case": 'sections содержит "default_forms"',
                "shape": "array<object> или object.default_forms",
                "fields": [
                    field("[].role", "string", "Роль формы по умолчанию."),
                    field("[].name", "string", "Имя формы."),
                    field("[].qualified_name", "string", "Полный путь формы."),
                    field("[].config_name", "string", "Имя конфигурации."),
                    field("[].owner_qn", "string", "Полный путь объекта-владельца."),
                ],
            },
            {
                "case": 'sections содержит "predefined"',
                "shape": "array<object> или object.predefined",
                "fields": [
                    field("[].name", "string", "Имя предопределенного значения."),
                    field("[].config_name", "string", "Имя конфигурации."),
                    field("[].owner_qn", "string", "Полный путь объекта-владельца."),
                    field("[].qualified_name", "string", "Полный путь предопределенного значения."),
                    field("[].code", "string", "Код предопределенного значения."),
                    field("[].description", "string", "Наименование/описание предопределенного значения."),
                ],
            },
            {
                "case": 'sections содержит "url_templates"',
                "shape": "array<object> или object.url_templates",
                "fields": [
                    field("[].name", "string", "Имя URL-шаблона."),
                    field("[].qualified_name", "string", "Полный путь URL-шаблона."),
                    field("[].config_name", "string", "Имя конфигурации HTTP-сервиса."),
                    field("[].owner_qn", "string", "Полный путь HTTP-сервиса."),
                    field("[].pattern", "string", "Шаблон URL."),
                    *adoption_fields("[].adoption", "URL-шаблон"),
                ],
            },
            {
                "case": 'sections содержит "url_methods"',
                "shape": "array<object> или object.url_methods",
                "fields": [
                    field("[].name", "string", "Имя URL-метода."),
                    field("[].qualified_name", "string", "Полный путь URL-метода."),
                    field("[].config_name", "string", "Имя конфигурации HTTP-сервиса."),
                    field("[].owner_qn", "string", "Полный путь URL-шаблона."),
                    field("[].httpMethod", "string", "HTTP-метод."),
                    field("[].handler", "string", "Имя обработчика."),
                    *adoption_fields("[].adoption", "URL-метод"),
                ],
            },
        ],
        "examples": [
            {
                "case": 'sections=["forms","commands"]',
                "json": {
                    "forms": [
                        {
                            "name": "ФормаДокумента",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента",
                            "config_name": "КонфигурацияЗУП",
                            "owner_qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                            "role": "object",
                            "is_default": True,
                        }
                    ],
                    "commands": [
                        {
                            "name": "Провести",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Command/Провести",
                            "config_name": "КонфигурацияЗУП",
                            "owner_qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                        }
                    ],
                },
            }
        ],
        "notes": ["Один section обычно возвращается как массив, несколько sections — как объект по именам sections."],
    },
    "find_metadata_elements": {
        "summary": "Ищет дочерние элементы метаданных по всему проекту.",
        "returns": [
            {
                "case": "Любой element_type",
                "shape": "object{page, elements}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. next_offset присутствует только при has_more=true."),
                    field("elements", "array<object>", "Найденные элементы с контекстом объекта-владельца."),
                    field("elements[].config_name", "string", "Имя конфигурации."),
                    field("elements[].category", "string", "Категория объекта-владельца.", 'Для element_type, возвращающих category (attribute, attributes_of_matching_objects, tabular_attribute, journal_graph).'),
                    field("elements[].object", "string", "Имя объекта-владельца."),
                    field("elements[].owner_qn", "string", "Полный путь объекта-владельца."),
                    field("elements[].name", "string", "Имя найденного элемента."),
                    field("elements[].qualified_name", "string", "Полный путь найденного элемента."),
                    field("elements[].role", "string", "Роль формы: object/group/list/picker/group_picker.", 'Только element_type="form".'),
                    field("elements[].is_default", "boolean", "Является ли форма основной для объекта.", 'Только element_type="form".'),
                    field("elements[].object_qn", "string", "Полный путь объекта-владельца формы.", 'Только element_type="form_attribute".'),
                    *adoption_fields("elements[].adoption", "элемент"),
                ],
            }
        ],
        "examples": [
            {
                "case": 'element_type="attribute"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "elements": [
                        {
                            "config_name": "КонфигурацияЗУП",
                            "category": "Документы",
                            "object": "ДокументРасчета",
                            "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                            "name": "Организация",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация",
                        }
                    ],
                },
            }
        ],
        "notes": ["Состав дополнительных полей в elements[] зависит от element_type."],
    },
    "find_metadata_usages": {
        "summary": "Ищет использования объекта или документы, которые делают движения по регистру.",
        "returns": [
            {
                "case": 'mode="objects" или mode="register_movements"',
                "shape": "object{page, configurations, object_groups}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. returned считает объекты, а не группы."),
                    field("configurations", "array<object>", "Конфигурации, упомянутые в object_groups."),
                    field("configurations[].config_id", "string", "Локальный идентификатор cfgN — действителен только внутри одного ответа."),
                    field("configurations[].config_name", "string", "Имя конфигурации."),
                    field("configurations[].qualified_name", "string", "Полный путь конфигурации."),
                    field("configurations[].is_extension", "boolean", "Является ли конфигурация расширением."),
                    field("object_groups", "array<object>", "Группы объектов с общим config_id/category/QN-префиксом."),
                    field("object_groups[].config_id", "string", "Ссылка на configurations[].config_id."),
                    field("object_groups[].category", "string", "Категория объектов группы (для register_movements — Документы)."),
                    field("object_groups[].qualified_name_prefix", "string", "Общий QN-префикс; полный путь объекта = qualified_name_prefix + \"/\" + objects[].name."),
                    field("object_groups[].objects", "array<object>", "Объекты метаданных группы."),
                    field("object_groups[].objects[].name", "string", "Имя объекта."),
                    *adoption_fields("object_groups[].objects[].adoption", "объект"),
                ],
            },
            {
                "case": 'mode="paths"',
                "shape": "object{page, paths}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. returned считает пути."),
                    field("paths", "array<object>", "Найденные 1С-пути использования."),
                    field("paths[].target_config_name", "string", "Конфигурация целевого объекта."),
                    field("paths[].target_qn", "string", "Полный путь целевого объекта."),
                    field("paths[].config_name", "string", "Конфигурация объекта, где найдено использование."),
                    field("paths[].path", "string", "Путь свойства/элемента, через который найдено использование."),
                ],
            },
        ],
    },
    "get_metadata_element_type": {
        "summary": (
            "Возвращает типы (Тип) типизированных дочерних элементов объекта метаданных "
            "одним вызовом. Покрывает реквизиты, реквизиты адресации задач, реквизиты ТЧ, "
            "ресурсы, измерения, признаки учёта, признаки учёта субконто и реквизиты форм."
        ),
        "returns": [
            {
                "case": "Любой набор element_type",
                "shape": "object",
                "fields": [
                    field("overview", "object", "Шапка ответа."),
                    field("overview.object", "string", "Имя объекта."),
                    field("overview.qualified_name", "string", "Полный путь объекта."),
                    field(
                        "overview.config", "string",
                        "Имя конфигурации, если был задан фильтр config.",
                        "Если был задан config.",
                    ),
                    field(
                        "attribute", "array<object>",
                        "Обычные реквизиты объекта (без признака адресации).",
                        "Если категория attribute была запрошена и есть данные.",
                    ),
                    field(
                        "addressing_attribute", "array<object>",
                        "Реквизиты адресации (для объектов категории Задачи).",
                        "Если категория addressing_attribute была запрошена и есть данные.",
                    ),
                    field(
                        "tabular_attributes", "object",
                        "Словарь по имени ТЧ; значение — массив строк реквизитов этой ТЧ.",
                        "Если категория tabular_attribute была запрошена и есть данные.",
                    ),
                    field(
                        "resource", "array<object>",
                        "Ресурсы регистра.",
                        "Если категория resource была запрошена и есть данные.",
                    ),
                    field(
                        "dimension", "array<object>",
                        "Измерения регистра.",
                        "Если категория dimension была запрошена и есть данные.",
                    ),
                    field(
                        "accounting_flag", "array<object>",
                        "Признаки учёта (План счетов).",
                        "Если категория accounting_flag была запрошена и есть данные.",
                    ),
                    field(
                        "dimension_accounting_flag", "array<object>",
                        "Признаки учёта субконто (План счетов).",
                        "Если категория dimension_accounting_flag была запрошена и есть данные.",
                    ),
                    field(
                        "form_attributes", "object",
                        "Словарь по имени формы; значение — массив строк реквизитов этой формы.",
                        "Если категория form_attribute была явно запрошена и есть данные.",
                    ),
                    field("<element>.name", "string", "Имя элемента."),
                    field("<element>.qualified_name", "string", "Полный путь элемента."),
                    field("<element>.owner_qn", "string", "Полный путь владельца (объект, ТЧ или форма)."),
                    field("<element>.config_name", "string", "Имя конфигурации этого элемента."),
                    field(
                        "<element>.type", "string",
                        "Тип значения: одиночный атом или несколько атомов через '|' для составного типа. null если тип не определён.",
                    ),
                    *adoption_fields("<element>.adoption", "элемент"),
                ],
            }
        ],
    },
    "find_predefined_values": {
        "summary": "Ищет предопределенные значения объектов метаданных, сгруппированные по объекту-владельцу в paged-object формате.",
        "returns": [
            {
                "case": "Любой mode",
                "shape": "object{page, object_groups}",
                "fields": [
                    field("page", "object", "Пагинация текущей страницы."),
                    field("page.limit", "integer", "Запрошенный лимит строк."),
                    field("page.offset", "integer", "Смещение текущей страницы."),
                    field("page.returned", "integer", "Сколько предопределенных значений возвращено на странице (считается по значениям, а не по группам object_groups)."),
                    field("page.has_more", "boolean", "Есть ли ещё значения за текущей страницей."),
                    field("page.next_offset", "integer", "Offset для следующей страницы; считается по предопределенным значениям. Одна группа-владелец может быть разрезана границей страницы — продолжение придёт на следующей странице.", "Только если has_more=true."),
                    field("object_groups", "array<object>", "Группы объектов-владельцев; значения одного объекта собраны в одну группу."),
                    field("object_groups[].config_name", "string", "Имя конфигурации объекта-владельца."),
                    field("object_groups[].category", "string", "Категория объекта-владельца."),
                    field("object_groups[].object", "string", "Имя объекта-владельца."),
                    field("object_groups[].owner_qn", "string", "Полный путь объекта-владельца."),
                    field("object_groups[].predefined", "array<object>", "Предопределенные значения этого объекта; поля зависят от mode."),
                    field("object_groups[].predefined[].name", "string", "Имя предопределенного значения."),
                    field("object_groups[].predefined[].qualified_name", "string", "Полный путь предопределенного значения."),
                    field("object_groups[].predefined[].code", "string", "Код значения.", 'В режиме name, если код задан.'),
                    field("object_groups[].predefined[].description", "string", "Представление/описание значения.", 'В режиме name, если описание задано.'),
                    field("object_groups[].predefined[].flag_name", "string", "Имя учетного флага.", 'В режиме flag.'),
                    field("object_groups[].predefined[].flag_value", "boolean", "Значение учетного флага.", 'В режиме flag.'),
                    field("object_groups[].predefined[].account_type", "string", "Тип счета (ТипСчета).", 'В режиме account_type.'),
                    field("object_groups[].predefined[].subconto_kind", "string", "Совпавший вид субконто.", 'В режиме subconto_type.'),
                    *adoption_fields("object_groups[].predefined[].adoption", "предопределенное значение"),
                ],
            }
        ],
        "examples": [
            {
                "case": "Значение справочника",
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "object_groups": [
                        {
                            "config_name": "КонфигурацияЗУП",
                            "category": "Справочники",
                            "object": "ВидыЗанятости",
                            "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/ВидыЗанятости",
                            "predefined": [
                                {
                                    "name": "ОсновноеМестоРаботы",
                                    "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Справочники/ВидыЗанятости/Predefined/ОсновноеМестоРаботы",
                                    "code": "000000001",
                                    "description": "Основное место работы",
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    },
    "get_access_rights": {
        "summary": "Возвращает роли и права доступа к объектам или целям роли.",
        "returns": [
            {
                "case": 'mode="roles_for_target"',
                "shape": "array<object>",
                "fields": [
                    field("role", "string", "Имя роли."),
                    field("role_qn", "string", "Полный путь роли."),
                    field("config_name", "string", "Имя конфигурации."),
                    field("rights", "array<object>", "Права этой роли на целевой объект."),
                    field("rights[].right_ru", "string", "Имя права на русском."),
                    field("rights[].allowed", "boolean", "Разрешено ли право."),
                    field("rights[].has_condition", "boolean", "Есть ли условие права."),
                ],
            },
            {
                "case": 'mode="targets_of_role"',
                "shape": "array<object>",
                "fields": [
                    field("role", "string", "Имя роли."),
                    field("role_qn", "string", "Полный путь роли."),
                    field("config_name", "string", "Имя конфигурации."),
                    field("target_label", "string", "Тип целевого узла в графе."),
                    field("target_name", "string", "Имя целевого объекта."),
                    field("target_qn", "string", "Полный путь целевого объекта."),
                    field("rights", "array<object>", "Права роли на целевой объект."),
                ],
            },
            {
                "case": 'mode="role_rights_to_target"',
                "shape": "array<object>",
                "fields": [
                    field("role", "string", "Имя роли."),
                    field("role_qn", "string", "Полный путь роли."),
                    field("config_name", "string", "Имя конфигурации."),
                    field("target_label", "string", "Тип целевого узла в графе."),
                    field("target_name", "string", "Имя целевого объекта."),
                    field("target_qn", "string", "Полный путь целевого объекта."),
                    field("rights", "array<object>", "Права роли на целевой объект."),
                    field("rights[].condition", "string", "Текст условия права; \"\" если условия нет.", 'Только при include_conditions=true.'),
                ],
            },
        ],
        "examples": [
            {
                "case": 'mode="roles_for_target"',
                "json": [
                    {
                        "role": "РольКадровика",
                        "role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика",
                        "config_name": "КонфигурацияЗУП",
                        "rights": [
                            {"right_ru": "Чтение", "allowed": True, "has_condition": False},
                            {"right_ru": "Изменение", "allowed": True, "has_condition": True},
                        ],
                    }
                ],
            }
        ],
    },
    "get_metadata_details": {
        "summary": "Разрешает ссылку/GUID или возвращает отфильтрованные свойства узла метаданных.",
        "returns": [
            {
                "case": 'mode="resolve"',
                "shape": "object{page, nodes}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. next_offset присутствует только при has_more=true."),
                    field("nodes", "array<object>", "Найденные узлы."),
                    field("nodes[].kind", "string", "Тип узла графа."),
                    field("nodes[].qualified_name", "string", "Полный путь найденного узла."),
                    field("nodes[].name", "string", "Имя узла."),
                    field("nodes[].config_name", "string", "Имя конфигурации."),
                    field("nodes[].category", "string", "Категория объекта.", 'ref_type="object" и "guid".'),
                    field("nodes[].object", "string", "Имя объекта-владельца.", 'Только ref_type="guid".'),
                    field("nodes[].tabular", "string", "Имя табличной части.", 'Только ref_type="guid".'),
                    field("nodes[].id", "string", "Идентификатор Routine.", 'Только ref_type="routine_id".'),
                    field("nodes[].owner_qn", "string", "QN владельца Routine.", 'Только ref_type="routine_id".'),
                ],
            },
            {
                "case": 'mode="properties"',
                "shape": "object{page, nodes, properties, help?}",
                "fields": [
                    field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}. returned считает узлы."),
                    field("nodes", "array<object>", "Найденные узлы с контекстом."),
                    field("nodes[].kind", "string", "Тип узла графа."),
                    field("nodes[].node_qn", "string", "Канонический join-ключ узла: qualified_name, либо \"\" для routine (тогда идентичность в nodes[].id)."),
                    field("nodes[].qualified_name", "string", "Полный путь узла (может быть пустым для Routine)."),
                    field("nodes[].name", "string", "Имя узла."),
                    field("nodes[].config_name", "string", "Имя конфигурации."),
                    field("nodes[].category", "string", "Категория объекта.", "Для объектов метаданных."),
                    field("nodes[].id", "string", "Идентификатор Routine.", 'Только ref_type="routine_id".'),
                    field("nodes[].property_count", "number", "Число свойств узла в properties[] после фильтрации."),
                    field("nodes[].help_available", "boolean", "Есть ли у узла непустое поле Справка."),
                    *adoption_fields("nodes[].adoption", "узел"),
                    field("properties", "array<object>", "Отфильтрованные свойства всех узлов; служебные/технические поля исключены."),
                    field("properties[].node_qn", "string", "Ссылка на nodes[].node_qn."),
                    field("properties[].property", "string", "Имя свойства (или @prop:N при compact refs)."),
                    field("properties[].value", "string|number|boolean|null", "Значение свойства; свойство-массив отдаётся строкой через \"|\"."),
                    field("property_names", "object", "Словарь {@prop:N -> имя свойства}.", "Только при compact refs и более чем одном свойстве."),
                    field("help", "array<object>", "Текст Справки по узлам.", "Только при include_help=true."),
                    field("help[].node_qn", "string", "Ссылка на nodes[].node_qn."),
                    field("help[].text", "string", "Текст Справки узла."),
                ],
            },
        ],
        "examples": [
            {
                "case": 'mode="resolve"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "nodes": [
                        {
                            "kind": "MetadataObject",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                            "name": "ДокументРасчета",
                            "config_name": "КонфигурацияЗУП",
                        }
                    ],
                },
            },
            {
                "case": 'mode="properties", ref_type="object"',
                "json": {
                    "page": {"limit": 1, "offset": 0, "returned": 1, "has_more": False},
                    "nodes": [
                        {
                            "kind": "MetadataObject",
                            "node_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники",
                            "name": "Сотрудники",
                            "config_name": "КонфигурацияЗУП",
                            "category": "Справочники",
                            "property_count": 2,
                            "help_available": False,
                        }
                    ],
                    "properties": [
                        {"node_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники", "property": "Синоним", "value": "Сотрудники"},
                        {"node_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники", "property": "ИспользуемыеСтандартныеРеквизиты", "value": "Код|Наименование"},
                    ],
                },
            },
        ],
        "notes": [
            "properties[] содержит отфильтрованные простые свойства узла; служебные поля (body, embedding, console_*, object_summary_*, project_name, Справка) исключены.",
            "Свойство-массив сериализуется в одну строку через \"|\".",
            "Справка не попадает в properties[]; её текст доступен только при include_help=true в секции help[].",
        ],
    },
    "get_form_structure": {
        "summary": "Возвращает структуру формы: элементы, реквизиты, команды, события, обработчики и связи.",
        "returns": [
            {
                "case": "Общая структура ответа (любые sections)",
                "shape": "object{context, pages, controls?, form_events?, event_actions?, event_handlers?, form_attributes?, form_commands?, command_usages?, forms?, bindings?}",
                "fields": [
                    field("context", "object", "Авторитетный заголовок: описывает весь ответ."),
                    field("context.object", "string", "Имя объекта или общей формы."),
                    field("context.category", "string", "Категория объекта."),
                    field("context.object_qn", "string", "Полный путь объекта."),
                    field("context.config_name", "string", "Имя конфигурации."),
                    field("context.form_name", "string", "Имя формы.", "Для одной формы (обычной или общей)."),
                    field("context.form_qn", "string", "Полный путь формы.", "Для одной формы."),
                    field("context.forms_scope", "string", 'Равно "all", когда bindings запрошены без form_name (скан всех форм объекта).', "Для bindings без form_name."),
                    field("pages", "object", "Пагинация по секциям: pages.<section> = {limit, offset, returned, has_more, next_offset?}. next_offset присутствует только при has_more=true. event_actions записи в pages не имеет (производная от form_events)."),
                ],
            },
            {
                "case": 'sections содержит "controls"',
                "shape": "object{context, pages, controls}",
                "fields": [
                    field("controls[].name", "string", "Имя элемента формы."),
                    field("controls[].qualified_name", "string", "Полный путь элемента формы."),
                    field("controls[].type", "string", "Тип элемента формы."),
                    field("controls[].id", "string", "Идентификатор элемента формы."),
                    field("controls[].parent", "string", "Имя родительского элемента."),
                    field("controls[].parent_id", "string", "Идентификатор родительского элемента."),
                    *adoption_fields("controls[].adoption", "элемент формы"),
                ],
            },
            {
                "case": 'sections содержит "events"',
                "shape": "object{context, pages, form_events, event_actions}",
                "fields": [
                    field("form_events[].event_qn", "string", "Полный путь события."),
                    field("form_events[].event", "string", "Имя события."),
                    field("form_events[].source", "string", 'Источник события: "form" или имя элемента формы.'),
                    field("form_events[].source_qn", "string", "Полный путь элемента-источника; пусто для события формы."),
                    *adoption_fields("form_events[].adoption", "событие формы"),
                    field("event_actions[].event_qn", "string", "Ссылка на form_events[].event_qn. Содержит действия только для событий текущей страницы form_events."),
                    field("event_actions[].call_type", "string", "Тип вызова обработчика."),
                    field("event_actions[].handler_name", "string", "Имя обработчика."),
                ],
            },
            {
                "case": 'sections содержит "event_handlers"',
                "shape": "object{context, pages, event_handlers}",
                "fields": [
                    field("event_handlers[].event", "string", "Имя события."),
                    field("event_handlers[].event_qn", "string", "Полный путь события."),
                    field("event_handlers[].call_type", "string", "Тип вызова обработчика."),
                    field("event_handlers[].handler_name", "string", "Имя обработчика."),
                    field("event_handlers[].routine_id", "string", "Идентификатор routine-обработчика."),
                    field("event_handlers[].routine", "string", "Имя routine-обработчика."),
                    field("event_handlers[].routine_owner_qn", "string", "Полный путь владельца routine."),
                    field("event_handlers[].source_kind", "string", "Источник обработчика: Form или элемент формы.", "Если form_event_source=\"all\"."),
                ],
            },
            {
                "case": 'sections содержит "attributes" или "commands"',
                "shape": "object{context, pages, form_attributes?, form_commands?}",
                "fields": [
                    field("form_attributes[].name", "string", "Имя реквизита формы."),
                    field("form_attributes[].qualified_name", "string", "Полный путь реквизита формы."),
                    *adoption_fields("form_attributes[].adoption", "реквизит формы"),
                    field("form_commands[].name", "string", "Имя команды формы."),
                    field("form_commands[].qualified_name", "string", "Полный путь команды формы."),
                    *adoption_fields("form_commands[].adoption", "команда формы"),
                ],
            },
            {
                "case": 'sections содержит "command_usages"',
                "shape": "object{context, pages, command_usages}",
                "fields": [
                    field("command_usages[].control", "string", "Имя элемента формы, связанного с командой."),
                    field("command_usages[].control_qn", "string", "Полный путь элемента формы."),
                    field("command_usages[].button_id", "string", "Идентификатор кнопки/представления команды."),
                    field("command_usages[].button_name", "string", "Представление кнопки/команды."),
                    field("command_usages[].command", "string", "Имя команды."),
                    field("command_usages[].command_qn", "string", "Полный путь команды."),
                ],
            },
            {
                "case": 'sections содержит "bindings"',
                "shape": "object{context, pages, bindings, forms?}",
                "fields": [
                    field("forms[].form_id", "string", "Локальный идентификатор формы внутри ответа (form1, form2, ...).", "Для bindings без form_name (несколько форм)."),
                    field("forms[].name", "string", "Имя формы.", "Для bindings без form_name."),
                    field("forms[].qualified_name", "string", "Полный путь формы.", "Для bindings без form_name."),
                    field("bindings[].form_id", "string", "Ссылка на forms[].form_id.", "Для bindings без form_name."),
                    field("bindings[].control", "string", "Имя элемента формы."),
                    field("bindings[].control_qn", "string", "Полный путь элемента формы."),
                    field("bindings[].target_type", "string", "Тип цели привязки: attribute, dimension, resource, form_attribute, metadata_object."),
                    field("bindings[].target_name", "string", "Имя целевого узла привязки."),
                    field("bindings[].target_qn", "string", "Полный путь целевого узла привязки."),
                    field("bindings[].via", "string", "Тип/способ связи привязки."),
                ],
            },
        ],
        "examples": [
            {
                "case": 'sections=["controls"], form_name задан',
                "json": {
                    "context": {"object": "ДокументРасчета", "category": "Документы", "object_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "form_name": "ФормаДокумента", "form_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"},
                    "pages": {"controls": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}},
                    "controls": [{"name": "ГруппаОсновная", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ГруппаОсновная", "type": "ГруппаФормы", "id": "GroupMain", "parent": "", "parent_id": ""}],
                },
            }
        ],
    },
    "find_form_links": {
        "summary": "Ищет привязки элементов форм к метаданным и обработчики событий форм в едином paged-object формате.",
        "returns": [
            {
                "case": "Любой mode",
                "shape": "object{page, links}",
                "fields": [
                    field("page", "object", "Пагинация текущей страницы."),
                    field("page.limit", "integer", "Запрошенный лимит строк."),
                    field("page.offset", "integer", "Смещение текущей страницы."),
                    field("page.returned", "integer", "Сколько строк возвращено на странице."),
                    field("page.has_more", "boolean", "Есть ли ещё строки за текущей страницей."),
                    field("page.next_offset", "integer", "Offset для следующей страницы.", "Только если has_more=true."),
                    field("links", "array<object>", "Найденные связи; поля зависят от mode."),
                    field("links[].object", "string", "Имя объекта метаданных, владеющего формой."),
                    field("links[].form", "string", "Имя формы."),
                    field("links[].config_name", "string", "Имя конфигурации."),
                    field("links[].control", "string", "Имя элемента формы.", "В режиме controls_bound_to."),
                    field("links[].control_qn", "string", "Полный путь элемента формы.", "В режиме controls_bound_to."),
                    field("links[].target_label", "string", "Вид целевого узла привязки (Attribute/Dimension/Resource/FormAttribute/MetadataObject).", "В режиме controls_bound_to."),
                    field("links[].target_name", "string", "Имя цели привязки.", "В режиме controls_bound_to."),
                    field("links[].target_qn", "string", "Полный путь цели привязки.", "В режиме controls_bound_to."),
                    field("links[].via", "string", "Способ привязки (например data_path или list).", "В режиме controls_bound_to."),
                    field("links[].source", "string", "Источник события: 'Form' или имя элемента формы.", "В режиме events_handled_by_routine."),
                    field("links[].event", "string", "Имя события формы или элемента.", "В режиме events_handled_by_routine."),
                    field("links[].call_type", "string", "Тип вызова обработчика.", "В режиме events_handled_by_routine."),
                    field("links[].routine_id", "string", "Идентификатор процедуры-обработчика.", "В режиме events_handled_by_routine."),
                    field("links[].routine", "string", "Имя процедуры-обработчика.", "В режиме events_handled_by_routine."),
                    field("links[].routine_owner_qn", "string", "Полный путь владельца обработчика.", "В режиме events_handled_by_routine."),
                ],
            }
        ],
        "examples": [
            {
                "case": 'mode="controls_bound_to"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "links": [
                        {
                            "object": "ДокументРасчета",
                            "form": "ФормаДокумента",
                            "config_name": "КонфигурацияЗУП",
                            "control": "ПолеСотрудник",
                            "control_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ПолеСотрудник",
                            "target_label": "Attribute",
                            "target_name": "Сотрудник",
                            "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Сотрудник",
                            "via": "data_path",
                        }
                    ],
                },
            },
            {
                "case": 'mode="events_handled_by_routine"',
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "links": [
                        {
                            "object": "ДокументРасчета",
                            "form": "ФормаДокумента",
                            "config_name": "КонфигурацияЗУП",
                            "source": "Form",
                            "event": "ПриОткрытии",
                            "call_type": "After",
                            "routine_id": "demo-routine-id",
                            "routine": "ПриОткрытии",
                            "routine_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента",
                        }
                    ],
                },
            },
        ],
    },
    "get_event_subscriptions": {
        "summary": "Возвращает подписки на события, их источники и процедуры-обработчики в едином paged-object формате.",
        "returns": [
            {
                "case": "Любой mode",
                "shape": "object{page, subscriptions}",
                "fields": [
                    field("page", "object", "Пагинация текущей страницы."),
                    field("page.limit", "integer", "Запрошенный лимит строк."),
                    field("page.offset", "integer", "Смещение текущей страницы."),
                    field("page.returned", "integer", "Сколько строк возвращено на странице."),
                    field("page.has_more", "boolean", "Есть ли ещё строки за текущей страницей."),
                    field("page.next_offset", "integer", "Offset для следующей страницы.", "Только если has_more=true."),
                    field("subscriptions", "array<object>", "Найденные подписки/источники/обработчики; поля зависят от mode."),
                    field("subscriptions[].subscription", "string", "Имя подписки на событие."),
                    field("subscriptions[].config_name", "string", "Имя конфигурации.", "В режимах list/of_object/sources."),
                    field("subscriptions[].qualified_name", "string", "Полный путь подписки.", "В режимах list/of_object."),
                    field("subscriptions[].event", "string", "Имя события.", "В режимах list/of_object/handlers."),
                    field("subscriptions[].handler", "string", "Имя обработчика из свойства подписки.", 'В режиме list.'),
                    field("subscriptions[].source_object", "string", "Имя объекта-источника события.", 'В режиме of_object.'),
                    field("subscriptions[].source_category", "string", "Категория объекта-источника.", "В режимах of_object/handlers."),
                    field("subscriptions[].source_qn", "string", "Полный путь объекта-источника.", "В режимах of_object/handlers."),
                    field("subscriptions[].subscription_qn", "string", "Полный путь подписки.", "В режимах sources/handlers."),
                    field("subscriptions[].source", "string", "Значение из массива Источник подписки.", 'В режиме sources.'),
                    field("subscriptions[].object", "string", "Имя объекта-источника обработчика.", 'В режиме handlers.'),
                    field("subscriptions[].routine_id", "string", "Идентификатор процедуры-обработчика.", 'В режиме handlers.'),
                    field("subscriptions[].routine", "string", "Имя процедуры-обработчика.", 'В режиме handlers.'),
                    field("subscriptions[].routine_owner_qn", "string", "Полный путь владельца обработчика.", 'В режиме handlers.'),
                    field("subscriptions[].source_config_name", "string", "Конфигурация объекта-источника.", 'В режиме handlers.'),
                    field("subscriptions[].subscription_config_name", "string", "Конфигурация подписки.", 'В режиме handlers.'),
                    field("subscriptions[].routine_config_name", "string", "Конфигурация обработчика.", 'В режиме handlers.'),
                    *adoption_fields("subscriptions[].adoption", "подписка"),
                ],
            }
        ],
    },
    "search_bsl_routines": {
        "summary": "Ищет процедуры и функции BSL по описанию, имени, сигнатуре или режимам списка.",
        "returns": [
            {
                "case": 'mode ∈ {name, signature, unused, exported} (routine_fields-dialect)',
                "shape": "object{context, page, module_contexts, routines, interceptions?, callees?, callers?}",
                "fields": _SEARCH_BSL_COMMON_FIELDS + [
                    field("module_contexts[].file_path", "string", "Путь к файлу модуля внутри выгрузки."),
                    field("routines[].routine_type", "string", "Procedure или Function."),
                    field("routines[].export", "boolean", "Признак Экспорт."),
                    field("routines[].line", "integer", "Номер строки начала routine."),
                ] + _SEARCH_BSL_SIDE_FIELDS,
            },
            {
                "case": 'mode="description" main (description_service-dialect)',
                "shape": "object{context, page, module_contexts, routines, interceptions?, callees?, callers?}",
                "fields": _SEARCH_BSL_COMMON_FIELDS + [
                    field("module_contexts[].owner", "string", "Краткое имя владельца (например, Справочники.Организации)."),
                    field("module_contexts[].form_name", "string", "Имя формы.", "Для модулей форм."),
                    field("routines[].signature", "string", "Сигнатура процедуры/функции."),
                    field("routines[].doc_description", "string", "Описание из документационного комментария."),
                    field("routines[].doc_params_text", "string", "Описание параметров из комментария."),
                    field("routines[].doc_return_text", "string", "Описание возвращаемого значения."),
                    field("routines[].score", "number|null", "Fulltext-скор (округлён).", "null, если поле не участвовало."),
                    field("routines[].similarity", "number|null", "Векторная близость (округлена).", "null, если поле не участвовало."),
                    field("routines[].fulltext_score", "number|null", "Нормализованный fulltext-скор."),
                    field("routines[].vector_score", "number|null", "Нормализованный векторный скор."),
                    field("routines[].hybrid_score", "number|null", "Итоговый гибридный скор."),
                ] + _SEARCH_BSL_SIDE_FIELDS,
            },
            {
                "case": 'mode="description" fallback (RoutineSearchService недоступен, context.dialect="routine_fields", без score/doc-полей)',
                "shape": "object{context, page, module_contexts, routines, interceptions?, callees?, callers?}",
                "fields": _SEARCH_BSL_COMMON_FIELDS + [
                    field("module_contexts[].file_path", "string", "Путь к файлу модуля внутри выгрузки."),
                    field("routines[].routine_type", "string", "Procedure или Function."),
                    field("routines[].export", "boolean", "Признак Экспорт."),
                    field("routines[].line", "integer", "Номер строки начала routine."),
                ] + _SEARCH_BSL_SIDE_FIELDS,
            },
        ],
        "examples": [],
    },
    "get_bsl_routine_body": {
        "summary": "Возвращает карточку процедуры/функции BSL и фрагмент ее тела в paged object-shape.",
        "returns": [{"case": "По id, имени или сигнатуре", "shape": "object{page, routines}", "fields": [
            field("page", "object", "Пагинация совпавших routines."),
            field("page.limit", "integer", "Запрошенный лимит routines (для id всегда 1)."),
            field("page.offset", "integer", "Смещение страницы (для id всегда 0)."),
            field("page.returned", "integer", "Сколько routines реально возвращено."),
            field("page.has_more", "boolean", "Есть ли ещё routines за этой страницей (для id всегда false)."),
            field("page.next_offset", "integer", "Offset следующей страницы routines.", "Только при has_more=true."),
            field("routines", "array<object>", "Совпавшие routines с телом и метаданными."),
            field("routines[].id", "string", "Идентификатор routine."),
            field("routines[].name", "string", "Имя routine."),
            field("routines[].owner", "string", "Краткое имя владельца."),
            field("routines[].module_type", "string", "Тип модуля."),
            field("routines[].form_name", "string", "Имя формы.", "Для модулей форм."),
            field("routines[].owner_qn", "string", "Полный qualified_name владельца модуля."),
            field("routines[].signature", "string", "Сигнатура процедуры/функции."),
            field("routines[].directives", "array<string>", "Директивы компиляции."),
            field("routines[].doc_description", "string", "Описание из комментария к routine.", "Если есть документационный комментарий."),
            field("routines[].doc_params_text", "string", "Описание параметров из комментария.", "Если есть документационный комментарий."),
            field("routines[].doc_return_text", "string", "Описание возвращаемого значения.", "Для функций с документационным комментарием."),
            field("routines[].file_path", "string", "Путь к файлу модуля."),
            field("routines[].line", "integer", "Строка начала routine."),
            field("routines[].body", "string", "Фрагмент тела routine (chunk без служебного маркера обрезки)."),
            field("routines[].body_offset", "integer", "Смещение chunk в символах от начала тела."),
            field("routines[].body_limit", "integer", "Лимит символов на chunk."),
            field("routines[].body_total_chars", "integer", "Полная длина тела routine в символах."),
            field("routines[].body_returned_chars", "integer", "Длина возвращённого chunk в символах."),
            field("routines[].body_truncated", "boolean", "Тело длиннее возвращённого chunk."),
            field("routines[].body_next_offset", "integer", "body_offset для чтения следующего chunk.", "Только при body_truncated=true."),
        ]}],
        "notes": ["body_limit/body_offset читают большое тело routine по частям; при body_truncated=true дочитывать со значения body_next_offset."],
    },
    "get_bsl_modules": {
        "summary": "Возвращает BSL-модули владельца или routines модуля/владельца в группированном виде.",
        "returns": [
            {
                "case": 'mode="modules_of_owner"',
                "shape": "object",
                "fields": [
                    field("owner", "object", "Контекст владельца, общий для всех модулей."),
                    field("owner.config_name", "string", "Имя конфигурации владельца (для Configuration-узлов берётся owner.name)."),
                    field("owner.owner_qn", "string", "Полный qualified_name владельца."),
                    field("modules", "array<object>", "Модули, привязанные к владельцу через HAS_MODULE."),
                    field("modules[].id", "string", "Идентификатор Module-узла (sha1)."),
                    field("modules[].name", "string", "Имя модуля."),
                    field("modules[].module_type", "string", "Тип модуля: ObjectModule, ManagerModule, FormModule и т.п."),
                    field("modules[].path", "string", "Путь к файлу модуля внутри выгрузки."),
                ],
            },
            {
                "case": 'mode="modules_by_owner_name"',
                "shape": "object",
                "fields": [
                    field("owners", "array<object>", "Найденные владельцы по имени."),
                    field("owners[].owner_id", "string", "Локальный идентификатор владельца для связи с modules[] (только в пределах ответа)."),
                    field("owners[].owner_name", "string", "Имя владельца."),
                    field("owners[].config_name", "string", "Имя конфигурации владельца."),
                    field("owners[].owner_qn", "string", "Полный qualified_name владельца."),
                    field("modules", "array<object>", "Модули всех найденных владельцев."),
                    field("modules[].owner_id", "string", "Ссылка на owners[].owner_id."),
                    field("modules[].id", "string", "Идентификатор Module-узла (sha1)."),
                    field("modules[].name", "string", "Имя модуля."),
                    field("modules[].module_type", "string", "Тип модуля."),
                    field("modules[].path", "string", "Путь к файлу модуля."),
                ],
            },
            {
                "case": 'mode="module_routines" (по module_ref как module id, либо owner_ref на общий модуль)',
                "shape": "object",
                "fields": [
                    field("module", "object", "Контекст модуля, общий для всех routines."),
                    field("module.id", "string", "Идентификатор Module-узла.", "Только для Branch A (по module id); отсутствует для CommonModule-owner."),
                    field("module.name", "string", "Имя модуля или общего модуля."),
                    field("module.module_type", "string", "Тип модуля."),
                    field("module.file_path", "string", "Путь к файлу модуля."),
                    field("module.config_name", "string", "Имя конфигурации."),
                    field("module.owner_qn", "string", "qualified_name владельца модуля."),
                    field("routines", "array<object>", "Routines модуля."),
                    field("routines[].id", "string", "Стабильный идентификатор routine."),
                    field("routines[].name", "string", "Имя routine."),
                    field("routines[].routine_type", "string", "Procedure или Function."),
                    field("routines[].export", "boolean", "Признак Экспорт."),
                    field("routines[].directives", "array<string>", "Директивы компиляции BSL."),
                    field("routines[].line", "integer", "Номер строки начала routine."),
                    field("interceptions", "array<object>", "Связи routine с расширениями.", "Только если хотя бы у одной routine есть непустой interception."),
                    field("interceptions[].routine_id", "string", "Routines[].id."),
                    field("interceptions[].role", "string", '"base" или "extension".'),
                    field("interceptions[].base_config_name", "string", "Имя базовой конфигурации.", 'Только при role="extension".'),
                    field("interceptions[].decorator", "string", "Decorator routine расширения.", 'Только при role="extension".'),
                    field("interceptions[].base_routine_name", "string", "Имя routine в базе.", 'Только при role="extension".'),
                    field("interceptions[].extension_config_names", "array<string>", "Имена расширений, перекрывающих routine.", 'Только при role="base".'),
                    field("interceptions[].extension_decorators", "array<string>", "Декораторы routine в расширениях (порядок согласован).", 'Только при role="base".'),
                    field("interceptions[].extension_routine_names", "array<string>", "Имена routine в расширениях (порядок согласован).", 'Только при role="base".'),
                ],
            },
            {
                "case": 'mode="module_routines" (по owner_ref на обычный объект с несколькими модулями)',
                "shape": "object",
                "fields": [
                    field("owner", "object", "Контекст владельца."),
                    field("owner.config_name", "string", "Имя конфигурации владельца."),
                    field("owner.owner_qn", "string", "qualified_name владельца."),
                    field("modules", "array<object>", "Контексты модулей, на которые ссылаются routines[].module_id в текущем ответе."),
                    field("modules[].id", "string", "Идентификатор Module-узла (sha1)."),
                    field("modules[].name", "string", "Имя модуля."),
                    field("modules[].module_type", "string", "Тип модуля."),
                    field("modules[].file_path", "string", "Путь к файлу модуля."),
                    field("routines", "array<object>", "Routines из всех модулей владельца."),
                    field("routines[].module_id", "string", "Ссылка на modules[].id."),
                    field("routines[].id", "string", "Идентификатор routine."),
                    field("routines[].name", "string", "Имя routine."),
                    field("routines[].routine_type", "string", "Procedure или Function."),
                    field("routines[].export", "boolean", "Признак Экспорт."),
                    field("routines[].directives", "array<string>", "Директивы компиляции."),
                    field("routines[].line", "integer", "Номер строки начала routine."),
                    field("interceptions", "array<object>", "Связи routine с расширениями.", "Только если хотя бы у одной routine есть непустой interception."),
                ],
            },
            {
                "case": 'mode="common_module_routines" (один матч по module_ref)',
                "shape": "object",
                "fields": [
                    field("module", "object", "Контекст общего модуля."),
                    field("module.name", "string", "Имя общего модуля."),
                    field("module.module_type", "string", 'Всегда "CommonModule".'),
                    field("module.file_path", "string", "Путь к файлу модуля."),
                    field("module.config_name", "string", "Имя конфигурации."),
                    field("module.owner_qn", "string", "qualified_name общего модуля."),
                    field("routines", "array<object>", "Routines общего модуля."),
                    field("routines[].id", "string", "Идентификатор routine."),
                    field("routines[].name", "string", "Имя routine."),
                    field("routines[].routine_type", "string", "Procedure или Function."),
                    field("routines[].export", "boolean", "Признак Экспорт."),
                    field("routines[].directives", "array<string>", "Директивы компиляции."),
                    field("routines[].line", "integer", "Номер строки начала routine."),
                    field("interceptions", "array<object>", "Связи routine с расширениями.", "Только если хотя бы у одной routine есть непустой interception."),
                ],
            },
            {
                "case": 'mode="common_module_routines" (несколько матчей по module_match)',
                "shape": "object",
                "fields": [
                    field("modules", "array<object>", "Найденные общие модули."),
                    field("modules[].name", "string", "Имя общего модуля."),
                    field("modules[].file_path", "string", "Путь к файлу модуля."),
                    field("modules[].config_name", "string", "Имя конфигурации."),
                    field("modules[].owner_qn", "string", "qualified_name общего модуля."),
                    field("routines", "array<object>", "Routines всех найденных общих модулей."),
                    field("routines[].module_owner_qn", "string", "Ссылка на modules[].owner_qn."),
                    field("routines[].id", "string", "Идентификатор routine."),
                    field("routines[].name", "string", "Имя routine."),
                    field("routines[].routine_type", "string", "Procedure или Function."),
                    field("routines[].export", "boolean", "Признак Экспорт."),
                    field("routines[].directives", "array<string>", "Директивы компиляции."),
                    field("routines[].line", "integer", "Номер строки начала routine."),
                    field("interceptions", "array<object>", "Связи routine с расширениями.", "Только если хотя бы у одной routine есть непустой interception."),
                ],
            },
        ],
        "examples": [
            {
                "case": 'mode="modules_of_owner"',
                "json": {
                    "owner": {
                        "config_name": "ДемоКонфиг",
                        "owner_qn": "ДемоПроект/ДемоКонфиг/Документы/ДокументА",
                    },
                    "modules": [
                        {
                            "id": "demo-module-id",
                            "name": "ObjectModule",
                            "module_type": "ObjectModule",
                            "path": "Documents/ДокументА/Ext/ObjectModule.bsl",
                        }
                    ],
                },
            },
            {
                "case": 'mode="module_routines" (single-module shape)',
                "json": {
                    "module": {
                        "id": "demo-module-id",
                        "name": "ObjectModule",
                        "module_type": "ObjectModule",
                        "file_path": "Documents/ДокументА/Ext/ObjectModule.bsl",
                        "config_name": "ДемоКонфиг",
                        "owner_qn": "ДемоПроект/ДемоКонфиг/Документы/ДокументА",
                    },
                    "routines": [
                        {
                            "id": "demo-routine-id",
                            "name": "ОбработкаПроведения",
                            "routine_type": "Procedure",
                            "export": False,
                            "directives": [],
                            "line": 47,
                        }
                    ],
                },
            },
        ],
        "notes": [
            "directives — массив строк в JSON; в TOON-выводе склеивается через | в одну ячейку.",
            'interceptions[] возвращается только при наличии расширений у хотя бы одной routine. Для role="base" массив extensions разворачивается в три параллельных списка примитивов (extension_config_names, extension_decorators, extension_routine_names) с согласованным порядком индексов.',
            "Если owner_ref указывает на общий модуль (категория ОбщиеМодули), mode='module_routines' возвращает single-module shape (как common_module_routines с одним матчем) — без поля routines[].module_id.",
        ],
    },
    "get_bsl_call_graph": {
        "summary": "Возвращает связи вызовов BSL между процедурами и функциями.",
        "returns": [
            {
                "case": 'mode="callees" или mode="callers"',
                "shape": "object",
                "fields": [
                    field("context", "object", "Параметры запроса: mode, routine_id."),
                    field("page", "object", "Пагинация строк calls[]: limit, offset, returned, has_more, next_offset."),
                    field("page.has_more", "boolean", "Есть ли ещё строки за текущей страницей."),
                    field("page.next_offset", "integer", "Offset следующей страницы.", "Только при has_more."),
                    field("calls", "array<object>", "Прямые вызовы."),
                    field("calls[].callee_id", "string", "Идентификатор вызываемой routine.", 'Для mode="callees".'),
                    field("calls[].callee", "string", "Имя вызываемой routine.", 'Для mode="callees".'),
                    field("calls[].callee_owner_qn", "string", "Владелец вызываемой routine.", 'Для mode="callees".'),
                    field("calls[].caller_id", "string", "Идентификатор вызывающей routine.", 'Для mode="callers".'),
                    field("calls[].caller", "string", "Имя вызывающей routine.", 'Для mode="callers".'),
                    field("calls[].caller_owner_qn", "string", "Владелец вызывающей routine.", 'Для mode="callers".'),
                    field("calls[].kind", "string", "Тип routine."),
                    field("calls[].count", "integer", "Количество найденных вызовов."),
                    field("calls[].lines", "array<integer>", "Строки вызовов."),
                    field("interceptions", "array<object>", "Перехваты routine расширениями.", "Только при наличии расширений."),
                    field("interceptions[].routine_id", "string", "Routine, к которой относится перехват (callee_id/caller_id)."),
                ],
            },
            {
                "case": 'mode="between_owners"',
                "shape": "object",
                "fields": [
                    field("context", "object", "Параметры запроса: mode, from_owner_qn, to_owner_qn."),
                    field("page", "object", "Пагинация строк calls[]: limit, offset, returned, has_more, next_offset."),
                    field("calls", "array<object>", "Вызовы из одного владельца в другой."),
                    field("calls[].caller_id", "string", "Идентификатор вызывающей routine."),
                    field("calls[].caller", "string", "Имя вызывающей routine."),
                    field("calls[].callee_id", "string", "Идентификатор вызываемой routine."),
                    field("calls[].callee", "string", "Имя вызываемой routine."),
                ],
            },
            {
                "case": 'mode="subtree"',
                "shape": "object",
                "fields": [
                    field("context", "object", "Параметры обхода: mode, routine_id, direction, max_depth."),
                    field("page", "object", "Пагинация traversal-путей (unit=\"paths\"), а не строк routines[]/calls[]."),
                    field("page.unit", "string", 'Единица пагинации subtree: "paths".'),
                    field("page.has_more", "boolean", "Есть ли ещё непройденные пути; лишний путь может не добавить новых узлов/рёбер."),
                    field("routines", "array<object>", "Узлы routines в подграфе."),
                    field("routines[].owner_qn", "string", "Владелец routine."),
                    field("routines[].depth", "integer", "Глубина routine относительно исходной (min BFS distance)."),
                    field("calls", "array<object>", "Ребра вызовов."),
                    field("calls[].caller_id", "string", "Идентификатор вызывающей routine."),
                    field("calls[].callee_id", "string", "Идентификатор вызываемой routine."),
                    field("calls[].side", "string", "Сторона связи относительно обхода."),
                ],
            },
        ],
        "examples": [],
    },
    "find_dependency_paths": {
        "summary": "Возвращает paged-набор многошаговых путей зависимостей от выбранного объекта, элемента, формы/события или BSL-рутины.",
        "returns": [{"case": "Поиск путей зависимостей", "shape": "object{page, paths, multi_steps, _hint}", "fields": [
            field("page", "object", "Метаданные пагинации: {limit, offset, returned, has_more, next_offset?}."),
            field("page.next_offset", "integer", "Offset следующей страницы.", "Только при has_more=true."),
            field("paths", "array<object>", "Найденные пути зависимостей (одна строка на путь)."),
            field("paths[].path_id", "integer", "Response-local join key пути (1-based) для связи с multi_steps[]."),
            field("paths[].depth", "integer", "Глубина пути (число dependency-hop; owner-bridge не считается)."),
            field("paths[].step_count", "integer", "Число шагов пути (включая owner-bridge)."),
            field("paths[].start_qn", "string", "Полный путь стартового узла (или Routine.id, если start_label == Routine)."),
            field("paths[].start_label", "string", "Тип стартового узла."),
            field("paths[].end_qn", "string", "Полный путь конечного узла (или Routine.id, если end_label == Routine)."),
            field("paths[].end_label", "string", "Тип конечного узла."),
            field("paths[].end_name", "string", "Имя конечного узла."),
            field("paths[].end_owner_qn", "string", "Полный путь владельца конечного узла (пустая строка, если владельца нет)."),
            field("paths[].relationship_chain", "string", "Типы связей пути (без owner-bridge), объединённые через ' -> '."),
            field("multi_steps", "array<object>", "Пошаговое раскрытие; строки только для путей со step_count > 1."),
            field("multi_steps[].path_id", "integer", "Ссылка на paths[].path_id."),
            field("multi_steps[].step_no", "integer", "Порядковый номер шага (1-based)."),
            field("multi_steps[].from_qn", "string", "Полный путь исходного узла шага (или Routine.id, если from_label == Routine)."),
            field("multi_steps[].from_label", "string", "Тип исходного узла шага."),
            field("multi_steps[].to_qn", "string", "Полный путь целевого узла шага (или Routine.id, если to_label == Routine)."),
            field("multi_steps[].to_label", "string", "Тип целевого узла шага."),
            field("multi_steps[].to_name", "string", "Имя целевого узла шага."),
            field("multi_steps[].relationship_type", "string", "Тип связи шага (или OWNER_BRIDGE)."),
            field("multi_steps[].owner_step", "boolean", "Является ли шаг переходом к владельцу (owner-bridge)."),
            field("_hint", "string", "Пояснение семантики *_qn: qualified_name либо Routine.id, когда парный *_label == 'Routine'."),
        ]}],
        "examples": [
            {
                "case": "Путь от справочника к реквизиту (одношаговый, multi_steps пуст)",
                "json": {
                    "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                    "paths": [
                        {
                            "path_id": 1,
                            "depth": 1,
                            "step_count": 1,
                            "start_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники",
                            "start_label": "MetadataObject",
                            "end_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Сотрудник",
                            "end_label": "Attribute",
                            "end_name": "Сотрудник",
                            "end_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                            "relationship_chain": "USED_IN",
                        }
                    ],
                    "multi_steps": [],
                    "_hint": "Fields *_qn hold qualified_name, or Routine id when *_label is 'Routine'",
                },
            }
        ],
    },
    "inspect_metadata_object": {
        "summary": "Возвращает инвентарную карточку объекта и, при запросе sections, ограниченные списки деталей.",
        "returns": [
            {
                "case": "sections не передан или detail=\"brief\"",
                "shape": "object",
                "fields": [
                    field("object", "string", "Имя объекта метаданных."),
                    field("category", "string", "Категория объекта метаданных."),
                    field("config_name", "string", "Имя конфигурации."),
                    field("qualified_name", "string", "Полный путь объекта."),
                    field("sections", "object", "Краткая статистика по секциям. Если sections не передан, включает все обзорные секции. Если sections передан и detail=\"brief\", включает только запрошенные секции."),
                    field("sections.structure.attributes", "integer", "Количество реквизитов объекта.", "Если section structure включен."),
                    field("sections.structure.tabular_parts", "integer", "Количество табличных частей.", "Если section structure включен."),
                    field("sections.structure.tabular_attributes", "integer", "Количество реквизитов всех табличных частей объекта.", "Если section structure включен."),
                    field("sections.structure.resources", "integer", "Количество ресурсов.", "Если section structure включен и объект поддерживает ресурсы."),
                    field("sections.structure.dimensions", "integer", "Количество измерений.", "Если section structure включен и объект поддерживает измерения."),
                    field("sections.structure.commands", "integer", "Количество команд.", "Если section structure включен."),
                    field("sections.structure.layouts", "integer", "Количество макетов.", "Если section structure включен."),
                    field("sections.structure.enum_values", "integer", "Количество значений перечисления.", "Если section structure включен."),
                    field("sections.forms.count", "integer", "Количество форм.", "Если section forms включен."),
                    field("sections.forms.default_forms", "integer", "Количество форм по умолчанию.", "Если section forms включен."),
                    field("sections.form_events.form_level_events", "integer", "Количество событий форм.", "Если section form_events включен."),
                    field("sections.form_attributes.count", "integer", "Количество реквизитов форм объекта (включая случай ОбщиеФормы, когда реквизиты висят на самой общей форме).", "Если section form_attributes включен."),
                    field("sections.usages.referencing_objects", "integer", "Количество объектов, где используется текущий объект.", "Если section usages включен."),
                    field("sections.usages.referencing_fields", "integer", "Количество полей/путей использования текущего объекта.", "Если section usages включен."),
                    field("sections.usages.register_movement_documents", "integer", "Количество документов, делающих движения по текущему регистру.", "Если section usages включен."),
                    field("sections.dependencies.available", "boolean", "Есть ли зависимости для дальнейшего обхода.", "Если section dependencies включен."),
                    field("sections.dependencies.standard_max_depth", "integer", "Глубина обхода для detail=\"standard\".", "Если section dependencies включен."),
                    field("sections.dependencies.extended_max_depth", "integer", "Глубина обхода для detail=\"extended\".", "Если section dependencies включен."),
                    field("sections.access.roles_with_access", "integer", "Количество ролей с правами на объект.", "Если section access включен."),
                    field("sections.access.roles_with_conditional_rights", "integer", "Количество ролей с условными правами.", "Если section access включен."),
                    field("sections.subscriptions.count", "integer", "Количество подписок на события объекта.", "Если section subscriptions включен."),
                    field("sections.bsl.available", "boolean", "Доступны ли BSL-данные.", "Если section bsl включен."),
                    field("sections.bsl.object_modules", "integer", "Количество модулей объекта.", "Если section bsl включен и LOAD_BSL_SIGNATURES=true."),
                    field("sections.bsl.form_modules", "integer", "Количество модулей форм.", "Если section bsl включен и LOAD_BSL_SIGNATURES=true."),
                    field("sections.bsl.routines", "integer", "Количество процедур/функций.", "Если section bsl включен и LOAD_BSL_SIGNATURES=true."),
                    field("sections.bsl.exported", "integer", "Количество экспортных процедур/функций.", "Если section bsl включен и LOAD_BSL_SIGNATURES=true."),
                    field("sections.bsl.reason", "string", "Причина отсутствия BSL-данных.", "Если section bsl включен и LOAD_BSL_SIGNATURES=false."),
                    field("sections.predefined.count", "integer", "Количество предопределенных значений.", "Если section predefined включен."),
                    field("sections.overview.synonym", "string", "Синоним объекта.", "Если sections содержит overview и синоним не пустой."),
                    field("sections.overview.comment", "string", "Комментарий объекта.", "Если sections содержит overview и комментарий не пустой."),
                    field("next_actions", "array<object>", "Рекомендованные следующие tools по секциям, где есть данные.", "Если для включенных секций найдены данные."),
                    field("next_actions[].section", "string", "Секция, для которой есть следующий tool.", "Если next_actions есть."),
                    field("next_actions[].tool", "string", "Имя tool для детального запроса секции.", "Если next_actions есть."),
                    field("hint", "string", "Подсказка, что sections=None возвращает только inventory card.", "Только если sections не передан."),
                ],
            },
            {
                "case": 'sections содержит "overview", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("overview.name", "string", "Имя объекта."),
                    field("overview.category", "string", "Категория объекта."),
                    field("overview.qualified_name", "string", "Полный путь объекта."),
                    field("overview.config_name", "string", "Имя конфигурации."),
                    field("overview.synonym", "string", "Синоним объекта."),
                    field("overview.comment", "string", "Комментарий объекта."),
                    *adoption_fields("overview.adoption", "объект"),
                    field("overview.properties", "object", "Свойства узла метаданных без body и embedding.", "Только при detail=\"extended\"."),
                    field("overview.properties.<property_name>", "any", "Значение свойства из графа.", "Только при detail=\"extended\" и если свойство не исключено фильтрами summary."),
                ],
            },
            {
                "case": 'sections содержит "structure", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("structure.attributes", "array<object>", "Реквизиты объекта."),
                    field("structure.tabular_parts", "array<object>", "Табличные части объекта."),
                    field("structure.tabular_attributes", "array<object>", "Плоский список реквизитов всех табличных частей объекта (не вложен в tabular_parts)."),
                    field("structure.tabular_attributes[].tabular_part", "string", "Имя табличной части, которой принадлежит реквизит. owner_qn для tabular_attributes — это qualified_name табличной части."),
                    field("structure.resources", "array<object>", "Ресурсы регистра.", "Если у объекта есть ресурсы."),
                    field("structure.dimensions", "array<object>", "Измерения регистра.", "Если у объекта есть измерения."),
                    field("structure.enum_values", "array<object>", "Значения перечисления.", "Если у объекта есть значения перечисления."),
                    field("structure.commands", "array<object>", "Команды объекта.", "Только при detail=\"extended\"."),
                    field("structure.layouts", "array<object>", "Макеты объекта.", "Только при detail=\"extended\"."),
                    field("structure.*[].name", "string", "Имя дочернего элемента."),
                    field("structure.*[].qualified_name", "string", "Полный путь дочернего элемента."),
                    field("structure.*[].config_name", "string", "Имя конфигурации владельца."),
                    field("structure.*[].owner_qn", "string", "Полный путь объекта-владельца."),
                    *adoption_fields("structure.*[].adoption", "дочерний элемент"),
                ],
            },
            {
                "case": 'sections содержит "forms", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("forms", "array<object>", "Формы объекта."),
                    field("forms[].name", "string", "Имя формы."),
                    field("forms[].qualified_name", "string", "Полный путь формы."),
                    field("forms[].config_name", "string", "Имя конфигурации."),
                    field("forms[].owner_qn", "string", "Полный путь объекта-владельца."),
                    field("forms[].role", "string", "Роль формы из связи HAS_FORM."),
                    field("forms[].is_default", "boolean", "Признак формы по умолчанию."),
                    *adoption_fields("forms[].adoption", "форма"),
                ],
            },
            {
                "case": 'sections содержит "form_events", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("form_events", "array<object>", "События форм объекта."),
                    field("form_events[].form_name", "string", "Имя формы."),
                    field("form_events[].event_name", "string", "Имя события формы."),
                    field("form_events[].qualified_name", "string", "Полный путь события формы."),
                ],
            },
            {
                "case": 'sections содержит "form_attributes", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("form_attributes", "array<object>", "Реквизиты форм объекта. Для ОбщиеФормы — реквизиты самой общей формы."),
                    field("form_attributes[].form", "string", "Имя формы. Для обычных объектов — имя Form-узла; для ОбщиеФормы — имя самой общей формы."),
                    field("form_attributes[].name", "string", "Имя реквизита формы."),
                    field("form_attributes[].qualified_name", "string", "Полный путь реквизита формы."),
                    field("form_attributes[].config_name", "string", "Имя конфигурации."),
                    field("form_attributes[].owner_qn", "string", "Для обычных объектов — qualified_name формы; для ОбщиеФормы — qualified_name самой общей формы."),
                    *adoption_fields("form_attributes[].adoption", "реквизит формы"),
                ],
            },
            {
                "case": 'sections содержит "usages", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("usages.objects", "array<object>", "Объекты, где используется текущий объект."),
                    field("usages.objects[].config_name", "string", "Имя конфигурации объекта-использования."),
                    field("usages.objects[].category", "string", "Категория объекта-использования."),
                    field("usages.objects[].name", "string", "Имя объекта-использования."),
                    field("usages.objects[].qualified_name", "string", "Полный путь объекта-использования."),
                    *adoption_fields("usages.objects[].adoption", "объект-использование"),
                    field("usages.paths", "array<object>", "Конкретные пути использования.", "Только при detail=\"extended\"."),
                    field("usages.paths[].target_qn", "string", "Полный путь исходного объекта, для которого ищутся использования.", "Только при detail=\"extended\"."),
                    field("usages.paths[].config_name", "string", "Имя конфигурации места использования.", "Только при detail=\"extended\"."),
                    field("usages.paths[].path", "string", "Человекочитаемый путь поля/формы/измерения/ресурса, где найдено использование.", "Только при detail=\"extended\"."),
                ],
            },
            {
                "case": 'sections содержит "dependencies", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("dependencies.paths", "array<object>", "Таблица путей зависимостей, построенная из обхода графа."),
                    field("dependencies.paths[].depth", "integer", "Глубина пути."),
                    field("dependencies.paths[].start_label", "string", "Тип стартового узла."),
                    field("dependencies.paths[].start_ref", "string", "Ссылка/qualified_name стартового узла."),
                    field("dependencies.paths[].end_label", "string", "Тип конечного узла."),
                    field("dependencies.paths[].end_ref", "string", "Ссылка/qualified_name конечного узла."),
                    field("dependencies.paths[].end_name", "string", "Имя конечного узла."),
                    field("dependencies.paths[].relationship_chain", "array<string>|string", "Цепочка типов связей. В TOON может быть строкой.", "Всегда для dependency paths."),
                    field("dependencies.steps", "array<object>", "Таблица шагов путей зависимостей."),
                    field("dependencies.steps[].path_index", "integer", "Индекс пути, к которому относится шаг."),
                    field("dependencies.steps[].step_index", "integer", "Порядок шага внутри пути."),
                    field("dependencies.steps[].from_label", "string", "Тип исходного узла шага."),
                    field("dependencies.steps[].from_ref", "string", "Ссылка/qualified_name исходного узла шага."),
                    field("dependencies.steps[].relationship_type", "string", "Тип связи шага."),
                    field("dependencies.steps[].to_label", "string", "Тип целевого узла шага."),
                    field("dependencies.steps[].to_ref", "string", "Ссылка/qualified_name целевого узла шага."),
                    field("dependencies.steps[].to_name", "string", "Имя целевого узла шага."),
                    field("dependencies.steps[].owner_step", "boolean", "Является ли шаг переходом к владельцу."),
                ],
            },
            {
                "case": 'sections содержит "access", detail="standard"',
                "shape": "object",
                "fields": [
                    field("access", "array<object>", "Краткий список ролей с правами."),
                    field("access[].role", "string", "Имя роли."),
                    field("access[].role_qn", "string", "Полный путь роли."),
                    field("access[].config_name", "string", "Имя конфигурации роли."),
                    field("access[].rights_count", "integer", "Количество прав роли на объект."),
                    field("access[].has_conditions", "boolean", "Есть ли хотя бы одно условное право."),
                ],
            },
            {
                "case": 'sections содержит "access", detail="extended"',
                "shape": "object",
                "fields": [
                    field("access.roles", "array<object>", "Роли, у которых есть права на объект."),
                    field("access.roles[].role", "string", "Имя роли."),
                    field("access.roles[].role_qn", "string", "Полный путь роли."),
                    field("access.roles[].config_name", "string", "Имя конфигурации роли."),
                    field("access.roles[].rights_count", "integer", "Количество прав роли на объект."),
                    field("access.rights", "array<object>", "Детальные права, развернутые по ролям."),
                    field("access.rights[].role_qn", "string", "Полный путь роли, к которой относится право."),
                    field("access.rights[].right_ru", "string", "Имя права на русском."),
                    field("access.rights[].allowed", "boolean", "Разрешено ли право."),
                    field("access.rights[].has_condition", "boolean", "Есть ли условие права."),
                ],
            },
            {
                "case": 'sections содержит "subscriptions", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("subscriptions", "array<object>", "Подписки на события объекта."),
                    field("subscriptions[].subscription", "string", "Имя подписки."),
                    field("subscriptions[].config_name", "string", "Имя конфигурации подписки."),
                    field("subscriptions[].qualified_name", "string", "Полный путь подписки."),
                    field("subscriptions[].event", "string", "Имя события."),
                    field("subscriptions[].source_object", "string", "Имя объекта-источника."),
                    field("subscriptions[].source_category", "string", "Категория объекта-источника."),
                    field("subscriptions[].source_qn", "string", "Полный путь объекта-источника."),
                ],
            },
            {
                "case": 'sections содержит "bsl", detail="standard"',
                "shape": "object",
                "fields": [
                    field("bsl.available", "boolean", "Доступны ли BSL-данные."),
                    field("bsl.reason", "string", "Причина отсутствия BSL-данных.", "Если LOAD_BSL_SIGNATURES=false."),
                    field("bsl.modules", "array<string>", "Имена модулей объекта и форм.", "Если LOAD_BSL_SIGNATURES=true."),
                    field("bsl.routine_count", "integer", "Количество процедур/функций.", "Если LOAD_BSL_SIGNATURES=true."),
                    field("bsl.exported_count", "integer", "Количество экспортных процедур/функций.", "Если LOAD_BSL_SIGNATURES=true."),
                ],
            },
            {
                "case": 'sections содержит "bsl", detail="extended"',
                "shape": "object",
                "fields": [
                    field("bsl.available", "boolean", "Доступны ли BSL-данные."),
                    field("bsl.reason", "string", "Причина отсутствия BSL-данных.", "Если LOAD_BSL_SIGNATURES=false."),
                    field("bsl.modules", "array<string>", "Имена модулей объекта и форм.", "Если LOAD_BSL_SIGNATURES=true."),
                    field("bsl.routines", "array<object>", "Процедуры/функции объекта и его форм.", "Если LOAD_BSL_SIGNATURES=true."),
                    field("bsl.routines[].name", "string", "Имя процедуры/функции."),
                    field("bsl.routines[].routine_type", "string", "Тип routine."),
                    field("bsl.routines[].export", "boolean", "Признак Экспорт."),
                ],
            },
            {
                "case": 'sections содержит "predefined", detail="standard" или "extended"',
                "shape": "object",
                "fields": [
                    field("predefined", "array<object>", "Предопределенные значения объекта."),
                    field("predefined[].name", "string", "Имя предопределенного значения."),
                    field("predefined[].config_name", "string", "Имя конфигурации."),
                    field("predefined[].owner_qn", "string", "Полный путь объекта-владельца."),
                    field("predefined[].qualified_name", "string", "Полный путь предопределенного значения."),
                    field("predefined[].code", "string", "Код значения."),
                    field("predefined[].description", "string", "Наименование/описание значения."),
                ],
            },
        ],
        "examples": [
            {
                "case": "Базовая карточка",
                "json": {
                    "object": {
                        "name": "ДокументРасчета",
                        "category": "Документы",
                        "config_name": "КонфигурацияЗУП",
                        "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                        "synonym": "Документ расчета",
                        "comment": "",
                    },
                    "has_data": {"structure": True, "forms": True, "form_attributes": True, "usages": True, "dependencies": True, "access": True, "bsl": True},
                    "sections": {
                        "structure": {"attributes": 12, "tabular_parts": 2, "tabular_attributes": 7, "commands": 4},
                        "form_attributes": {"count": 5},
                    },
                },
            }
        ],
        "notes": [
            "detail=extended не означает полный дамп; списки ограничены limit_per_section.",
            "Для ОбщиеФормы form_attributes возвращает реквизиты самой общей формы (узла Form нет); поле form содержит имя ОбщейФормы.",
        ],
    },
    "get_extension_object_diff": {
        "summary": "Возвращает различия объекта расширения относительно базового объекта.",
        "returns": [{"case": "Сравнение объекта расширения", "shape": "object", "fields": [
            field("object.object_ref", "string", "Ссылка на объект из запроса."),
            field("object.object_name", "string", "Каноническое имя объекта."),
            field("object.category", "string", "Категория метаданных объекта."),
            field("extensions", "array<object>", "По строке на каждое проверенное расширение."),
            field("extensions[].extension_id", "string", "Локальный ключ ответа (ext1, ext2, ...); связывает counts/metadata_changes/code_changes. Не id графа."),
            field("extensions[].extension_config_name", "string", "Имя конфигурации-расширения."),
            field("extensions[].base_config_name", "string", "Имя базовой конфигурации."),
            field("extensions[].object_state", "string", "adopted | extension_only | not_found."),
            field("extensions[].extension_qn", "string", "Полный путь объекта в расширении.", "null если объект отсутствует в расширении."),
            field("extensions[].base_qn", "string", "Полный путь базового объекта.", "null если базового объекта нет."),
            field("extensions[].truncated", "boolean", "Списки изменений обрезаны по limit_per_section."),
            field("counts", "array<object>", "Агрегаты по видам элементов; по строке на (extension_id, section, kind)."),
            field("counts[].extension_id", "string", "Ссылка на extensions[].extension_id."),
            field("counts[].section", "string", "structure | forms | form_items | bsl."),
            field("counts[].kind", "string", "Вид элемента: Attribute, TabularPart, Form, FormControl, Module, Routine и т.п."),
            field("counts[].adopted", "integer", "Заимствованные без изменений свойств.", "Для structure/forms/form_items."),
            field("counts[].modified", "integer", "Заимствованные с изменёнными свойствами.", "Для form_items."),
            field("counts[].extension_only", "integer", "Есть только в расширении."),
            field("counts[].base_only", "integer", "Есть только в базе.", "Для structure/forms."),
            field("counts[].unchanged", "integer", "Заимствованные без изменений.", "Для form_items."),
            field("counts[].extends", "integer", "Модули, расширяющие базовый модуль.", "Для section=bsl, kind=Module."),
            field("counts[].intercepts", "integer", "Routines, перехватывающие базовые.", "Для section=bsl, kind=Routine."),
            field("metadata_changes", "array<object>", "Строки изменений метаданных.", "Пусто при detail=brief или sections=None."),
            field("metadata_changes[].change_id", "string", "Локальный ключ ответа (ch1, ch2, ...); связывает property_changes/complex_property_values. Не id графа."),
            field("metadata_changes[].extension_id", "string", "Ссылка на extensions[].extension_id."),
            field("metadata_changes[].section", "string", "structure | forms | form_items."),
            field("metadata_changes[].kind", "string", "Вид элемента: Attribute, Form, FormControl и т.п."),
            field("metadata_changes[].name", "string", "Имя элемента."),
            field("metadata_changes[].change", "string", "base_only | extension_only | adopted | modified | unchanged."),
            field("metadata_changes[].form_name", "string", "Имя формы-владельца.", "Для section=form_items."),
            field("metadata_changes[].extension_qn", "string", "Полный путь элемента в расширении.", "null при change=base_only."),
            field("metadata_changes[].base_qn", "string", "Полный путь элемента в базе.", "null при change=extension_only."),
            field("property_changes", "array<object>", "Скалярные различия свойств строк metadata_changes.", "Заполняется при detail=extended; при detail=standard для form_items — имена свойств с null-значениями."),
            field("property_changes[].change_id", "string", "Ссылка на metadata_changes[].change_id."),
            field("property_changes[].property", "string", "Имя свойства."),
            field("property_changes[].base_value", "string", "Значение в базе.", "null если свойства нет в базе."),
            field("property_changes[].extension_value", "string", "Значение в расширении.", "null если свойства нет в расширении."),
            field("complex_property_values", "array<object>", "Развёрнутые значения свойств-массивов; по строке на элемент массива.", "При detail=extended, если хотя бы одна сторона свойства — массив."),
            field("complex_property_values[].change_id", "string", "Ссылка на metadata_changes[].change_id."),
            field("complex_property_values[].property", "string", "Имя свойства."),
            field("complex_property_values[].side", "string", "base | extension. Отсутствие строк стороны — нет значений на этой стороне."),
            field("complex_property_values[].index", "integer", "Позиция в массиве с 0; скалярная сторона при смешанном сравнении — одна строка с index=0."),
            field("complex_property_values[].value", "string", "Элемент значения."),
            field("code_changes", "array<object>", "Строки изменений BSL-кода (модули и routines).", "Для section bsl, когда доступны данные BSL-индекса."),
            field("code_changes[].change_id", "string", "Локальный ключ ответа; общая нумерация с metadata_changes."),
            field("code_changes[].extension_id", "string", "Ссылка на extensions[].extension_id."),
            field("code_changes[].kind", "string", "Module | Routine."),
            field("code_changes[].name", "string", "Имя модуля или routine."),
            field("code_changes[].module_type", "string", "Тип модуля (ObjectModule, FormModule, ...)."),
            field("code_changes[].change", "string", "extends | intercepts | extension_only."),
            field("code_changes[].owner_qn", "string", "Полный путь владельца."),
            field("code_changes[].extension_node_id", "string", "Id модуля/routine расширения в графе; пригоден для BSL-инструментов."),
            field("code_changes[].base_node_id", "string", "Id базового модуля/routine в графе.", "null при change=extension_only."),
            field("code_changes[].decorator_type", "string", "Тип перехвата (Before, After, Around, ChangeAndValidate).", "При change=intercepts."),
            field("code_changes[].target", "string", "Имя перехватываемой базовой routine.", "При change=intercepts."),
            field("section_names", "object", "Словарь {@sec:N -> имя секции} для counts/metadata_changes.", "Только при compact refs и более чем одной строке с section."),
            field("kind_names", "object", "Словарь {@kind:N -> вид элемента} для counts/metadata_changes/code_changes.", "Только при compact refs и более чем одной строке с kind."),
        ]}],
    },
    "search_bsl_code": {
        "summary": "Ищет процедуры/функции по телу BSL-кода и возвращает совпавшие фрагменты.",
        "returns": [{"case": "Поиск по коду", "shape": "object", "fields": [
            field("items", "array<object>", "Найденные routines."),
            field("items[].routine_id", "string", "Идентификатор routine."),
            field("items[].name", "string", "Имя routine."),
            field("items[].signature", "string", "Сигнатура routine."),
            field("items[].owner_qn", "string", "Полный путь владельца."),
            field("items[].module_type", "string", "Тип модуля."),
            field("items[].file_path", "string", "Путь к файлу модуля."),
            field("items[].line", "integer", "Строка начала routine."),
            field("items[].score", "number", "Оценка релевантности.", "Для режимов с ранжированием."),
            field("items[].fragments", "array<object>", "Фрагменты совпадений.", "Если include_fragments=true."),
            field("items[].fragments[].fragment_id", "string", "Идентификатор фрагмента."),
            field("items[].fragments[].start_line", "integer", "Начальная строка фрагмента."),
            field("items[].fragments[].end_line", "integer", "Конечная строка фрагмента."),
            field("items[].fragments[].code", "string", "Текст фрагмента BSL."),
            field("items[].ranges", "array<object>", "Диапазоны совпавших фрагментов.", "Если include_fragments=false."),
            field("items[].ranges[].fragment_id", "string", "Идентификатор фрагмента."),
            field("items[].ranges[].start_line", "integer", "Начальная строка фрагмента."),
            field("items[].ranges[].end_line", "integer", "Конечная строка фрагмента."),
            field("count", "integer", "Количество найденных items."),
            field("notice", "string", "Техническое предупреждение о режиме поиска.", "Если индекс неполный или используется fallback."),
        ]}],
        "examples": [
            {
                "case": "С фрагментами",
                "json": {
                    "items": [
                        {
                            "routine_id": "demo-routine-id",
                            "name": "ЗаполнитьСтроки",
                            "signature": "Процедура ЗаполнитьСтроки()",
                            "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента",
                            "module_type": "FormModule",
                            "file_path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl",
                            "line": 120,
                            "score": 0.8123,
                            "fragments": [{"fragment_id": "demo-fragment-id", "start_line": 120, "end_line": 140, "code": "Процедура ЗаполнитьСтроки()\n    // ..."}],
                        }
                    ],
                    "count": 1,
                    "notice": "vector_index_partial",
                },
            }
        ],
    },
    "find_objects_by_summary": {
        "summary": "Ищет объекты по сгенерированным summary или возвращает summary конкретного объекта.",
        "returns": [
            {
                "case": "query задан",
                "shape": "object",
                "fields": [
                    field("count", "integer", "Количество найденных результатов."),
                    field("results", "array<object>", "Найденные объекты."),
                    field("results[].category", "string", "Категория объекта."),
                    field("results[].name", "string", "Имя объекта."),
                    field("results[].qualified_name", "string", "Полный путь объекта."),
                    field("results[].config_name", "string", "Имя конфигурации."),
                    field("results[].score", "number", "Оценка релевантности."),
                    field("results[].summary", "object", "Сводка объекта.", "Если include_summary не false."),
                ],
            },
            {
                "case": "object_ref задан",
                "shape": "object",
                "fields": [
                    field("category", "string", "Категория объекта."),
                    field("name", "string", "Имя объекта."),
                    field("qualified_name", "string", "Полный путь объекта."),
                    field("config_name", "string", "Имя конфигурации."),
                    field("summary", "object", "Полная или краткая сводка объекта."),
                    field("summary.core_idea", "string", "Основное назначение объекта."),
                    field("summary.data_composition", "array", "Описание состава данных.", "Если есть в summary."),
                    field("summary.capabilities", "array", "Возможности объекта.", "Если есть в summary."),
                    field("summary.important_links", "array", "Важные связи объекта.", "Если есть в summary."),
                ],
            },
        ],
        "examples": [
            {
                "case": "Поиск по summary",
                "json": {
                    "count": 1,
                    "results": [
                        {
                            "category": "Документы",
                            "name": "ДокументРасчета",
                            "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета",
                            "config_name": "КонфигурацияЗУП",
                            "score": 0.7211,
                            "summary": {"core_idea": "Документ выполняет расчет и хранит строки результата."},
                        }
                    ],
                },
            }
        ],
        "notes": ["include_summary управляет объемом поля summary в результатах поиска."],
    },
    "get_tool_return_schema": {
        "summary": "Возвращает вложенную schema полей ответа указанного MCP tool, скомпилированную из документации.",
        "returns": [
            {
                "case": "Известный tool_name",
                "shape": "object",
                "fields": [
                    field("tool_name", "string", "Имя MCP tool, для которого построена schema."),
                    field("returns", "array<object>", "По одному элементу на каждый режим ответа tool."),
                    field("returns[].case", "string", "Условие режима ответа (какой набор параметров даёт эту форму)."),
                    field("returns[].shape", "string", "Краткая форма верхнего уровня: object, array<object> и т.п."),
                    field("returns[].schema", "object", "Вложенная JSON-like schema полей этого режима."),
                    field("returns[].schema.type", "string", "Тип узла: object, array, string, integer, number, boolean, null, any."),
                    field("returns[].schema.properties", "object", "Свойства object-узла: имя поля -> вложенная schema.", "Для type=object."),
                    field("returns[].schema.items", "object", "Schema элемента массива.", "Для type=array."),
                    field("returns[].schema.oneOf", "array<object>", "Варианты схемы для union-типов или альтернативных форм ответа.", "Для union/альтернативных форм."),
                    field("returns[].schema.additionalProperties", "object", "Schema значений для динамических ключей (<имя>).", "Если узел имеет динамические ключи."),
                    field("returns[].schema.description", "string", "Описание поля.", "Если у поля есть описание."),
                    field("returns[].schema.when", "string", "Условие присутствия поля.", "Если поле присутствует не всегда."),
                    field("returns[].schema_complete", "boolean", "false, если часть полей не удалось скомпилировать (см. schema.unparsed_fields).", "Только при деградации компиляции."),
                ],
            }
        ],
    },
}


TOOL_EXAMPLE_DOCS: Dict[str, List[Dict[str, Any]]] = {
    "get_metadata": [
        {
            "case": 'mode="summary"',
            "json": {
                "page": {"returned": 2, "has_more": False, "truncated": False},
                "configurations": [
                    {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False},
                    {"config_id": "cfg2", "config_name": "РасширениеЗУП", "qualified_name": "Проект_ЗУП/РасширениеЗУП", "is_extension": True},
                ],
                "category_counts": [
                    {"config_id": "cfg1", "category": "Документы", "object_count": 42},
                    {"config_id": "cfg2", "category": "Документы", "object_count": 3},
                ],
            },
        },
        {
            "case": 'mode="categories"',
            "json": {
                "page": {"limit": 100, "offset": 0, "returned": 2, "has_more": False},
                "configurations": [
                    {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False},
                ],
                "category_groups": [
                    {
                        "config_id": "cfg1",
                        "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП",
                        "categories": [
                            {"category": "Документы"},
                            {"category": "Справочники"},
                        ],
                    },
                ],
            },
        },
        {
            "case": 'Максимальный объем: mode="objects", category задан, only_adopted=false',
            "json": {
                "page": {"limit": 100, "offset": 0, "returned": 2, "has_more": False},
                "configurations": [
                    {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False},
                    {"config_id": "cfg2", "config_name": "РасширениеЗУП", "qualified_name": "Проект_ЗУП/РасширениеЗУП", "is_extension": True},
                ],
                "objects": [
                    {"config_id": "cfg1", "category": "Документы", "name": "ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "adoption": {"role": "base", "extension_config_names": ["РасширениеЗУП"]}},
                    {"config_id": "cfg2", "category": "Документы", "name": "ДокументРасчета", "qualified_name": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "adoption": {"role": "extension", "base_config_name": "КонфигурацияЗУП"}},
                ],
            },
        },
    ],
    "find_metadata_objects": [
        {
            "case": 'search_by="description", include_help_text=false',
            "json": {
                "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                "configurations": [
                    {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False},
                ],
                "object_groups": [
                    {
                        "config_id": "cfg1",
                        "category": "Документы",
                        "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                        "objects": [
                            {"name": "ДокументРасчета", "adoption": {"role": "none"}, "synonym": "Синоним", "comment": "", "explanation": "", "score": 0.7821, "similarity": 0.81, "fulltext_score": 0.42, "vector_score": 0.81, "hybrid_score": 0.7821},
                        ],
                    },
                ],
            },
        },
        {
            "case": 'search_by="form_control"',
            "json": {
                "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                "configurations": [
                    {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False},
                ],
                "object_groups": [
                    {
                        "config_id": "cfg1",
                        "category": "Документы",
                        "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                        "objects": [
                            {"name": "ДокументРасчета", "form_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"},
                        ],
                    },
                ],
            },
        },
        {
            "case": 'Максимальный объем: search_by="description", include_help_text=true, расширения, has_more',
            "json": {
                "page": {"limit": 1, "offset": 0, "returned": 1, "has_more": True, "next_offset": 1},
                "configurations": [
                    {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False},
                    {"config_id": "cfg2", "config_name": "РасширениеЗУП", "qualified_name": "Проект_ЗУП/РасширениеЗУП", "is_extension": True},
                ],
                "object_groups": [
                    {
                        "config_id": "cfg1",
                        "category": "Документы",
                        "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                        "objects": [
                            {"name": "ДокументРасчета", "adoption": {"role": "base", "extension_config_names": ["РасширениеЗУП"]}, "synonym": "Синоним", "comment": "Комментарий", "explanation": "Назначение", "score": 0.91, "similarity": 0.88, "fulltext_score": 0.55, "vector_score": 0.88, "hybrid_score": 0.91, "help_text": "Полный текст справки объекта."},
                        ],
                    },
                ],
            },
        },
    ],
    "get_metadata_object_structure": [
        {
            "case": 'sections=["overview"]',
            "json": [{"config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "object": "ДокументРасчета", "attributes": ["Организация", "Дата"], "resources": [], "dimensions": [], "tabularParts": [{"name": "Строки", "attributes": ["Сотрудник", "Сумма"]}]}],
        },
        {
            "case": 'sections=["forms","commands"]',
            "json": {"forms": [{"name": "ФормаДокумента", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "role": "object", "is_default": True}], "commands": [{"name": "Провести", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Command/Провести", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"}]},
        },
        {
            "case": "Максимальный объем: все sections, включая URL, формы, команды, предопределенные",
            "json": {"overview": [{"config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/HTTPСервисы/СервисИнтеграции", "object": "СервисИнтеграции", "attributes": [], "resources": [], "dimensions": [], "tabularParts": [], "adoption": {"role": "none"}}], "attributes": [{"name": "Организация", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"}], "tabular_parts": [{"name": "Строки", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"}], "forms": [{"name": "ФормаДокумента", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "role": "object", "is_default": True}], "url_templates": [{"name": "ШаблонЗапроса", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/HTTPСервисы/СервисИнтеграции/UrlTemplate/ШаблонЗапроса", "pattern": "/api/calc"}], "url_methods": [{"name": "POST", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/HTTPСервисы/СервисИнтеграции/UrlTemplate/ШаблонЗапроса/Method/POST", "httpMethod": "POST", "handler": "ОбработатьЗапрос"}]},
        },
    ],
    "find_metadata_elements": [
        {
            "case": 'element_type="attribute"',
            "json": {
                "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                "elements": [
                    {"config_name": "КонфигурацияЗУП", "category": "Документы", "object": "ДокументРасчета", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "name": "Организация", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация"},
                ],
            },
        },
        {
            "case": 'element_type="form_attribute", form_role="object"',
            "json": {
                "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
                "elements": [
                    {"config_name": "КонфигурацияЗУП", "object": "ДокументРасчета", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "object_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "name": "Объект", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Attribute/Объект"},
                ],
            },
        },
        {
            "case": "Максимальный объем: широкий поиск element_type с пагинацией",
            "json": {
                "page": {"limit": 2, "offset": 0, "returned": 2, "has_more": True, "next_offset": 2},
                "elements": [
                    {"config_name": "КонфигурацияЗУП", "category": "Документы", "object": "ДокументРасчета", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "name": "Организация", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация", "adoption": {"role": "none"}},
                    {"config_name": "РасширениеЗУП", "category": "Документы", "object": "ДокументРасчета", "owner_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "name": "КомментарийРасширения", "qualified_name": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета/Attribute/КомментарийРасширения", "adoption": {"role": "extension", "base_config_name": "КонфигурацияЗУП"}},
                ],
            },
        },
    ],
    "find_metadata_usages": [
        {"case": 'mode="objects"', "json": {
            "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
            "configurations": [
                {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False}
            ],
            "object_groups": [
                {"config_id": "cfg1", "category": "Документы", "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                 "objects": [{"name": "ДокументРасчета"}]}
            ],
        }},
        {"case": 'mode="paths"', "json": {
            "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False},
            "paths": [
                {"target_config_name": "КонфигурацияЗУП", "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники",
                 "config_name": "КонфигурацияЗУП", "path": "Документы.ДокументРасчета.ТабличныеЧасти.Строки.Реквизиты.Сотрудник"}
            ],
        }},
        {"case": 'Максимальный объем: mode="register_movements" или paths с максимальным limit', "json": {
            "page": {"limit": 100, "offset": 0, "returned": 2, "has_more": True, "next_offset": 100},
            "configurations": [
                {"config_id": "cfg1", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП", "is_extension": False}
            ],
            "object_groups": [
                {"config_id": "cfg1", "category": "Документы", "qualified_name_prefix": "Проект_ЗУП/КонфигурацияЗУП/Документы",
                 "objects": [{"name": "ДокументРасчета", "adoption": {"role": "none"}}, {"name": "ДокументКорректировки"}]}
            ],
        }},
    ],
    "get_metadata_element_type": [
        {
            "case": 'element_type="attribute" (одиночное значение, legacy-style)',
            "json": {
                "overview": {"object": "ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"},
                "attribute": [
                    {"name": "Организация", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "type": "СправочникСсылка.Организации"},
                ],
            },
        },
        {
            "case": "element_type не задан (дефолт: все категории кроме form_attribute)",
            "json": {
                "overview": {"object": "ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"},
                "attribute": [
                    {"name": "Организация", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "type": "СправочникСсылка.Организации"},
                    {"name": "Комментарий", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Комментарий", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "type": "Строка(0,Переменная)"},
                ],
                "tabular_attributes": {
                    "Строки": [
                        {"name": "Сотрудник", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки/Attribute/Сотрудник", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки", "config_name": "КонфигурацияЗУП", "type": "СправочникСсылка.Сотрудники"},
                        {"name": "Сумма", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки/Attribute/Сумма", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки", "config_name": "КонфигурацияЗУП", "type": "Число(15,2)"},
                    ],
                },
            },
        },
        {
            "case": "Максимальный объем: широкий вызов по нескольким категориям (attribute с составным типом, tabular_attributes, resource, dimension) + adoption",
            "json": {
                "overview": {"object": "РегистрСведенийПример", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример"},
                "attribute": [
                    {"name": "Получатель", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример/Attribute/Получатель", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример", "config_name": "КонфигурацияЗУП", "type": "СправочникСсылка.Контрагенты|СправочникСсылка.ФизическиеЛица", "adoption": {"role": "none"}},
                    {"name": "КомментарийРасширения", "qualified_name": "Проект_ЗУП/РасширениеЗУП/РегистрыСведений/РегистрСведенийПример/Attribute/КомментарийРасширения", "owner_qn": "Проект_ЗУП/РасширениеЗУП/РегистрыСведений/РегистрСведенийПример", "config_name": "РасширениеЗУП", "type": "Строка(150,Переменная)", "adoption": {"role": "extension", "base_config_name": "КонфигурацияЗУП"}},
                ],
                "tabular_attributes": {
                    "Строки": [
                        {"name": "Сотрудник", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример/TabularPart/Строки/Attribute/Сотрудник", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример/TabularPart/Строки", "config_name": "КонфигурацияЗУП", "type": "СправочникСсылка.Сотрудники"},
                    ],
                },
                "resource": [
                    {"name": "Сумма", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример/Resource/Сумма", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример", "config_name": "КонфигурацияЗУП", "type": "Число(15,2)"},
                ],
                "dimension": [
                    {"name": "Период", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример/Dimension/Период", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/РегистрыСведений/РегистрСведенийПример", "config_name": "КонфигурацияЗУП", "type": "Дата"},
                ],
            },
        },
    ],
    "find_predefined_values": [
        {"case": "По имени значения", "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "object_groups": [{"config_name": "КонфигурацияЗУП", "category": "Справочники", "object": "ВидыЗанятости", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/ВидыЗанятости", "predefined": [{"name": "ОсновноеМестоРаботы", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Справочники/ВидыЗанятости/Predefined/ОсновноеМестоРаботы", "code": "000000001", "description": "Основное место работы"}]}]}},
        {"case": "По owner_object: несколько значений одной группы", "json": {"page": {"limit": 100, "offset": 0, "returned": 2, "has_more": False}, "object_groups": [{"config_name": "КонфигурацияЗУП", "category": "Перечисления", "object": "СтатусыДокументов", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Перечисления/СтатусыДокументов", "predefined": [{"name": "Черновик", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Перечисления/СтатусыДокументов/EnumValue/Черновик"}, {"name": "Проведен", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Перечисления/СтатусыДокументов/EnumValue/Проведен"}]}]}},
        {"case": "Максимальный объем: поиск по всем объектам с расширениями", "json": {"page": {"limit": 100, "offset": 0, "returned": 2, "has_more": True, "next_offset": 100}, "object_groups": [{"config_name": "КонфигурацияЗУП", "category": "Справочники", "object": "ВидыЗанятости", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/ВидыЗанятости", "predefined": [{"name": "ОсновноеМестоРаботы", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Справочники/ВидыЗанятости/Predefined/ОсновноеМестоРаботы", "adoption": {"role": "base", "extension_config_names": ["РасширениеЗУП"]}}]}, {"config_name": "РасширениеЗУП", "category": "Справочники", "object": "ВидыЗанятости", "owner_qn": "Проект_ЗУП/РасширениеЗУП/Справочники/ВидыЗанятости", "predefined": [{"name": "ДополнительныйВид", "qualified_name": "Проект_ЗУП/РасширениеЗУП/Справочники/ВидыЗанятости/Predefined/ДополнительныйВид", "adoption": {"role": "extension", "base_config_name": "КонфигурацияЗУП"}}]}]}},
    ],
    "get_access_rights": [
        {"case": 'mode="roles_for_target"', "json": [{"role": "РольКадровика", "role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика", "config_name": "КонфигурацияЗУП", "rights": [{"right_ru": "Чтение", "allowed": True, "has_condition": False}]}]},
        {"case": 'mode="role_rights_to_target", include_conditions=true', "json": [{"role": "РольКадровика", "role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика", "config_name": "КонфигурацияЗУП", "target_label": "MetadataObject", "target_name": "Сотрудники", "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники", "rights": [{"right_ru": "Чтение", "allowed": True, "has_condition": False, "condition": ""}, {"right_ru": "Изменение", "allowed": True, "has_condition": True, "condition": "Сотрудник.Организация = &ТекущаяОрганизация"}]}]},
        {"case": "Максимальный объем: targets_of_role с правами по нескольким объектам", "json": [{"role": "РольКадровика", "role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика", "config_name": "КонфигурацияЗУП", "target_label": "MetadataObject", "target_name": "ДокументРасчета", "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "rights": [{"right_ru": "Чтение", "allowed": True, "has_condition": False}, {"right_ru": "Изменение", "allowed": True, "has_condition": True}]}]},
    ],
    "get_metadata_details": [
        {"case": 'mode="resolve"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "nodes": [{"kind": "MetadataObject", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "name": "ДокументРасчета", "config_name": "КонфигурацияЗУП"}]}},
        {"case": 'mode="properties", ref_type="object"', "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": False}, "nodes": [{"kind": "MetadataObject", "node_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "name": "ДокументРасчета", "config_name": "КонфигурацияЗУП", "category": "Документы", "property_count": 1, "help_available": False, "adoption": {"role": "none"}}], "properties": [{"node_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "property": "Синоним", "value": "Документ расчета"}]}},
        {"case": "Максимальный объем: properties с массивом, расширением и include_help", "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": False}, "nodes": [{"kind": "MetadataObject", "node_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "qualified_name": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "name": "ДокументРасчета", "config_name": "РасширениеЗУП", "category": "Документы", "property_count": 3, "help_available": True, "adoption": {"role": "extension", "base_config_name": "КонфигурацияЗУП"}}], "properties": [{"node_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "property": "Синоним", "value": "Расчетный документ"}, {"node_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "property": "Комментарий", "value": "Синтетический пример"}, {"node_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "property": "ИспользуемыеСтандартныеРеквизиты", "value": "Код|Наименование"}], "help": [{"node_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "text": "Документ расчёта зарплаты."}]}},
    ],
    "get_form_structure": [
        {"case": 'sections=["controls"], form_name задан', "json": {"context": {"object": "ДокументРасчета", "category": "Документы", "object_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "form_name": "ФормаДокумента", "form_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}, "pages": {"controls": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}}, "controls": [{"name": "ГруппаОсновная", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ГруппаОсновная", "type": "ГруппаФормы", "id": "GroupMain", "parent": "", "parent_id": ""}]}},
        {"case": 'sections=["events"], form_event_source="all"', "json": {"context": {"object": "ДокументРасчета", "category": "Документы", "object_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "form_name": "ФормаДокумента", "form_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}, "pages": {"form_events": {"limit": 100, "offset": 0, "returned": 2, "has_more": False}}, "form_events": [{"event_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Event/ПриОткрытии", "event": "ПриОткрытии", "source": "form", "source_qn": ""}, {"event_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ПолеСотрудник/Event/ПриИзменении", "event": "ПриИзменении", "source": "ПолеСотрудник", "source_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ПолеСотрудник"}], "event_actions": [{"event_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Event/ПриОткрытии", "call_type": "After", "handler_name": "ПриОткрытии"}]}},
        {"case": "Максимальный объем: bindings без form_name (несколько форм) + forms", "json": {"context": {"object": "ДокументРасчета", "category": "Документы", "object_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "forms_scope": "all"}, "pages": {"bindings": {"limit": 100, "offset": 0, "returned": 2, "has_more": False}}, "forms": [{"form_id": "form1", "name": "ФормаДокумента", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}, {"form_id": "form2", "name": "ФормаСписка", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаСписка"}], "bindings": [{"control": "ПолеСотрудник", "control_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ПолеСотрудник", "target_type": "attribute", "target_name": "Сотрудник", "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки/Attribute/Сотрудник", "via": "data_path", "form_id": "form1"}, {"control": "Список", "control_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаСписка/Control/Список", "target_type": "metadata_object", "target_name": "ДокументРасчета", "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "via": "list", "form_id": "form2"}]}},
    ],
    "find_form_links": [
        {"case": 'mode="controls_bound_to"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "links": [{"object": "ДокументРасчета", "form": "ФормаДокумента", "config_name": "КонфигурацияЗУП", "control": "ПолеСотрудник", "control_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/Control/ПолеСотрудник", "target_label": "Attribute", "target_name": "Сотрудник", "target_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Сотрудник", "via": "data_path"}]}},
        {"case": 'mode="events_handled_by_routine"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "links": [{"object": "ДокументРасчета", "form": "ФормаДокумента", "config_name": "КонфигурацияЗУП", "source": "Form", "event": "ПриОткрытии", "call_type": "After", "routine_id": "demo-routine-id", "routine": "ПриОткрытии", "routine_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}]}},
        {"case": "Максимальный объем: неполная страница с событием элемента", "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": True, "next_offset": 1}, "links": [{"object": "ДокументРасчета", "form": "ФормаДокумента", "config_name": "КонфигурацияЗУП", "source": "ПолеСотрудник", "event": "ПриИзменении", "call_type": "After", "routine_id": "demo-routine-id", "routine": "ПолеСотрудникПриИзменении", "routine_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}]}},
    ],
    "get_event_subscriptions": [
        {"case": 'mode="list"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "subscriptions": [{"subscription": "ПодпискаНаЗапись", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/ПодпискиНаСобытия/ПодпискаНаЗапись", "event": "ПередЗаписью", "handler": "ОбработчикПередЗаписью"}]}},
        {"case": 'mode="of_object"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "subscriptions": [{"subscription": "ПодпискаНаЗапись", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/ПодпискиНаСобытия/ПодпискаНаЗапись", "event": "ПередЗаписью", "source_object": "Сотрудники", "source_category": "Справочники", "source_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники"}]}},
        {"case": "Максимальный объем: mode=handlers c обработчиком и adoption", "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": True, "next_offset": 100}, "subscriptions": [{"object": "Сотрудники", "source_category": "Справочники", "source_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники", "subscription": "ПодпискаНаЗапись", "subscription_qn": "Проект_ЗУП/КонфигурацияЗУП/ПодпискиНаСобытия/ПодпискаНаЗапись", "event": "ПередЗаписью", "routine_id": "demo-routine-id", "routine": "ОбработатьЗаписьСотрудника", "routine_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/ОбработчикиПодписок", "source_config_name": "КонфигурацияЗУП", "subscription_config_name": "КонфигурацияЗУП", "routine_config_name": "КонфигурацияЗУП", "adoption": {"role": "base", "extension_config_names": ["РасширениеЗУП"]}}]}},
    ],
    "search_bsl_routines": [
        {"case": 'mode="name" (routine_fields-dialect)', "json": {"context": {"mode": "name", "dialect": "routine_fields"}, "page": {"limit": 5, "offset": 0, "returned": 1, "has_more": False}, "module_contexts": [{"module_key": "module1", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "owner_category": "Документы", "module_type": "FormModule", "file_path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl"}], "routines": [{"module_key": "module1", "id": "demo-routine-id", "name": "ЗаполнитьСтроки", "routine_type": "Procedure", "export": False, "directives": ["&НаСервере"], "line": 120}]}},
        {"case": 'mode="description" main (description_service-dialect)', "json": {"context": {"mode": "description", "dialect": "description_service"}, "page": {"limit": 3, "offset": 0, "returned": 1, "has_more": True, "next_offset": 1}, "module_contexts": [{"module_key": "module1", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "owner": "Документы.ДокументРасчета", "owner_category": "Документы", "form_name": "ФормаДокумента", "module_type": "FormModule"}], "routines": [{"module_key": "module1", "id": "demo-routine-id", "name": "ЗаполнитьСтроки", "directives": ["&НаСервере"], "signature": "Процедура ЗаполнитьСтроки()", "doc_description": "Заполняет строки документа.", "doc_params_text": "", "doc_return_text": "", "score": None, "similarity": 0.8571, "fulltext_score": None, "vector_score": 0.8571, "hybrid_score": 0.6}]}},
        {"case": "Максимальный объем: mode=name + call_context_mode=both + extensions", "json": {"context": {"mode": "name", "dialect": "routine_fields"}, "page": {"limit": 5, "offset": 0, "returned": 1, "has_more": False}, "module_contexts": [{"module_key": "module1", "config_name": "РасширениеЗУП", "owner_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "owner_category": "Документы", "module_type": "FormModule", "file_path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl"}], "routines": [{"module_key": "module1", "id": "demo-routine-id", "name": "ЗаполнитьСтроки", "routine_type": "Procedure", "export": True, "directives": ["&НаСервере"], "line": 120}], "interceptions": [{"routine_id": "demo-routine-id", "role": "extension", "base_config_name": "КонфигурацияЗУП", "decorator": "After", "base_routine_name": "ЗаполнитьСтроки"}], "callees": [{"routine_id": "demo-routine-id", "callee_id": "demo-callee-id", "callee": "РассчитатьСтроки", "callee_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов"}], "callers": [{"routine_id": "demo-routine-id", "caller_id": "demo-caller-id", "caller": "ПриОткрытии", "caller_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}]}},
    ],
    "get_bsl_routine_body": [
        {"case": "По routine_id", "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": False}, "routines": [{"id": "demo-routine-id", "name": "ЗаполнитьСтроки", "owner": "Документы.ДокументРасчета", "module_type": "FormModule", "form_name": "ФормаДокумента", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "signature": "Процедура ЗаполнитьСтроки()", "directives": ["&НаСервере"], "file_path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl", "line": 120, "body": "Процедура ЗаполнитьСтроки()\nКонецПроцедуры", "body_offset": 0, "body_limit": 10000, "body_total_chars": 42, "body_returned_chars": 42, "body_truncated": False}]}},
        {"case": "По имени и owner (routine_owner_ref)", "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": False}, "routines": [{"id": "demo-routine-id", "name": "РассчитатьСтроки", "owner": "ОбщиеМодули.РасчетДокументов", "module_type": "CommonModule", "form_name": "", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "signature": "Функция РассчитатьСтроки(Данные) Экспорт", "directives": ["&НаСервере"], "doc_return_text": "Таблица рассчитанных строк.", "file_path": "CommonModules/РасчетДокументов/Module.bsl", "line": 42, "body": "Функция РассчитатьСтроки(Данные) Экспорт\nКонецФункции", "body_offset": 0, "body_limit": 10000, "body_total_chars": 58, "body_returned_chars": 58, "body_truncated": False}]}},
        {"case": "Максимальный объем: обрезанное тело + doc comments + has_more", "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": True, "next_offset": 1}, "routines": [{"id": "demo-routine-id", "name": "РассчитатьСтроки", "owner": "ОбщиеМодули.РасчетДокументов", "module_type": "CommonModule", "form_name": "", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "signature": "Функция РассчитатьСтроки(Данные) Экспорт", "directives": ["&НаСервере"], "doc_description": "Рассчитывает строки документа.", "doc_params_text": "Данные - Структура.", "doc_return_text": "ТаблицаЗначений.", "file_path": "CommonModules/РасчетДокументов/Module.bsl", "line": 42, "body": "Функция РассчитатьСтроки(Данные) Экспорт\n    // первый синтетический фрагмент тела", "body_offset": 0, "body_limit": 10000, "body_total_chars": 38200, "body_returned_chars": 10000, "body_truncated": True, "body_next_offset": 10000}]}},
    ],
    "get_bsl_modules": [
        {"case": 'mode="modules_of_owner"', "json": {"owner": {"config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"}, "modules": [{"id": "demo-module-id-1", "name": "ObjectModule", "module_type": "ObjectModule", "path": "Documents/ДокументРасчета/Ext/ObjectModule.bsl"}, {"id": "demo-module-id-2", "name": "ФормаДокумента", "module_type": "FormModule", "path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl"}]}},
        {"case": 'mode="module_routines" (по module_id)', "json": {"module": {"id": "demo-module-id-1", "name": "ObjectModule", "module_type": "ObjectModule", "file_path": "Documents/ДокументРасчета/Ext/ObjectModule.bsl", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"}, "routines": [{"id": "demo-routine-id", "name": "ПередЗаписью", "routine_type": "Procedure", "export": False, "directives": ["&НаСервере"], "line": 42}]}},
        {"case": "Максимальный объем: common_module_routines с несколькими матчами + interceptions для role=base", "json": {"modules": [{"name": "РасчетДокументов", "file_path": "CommonModules/РасчетДокументов/Module.bsl", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов"}, {"name": "РасчетДокументовКлиент", "file_path": "CommonModules/РасчетДокументовКлиент/Module.bsl", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументовКлиент"}], "routines": [{"module_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "id": "demo-routine-id-1", "name": "РассчитатьСтроки", "routine_type": "Function", "export": True, "directives": ["&НаСервере"], "line": 55}, {"module_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументовКлиент", "id": "demo-routine-id-2", "name": "РассчитатьСтрокиКлиент", "routine_type": "Procedure", "export": True, "directives": ["&НаКлиенте"], "line": 18}], "interceptions": [{"routine_id": "demo-routine-id-1", "role": "base", "extension_config_names": ["РасширениеЗУП"], "extension_decorators": ["Перед"], "extension_routine_names": ["РассчитатьСтрокиПеред"]}]}},
    ],
    "get_bsl_call_graph": [
        {"case": 'mode="callees"', "json": {"context": {"mode": "callees", "routine_id": "demo-routine-id"}, "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "calls": [{"callee_id": "demo-callee-id", "callee": "РассчитатьСтроки", "callee_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "config_name": "КонфигурацияЗУП", "kind": "Procedure", "count": 2, "lines": [130, 188]}]}},
        {"case": 'mode="callers" с перехватом расширением', "json": {"context": {"mode": "callers", "routine_id": "demo-routine-id"}, "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "calls": [{"caller_id": "demo-caller-id", "caller": "ПриОткрытии", "caller_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "config_name": "КонфигурацияЗУП", "kind": "Procedure", "count": 1, "lines": [25]}], "interceptions": [{"routine_id": "demo-caller-id", "role": "extension", "base_config_name": "КонфигурацияЗУП", "decorator": "Before", "base_routine_name": "ПриОткрытии"}]}},
        {"case": 'mode="between_owners"', "json": {"context": {"mode": "between_owners", "from_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "to_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов"}, "page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "calls": [{"caller_id": "demo-caller-id", "caller": "ЗаполнитьСтроки", "callee_id": "demo-callee-id", "callee": "РассчитатьСтроки", "config_name": "КонфигурацияЗУП"}]}},
        {"case": "Максимальный объем: mode=subtree, max_depth максимальный", "json": {"context": {"mode": "subtree", "routine_id": "demo-routine-id", "direction": "out", "max_depth": 3}, "page": {"unit": "paths", "limit": 100, "offset": 0, "returned": 1, "has_more": False}, "routines": [{"id": "demo-routine-id", "name": "ЗаполнитьСтроки", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "routine_type": "Procedure", "directives": [], "area_path": "", "depth": 0}, {"id": "demo-callee-id", "name": "РассчитатьСтроки", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "routine_type": "Function", "directives": [], "area_path": "", "depth": 1}], "calls": [{"caller_id": "demo-routine-id", "callee_id": "demo-callee-id", "side": "out"}]}},
    ],
    "find_dependency_paths": [
        {"case": 'direction="downstream"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "paths": [{"path_id": 1, "depth": 1, "step_count": 1, "start_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники", "start_label": "MetadataObject", "end_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Сотрудник", "end_label": "Attribute", "end_name": "Сотрудник", "end_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "relationship_chain": "USED_IN"}], "multi_steps": [], "_hint": "Fields *_qn hold qualified_name, or Routine id when *_label is 'Routine'"}},
        {"case": 'direction="upstream"', "json": {"page": {"limit": 100, "offset": 0, "returned": 1, "has_more": False}, "paths": [{"path_id": 1, "depth": 1, "step_count": 1, "start_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "start_label": "MetadataObject", "end_qn": "Проект_ЗУП/КонфигурацияЗУП/Справочники/Сотрудники", "end_label": "MetadataObject", "end_name": "Сотрудники", "end_owner_qn": "", "relationship_chain": "USED_IN"}], "multi_steps": [], "_hint": "Fields *_qn hold qualified_name, or Routine id when *_label is 'Routine'"}},
        {"case": "Максимальный объем: богатый путь (Command→Routine→Routine), пагинация limit=1, has_more", "json": {"page": {"limit": 1, "offset": 0, "returned": 1, "has_more": True, "next_offset": 1}, "paths": [{"path_id": 1, "depth": 2, "step_count": 2, "start_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Command/Провести", "start_label": "Command", "end_qn": "demo-routine-id", "end_label": "Routine", "end_name": "РассчитатьСтроки", "end_owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "relationship_chain": "HAS_HANDLER -> CALLS"}], "multi_steps": [{"path_id": 1, "step_no": 1, "from_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Command/Провести", "from_label": "Command", "to_qn": "demo-handler-id", "to_label": "Routine", "to_name": "ОбработкаПроведения", "relationship_type": "HAS_HANDLER", "owner_step": False}, {"path_id": 1, "step_no": 2, "from_qn": "demo-handler-id", "from_label": "Routine", "to_qn": "demo-routine-id", "to_label": "Routine", "to_name": "РассчитатьСтроки", "relationship_type": "CALLS", "owner_step": False}], "_hint": "Fields *_qn hold qualified_name, or Routine id when *_label is 'Routine'"}},
    ],
    "inspect_metadata_object": [
        {"case": "sections не передан", "json": {"object": "ДокументРасчета", "category": "Документы", "config_name": "КонфигурацияЗУП", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "sections": {"structure": {"attributes": 12, "tabular_parts": 2, "tabular_attributes": 7, "resources": 0, "dimensions": 0, "commands": 4, "layouts": 1, "enum_values": 0}, "forms": {"count": 3, "default_forms": 2}, "form_attributes": {"count": 5}}, "next_actions": [{"section": "structure", "tool": "get_metadata_object_structure"}, {"section": "form_attributes", "tool": "get_form_structure"}], "hint": "sections=None returns this inventory card only (counts/flags). Pass sections=[...] with detail='standard' or 'extended' for lists."}},
        {"case": 'sections=["access"], detail="standard"', "json": {"access": [{"role": "РольКадровика", "role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика", "config_name": "КонфигурацияЗУП", "rights_count": 8, "has_conditions": True}]}},
        {"case": "Максимальный объем: все sections, detail=extended, limit_per_section максимальный", "json": {"overview": {"name": "ДокументРасчета", "category": "Документы", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "synonym": "Документ расчета", "comment": "", "properties": {"Проведение": "Разрешить"}}, "structure": {"attributes": [{"name": "Организация", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Организация", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета"}], "tabular_attributes": [{"tabular_part": "Строки", "name": "Сотрудник", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки/Attribute/Сотрудник", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/TabularPart/Строки"}], "commands": [{"name": "Провести", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Command/Провести"}]}, "forms": [{"name": "ФормаДокумента", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "role": "object", "is_default": True}], "form_attributes": [{"form": "ФормаДокумента", "name": "Объект", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента/FormAttribute/Объект", "config_name": "КонфигурацияЗУП", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента"}], "usages": {"objects": [{"config_name": "КонфигурацияЗУП", "category": "Отчеты", "name": "ОтчетРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Отчеты/ОтчетРасчета"}], "paths": [{"target_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "path": "Отчеты.ОтчетРасчета.Реквизиты.Документ"}]}, "access": {"roles": [{"role": "РольКадровика", "role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика", "rights_count": 8}], "rights": [{"role_qn": "Проект_ЗУП/КонфигурацияЗУП/Роли/РольКадровика", "right_ru": "Чтение", "allowed": True, "has_condition": False}]}, "bsl": {"available": True, "modules": ["МодульОбъекта", "ФормаДокумента"], "routines": [{"name": "ПередЗаписью", "routine_type": "Procedure", "export": False}]}}},
    ],
    "get_extension_object_diff": [
        {"case": "sections не передан — только counts", "json": {"object": {"object_ref": "Документы.ДокументРасчета", "object_name": "ДокументРасчета", "category": "Документы"}, "extensions": [{"extension_id": "ext1", "extension_config_name": "РасширениеЗУП", "base_config_name": "КонфигурацияЗУП", "object_state": "adopted", "extension_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "base_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "truncated": False}], "counts": [{"extension_id": "ext1", "section": "structure", "kind": "Attribute", "adopted": 2, "extension_only": 1, "base_only": 0}, {"extension_id": "ext1", "section": "forms", "kind": "Form", "adopted": 1, "extension_only": 0, "base_only": 2}], "metadata_changes": [], "property_changes": [], "complex_property_values": [], "code_changes": []}},
        {"case": 'sections=["structure"], detail="standard"', "json": {"object": {"object_ref": "Документы.ДокументРасчета", "object_name": "ДокументРасчета", "category": "Документы"}, "extensions": [{"extension_id": "ext1", "extension_config_name": "РасширениеЗУП", "base_config_name": "КонфигурацияЗУП", "object_state": "adopted", "extension_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "base_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "truncated": False}], "counts": [{"extension_id": "ext1", "section": "structure", "kind": "Attribute", "adopted": 2, "extension_only": 1, "base_only": 0}], "metadata_changes": [{"change_id": "ch1", "extension_id": "ext1", "section": "structure", "kind": "Attribute", "name": "КомментарийРасширения", "change": "extension_only", "form_name": None, "extension_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета/Attribute/КомментарийРасширения", "base_qn": None}], "property_changes": [], "complex_property_values": [], "code_changes": []}},
        {"case": 'Максимальный объем: sections=["all"], detail="extended" — скалярное свойство, свойство-массив и перехват routine', "json": {"object": {"object_ref": "Документы.ДокументРасчета", "object_name": "ДокументРасчета", "category": "Документы"}, "extensions": [{"extension_id": "ext1", "extension_config_name": "РасширениеЗУП", "base_config_name": "КонфигурацияЗУП", "object_state": "adopted", "extension_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета", "base_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "truncated": False}], "counts": [{"extension_id": "ext1", "section": "structure", "kind": "Attribute", "adopted": 2, "extension_only": 0, "base_only": 0}, {"extension_id": "ext1", "section": "bsl", "kind": "Routine", "extension_only": 0, "intercepts": 1}], "metadata_changes": [{"change_id": "ch1", "extension_id": "ext1", "section": "structure", "kind": "Attribute", "name": "Ответственный", "change": "modified", "form_name": None, "extension_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета/Attribute/Ответственный", "base_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Attribute/Ответственный"}], "property_changes": [{"change_id": "ch1", "property": "Синоним", "base_value": "Ответственный", "extension_value": "Ответственный сотрудник"}], "complex_property_values": [{"change_id": "ch1", "property": "Тип", "side": "base", "index": 0, "value": "СправочникСсылка.Организации"}, {"change_id": "ch1", "property": "Тип", "side": "extension", "index": 0, "value": "СправочникСсылка.Организации"}, {"change_id": "ch1", "property": "Тип", "side": "extension", "index": 1, "value": "СправочникСсылка.Подразделения"}], "code_changes": [{"change_id": "ch2", "extension_id": "ext1", "kind": "Routine", "name": "ПередЗаписью", "module_type": "ObjectModule", "change": "intercepts", "owner_qn": "Проект_ЗУП/РасширениеЗУП/Документы/ДокументРасчета/ObjectModule", "extension_node_id": "demo-ext-routine-id", "base_node_id": "demo-base-routine-id", "decorator_type": "After", "target": "ПередЗаписью"}]}},
    ],
    "search_bsl_code": [
        {"case": "include_fragments=true", "json": {"items": [{"routine_id": "demo-routine-id", "name": "ЗаполнитьСтроки", "signature": "Процедура ЗаполнитьСтроки()", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "module_type": "FormModule", "file_path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl", "line": 120, "score": 0.8123, "fragments": [{"fragment_id": "demo-fragment-id", "start_line": 120, "end_line": 140, "code": "Процедура ЗаполнитьСтроки()\n    // ..."}]}], "count": 1}},
        {"case": "include_fragments=false", "json": {"items": [{"routine_id": "demo-routine-id", "name": "РассчитатьСтроки", "signature": "Функция РассчитатьСтроки(Данные) Экспорт", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "module_type": "CommonModule", "file_path": "CommonModules/РасчетДокументов/Module.bsl", "line": 42, "score": 0.74, "ranges": [{"fragment_id": "demo-fragment-id", "start_line": 42, "end_line": 70}]}], "count": 1}},
        {"case": "Максимальный объем: include_fragments=true, limit максимальный, широкий запрос", "json": {"items": [{"routine_id": "demo-routine-id", "name": "ЗаполнитьСтроки", "signature": "Процедура ЗаполнитьСтроки()", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета/Form/ФормаДокумента", "module_type": "FormModule", "file_path": "Documents/ДокументРасчета/Forms/ФормаДокумента/Ext/Form/Module.bsl", "line": 120, "score": 0.91, "fragments": [{"fragment_id": "demo-fragment-id", "start_line": 120, "end_line": 140, "code": "Процедура ЗаполнитьСтроки()\n    // расширенный фрагмент\nКонецПроцедуры"}]}, {"routine_id": "demo-routine-id-2", "name": "РассчитатьСтроки", "signature": "Функция РассчитатьСтроки(Данные) Экспорт", "owner_qn": "Проект_ЗУП/КонфигурацияЗУП/ОбщиеМодули/РасчетДокументов", "score": 0.84, "fragments": [{"fragment_id": "demo-fragment-id-2", "start_line": 42, "end_line": 70, "code": "Функция РассчитатьСтроки(Данные) Экспорт\n    // ...\nКонецФункции"}]}], "count": 2, "notice": "vector_index_partial"}},
    ],
    "find_objects_by_summary": [
        {"case": "query задан, include_summary=brief", "json": {"count": 1, "results": [{"category": "Документы", "name": "ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "score": 0.7211, "summary": {"core_idea": "Документ выполняет расчет и хранит строки результата."}}]}},
        {"case": "object_ref задан", "json": {"category": "Документы", "name": "ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "summary": {"core_idea": "Документ выполняет расчет и хранит строки результата.", "capabilities": ["Заполнение строк", "Расчет сумм"]}}},
        {"case": "Максимальный объем: query задан, include_summary=full, limit максимальный", "json": {"count": 2, "results": [{"category": "Документы", "name": "ДокументРасчета", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/Документы/ДокументРасчета", "config_name": "КонфигурацияЗУП", "score": 0.92, "summary": {"core_idea": "Документ выполняет расчет.", "data_composition": ["Реквизиты шапки", "Табличная часть строк"], "capabilities": ["Заполнение", "Проведение"], "important_links": ["РегистрРасчетов"]}}, {"category": "РегистрыНакопления", "name": "РегистрРасчетов", "qualified_name": "Проект_ЗУП/КонфигурацияЗУП/РегистрыНакопления/РегистрРасчетов", "config_name": "КонфигурацияЗУП", "score": 0.87, "summary": {"core_idea": "Хранит движения расчетов.", "data_composition": ["Измерения", "Ресурсы"]}}]}},
    ],
    "get_tool_return_schema": [
        {
            "case": "Tool с одной object-формой ответа",
            "json": {
                "tool_name": "search_bsl_code",
                "returns": [
                    {
                        "case": "Поиск по коду",
                        "shape": "object",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "items": {
                                    "type": "array",
                                    "description": "Найденные routines.",
                                    "items": {"type": "object", "properties": {
                                        "routine_id": {"type": "string", "description": "Идентификатор routine."},
                                        "owner_qn": {"type": "string", "description": "Полный путь владельца."},
                                    }},
                                },
                                "count": {"type": "integer", "description": "Количество найденных items."},
                            },
                        },
                    }
                ],
            },
        },
        {
            "case": "Tool с несколькими режимами ответа",
            "json": {
                "tool_name": "find_metadata_usages",
                "returns": [
                    {
                        "case": 'mode="objects" или mode="register_movements"',
                        "shape": "object{page, configurations, object_groups}",
                        "schema": {"type": "object", "properties": {
                            "object_groups": {"type": "array", "description": "Группы объектов.", "items": {"type": "object", "properties": {
                                "objects": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string", "description": "Имя объекта."}}}},
                            }}},
                        }},
                    },
                    {
                        "case": 'mode="paths"',
                        "shape": "object{page, paths}",
                        "schema": {"type": "object", "properties": {
                            "paths": {"type": "array", "description": "Найденные 1С-пути использования.", "items": {"type": "object", "properties": {"path": {"type": "string", "description": "Путь использования."}}}},
                        }},
                    },
                ],
            },
        },
        {
            "case": "Максимальный объем: все виды узлов — oneOf, additionalProperties, union, вложенные массивы",
            "json": {
                "tool_name": "get_metadata_object_structure",
                "returns": [
                    {
                        "case": 'sections содержит несколько section',
                        "shape": "array<object> или object с массивом по имени section",
                        "schema": {
                            "oneOf": [
                                {"type": "array", "items": {"type": "object", "properties": {
                                    "name": {"type": "string", "description": "Имя дочернего элемента."},
                                    "adoption": {"type": "object", "properties": {
                                        "role": {"type": "string", "description": "Роль в механизме расширений."},
                                        "extension_config_names": {"type": "array", "items": {"type": "string"}, "when": 'Только если adoption.role="base".'},
                                    }},
                                }}},
                                {"type": "object", "additionalProperties": {"type": "array", "description": "Ключ с именем section.", "when": "Если запрошено несколько sections.", "items": {"type": "object", "properties": {
                                    "name": {"type": "string", "description": "Имя дочернего элемента."},
                                }}}},
                            ]
                        },
                    },
                    {
                        "case": "Поле с union-типом и динамическими свойствами",
                        "shape": "object",
                        "schema": {"type": "object", "properties": {
                            "value": {"oneOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"}], "description": "Значение свойства."},
                            "overview": {"type": "object", "properties": {"properties": {"type": "object", "additionalProperties": {"type": "any", "description": "Значение свойства из графа."}}}},
                            "lines": {"type": "array", "items": {"type": "integer"}, "description": "Строки вызовов."},
                        }},
                    },
                ],
            },
        },
    ],
}


for _tool_name, _examples in TOOL_EXAMPLE_DOCS.items():
    if _tool_name in TOOL_RETURN_DOCS:
        TOOL_RETURN_DOCS[_tool_name]["examples"] = _examples


__all__ = ["TOOL_RETURN_DOCS"]
