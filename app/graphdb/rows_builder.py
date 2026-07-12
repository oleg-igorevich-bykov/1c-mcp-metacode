"""
RowsBuilderMixin: prepares all UNWIND-ready row collections for a Configuration.

This is a near 1:1 extraction of Neo4jLoader._build_rows_for_configuration()
from the monolithic module, adapted into a mixin. It relies on:
- self._enrich_guids(...) from GuidEnrichmentMixin
- self._extract_type_refs(...) from TypeRefMixin
- settings from config
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import logging

from config import settings
from parsers.metadata_parser import Configuration
from graphdb.console_search import build_console_search

logger = logging.getLogger(__name__)


class RowsBuilderMixin:
    def _build_rows_for_configuration(
        self,
        project_name: str,
        config: Configuration
    ) -> Dict[str, Any]:
        logger.info("Preparing rows for configuration: %s", config.name)
        # 1) Predefine containers
        config_qn = f"{project_name}/{config.name}"
        categories = [{'config_qn': config_qn, 'category_name': cat.name, 'category_qn': f"{config_qn}/{cat.name}"} for cat in config.categories]
        objects: List[Dict[str, Any]] = []
        forms: List[Dict[str, Any]] = []
        commands: List[Dict[str, Any]] = []
        layouts: List[Dict[str, Any]] = []
        default_cleanup: List[Dict[str, Any]] = []
        tabulars: List[Dict[str, Any]] = []
        obj_attrs: List[Dict[str, Any]] = []
        tab_attrs: List[Dict[str, Any]] = []
        resources: List[Dict[str, Any]] = []
        dimensions: List[Dict[str, Any]] = []
        schemes: List[Dict[str, Any]] = []
        enum_vals: List[Dict[str, Any]] = []
        url_templates: List[Dict[str, Any]] = []
        url_methods: List[Dict[str, Any]] = []
        journal_graphs: List[Dict[str, Any]] = []
        movements: List[Dict[str, Any]] = []
        subsystem_edges: List[Dict[str, Any]] = []
        account_flags: List[Dict[str, Any]] = []
        subconto_flags: List[Dict[str, Any]] = []

        # 2) Walk categories/objects to assemble rows
        for cat in config.categories:
            for obj in cat.metadata_objects:
                # Build qualified_name (qn) for object.
                # For subsystems use path-based QN to support duplicate names under different parents.
                if cat.name == "Подсистемы":
                    chain = obj.properties.get("ПутьПодсистемы")
                    if isinstance(chain, list) and chain:
                        obj_qn = f"{project_name}/{config.name}/{cat.name}/" + "/".join(chain)
                    else:
                        obj_qn = f"{project_name}/{config.name}/{cat.name}/{obj.name}"
                else:
                    obj_qn = f"{project_name}/{config.name}/{cat.name}/{obj.name}"

                objects.append({
                    'category_name': cat.name,
                    'category_qn': f"{config_qn}/{cat.name}",
                    'obj_qn': obj_qn,
                    'obj_name': obj.name,
                    'project_name': project_name,
                    'config_name': config.name,
                    # Sanitize properties: allow only primitives and lists of primitives (drop complex maps like HttpUrlTemplates)
                    'properties': {
                        k: v
                        for k, v in (obj.to_dict() or {}).items()
                        if (
                            isinstance(v, (str, int, float, bool)) or v is None
                            or (isinstance(v, list) and all(isinstance(i, (str, int, float, bool)) or i is None for i in v))
                        )
                    },
                })

                # If this is a Subsystem object with a detected parent, record hierarchy edge
                if cat.name == "Подсистемы":
                    chain = obj.properties.get("ПутьПодсистемы") if hasattr(obj, "properties") else None
                    if isinstance(chain, list) and len(chain) > 1:
                        parent_qn = f"{project_name}/{config.name}/{cat.name}/" + "/".join(chain[:-1])
                        subsystem_edges.append({
                            'parent_qn': parent_qn,
                            'child_qn': obj_qn,
                        })

                # Forms (Формы) and HAS_FORM edges
                role_key_map = {
                    'ОсновнаяФормаОбъекта': 'object',
                    'ОсновнаяФормаГруппы': 'group',
                    'ОсновнаяФормаСписка': 'list',
                    'ОсновнаяФормаДляВыбора': 'picker',
                    'ОсновнаяФормаДляВыбораГруппы': 'group_picker',
                }
                default_names_by_role: Dict[str, str] = {}
                default_names_set = set()
                for k, role in role_key_map.items():
                    raw = obj.properties.get(k)
                    if isinstance(raw, str) and raw.strip():
                        last = raw.strip().split('.')[-1].strip()
                        if last:
                            default_names_by_role[role] = last
                            default_names_set.add(last)

                heuristic_role_by_name = {
                    'ФормаЭлемента': 'object',
                    'ФормаГруппы': 'group',
                    'ФормаСписка': 'list',
                    'ФормаВыбора': 'picker',
                    'ФормаВыбораГруппы': 'group_picker',
                }

                for form in getattr(obj, 'forms', []) or []:
                    form_qn = f"{obj_qn}/Form/{form.name}"
                    role = heuristic_role_by_name.get(form.name)
                    if role is None:
                        for r, fname in default_names_by_role.items():
                            if fname == form.name:
                                role = r
                                break
                    is_default = form.name in default_names_set

                    forms.append({
                        'obj_qn': obj_qn,
                        'form_qn': form_qn,
                        'form_name': form.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': form.properties,
                        'role': role,
                        'is_default': is_default,
                    })

                    if is_default and role:
                        default_cleanup.append({
                            'obj_qn': obj_qn,
                            'role': role,
                            'form_qn': form_qn,
                        })

                # Commands (Команды)
                for cmd in getattr(obj, 'commands', []) or []:
                    cmd_qn = f"{obj_qn}/Command/{cmd.name}"
                    commands.append({
                        'obj_qn': obj_qn,
                        'cmd_qn': cmd_qn,
                        'cmd_name': cmd.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': cmd.properties,
                    })

                # Layouts (Макеты)
                for lay in getattr(obj, 'layouts', []) or []:
                    lay_qn = f"{obj_qn}/Layout/{lay.name}"
                    layouts.append({
                        'obj_qn': obj_qn,
                        'layout_qn': lay_qn,
                        'layout_name': lay.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': lay.properties,
                    })

                # Characteristic Schemes (Характеристики)
                for sch in getattr(obj, 'characteristic_schemes', []) or []:
                    sch_index = str(sch.get('Индекс', '') or '')
                    sch_name = f"Характеристики.{sch_index}" if sch_index else "Характеристики"
                    sch_qn = f"{obj_qn}/Characteristic/{sch_index}" if sch_index else f"{obj_qn}/Characteristic"
                    schemes.append({
                        'obj_qn': obj_qn,
                        'scheme_qn': sch_qn,
                        'scheme_name': sch_name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': sch,
                    })

                # Enum Values (ЗначенияПеречисления)
                for ev in getattr(obj, 'enum_values', []) or []:
                    value_qn = f"{obj_qn}/EnumValue/{ev.name}"
                    enum_vals.append({
                        'obj_qn': obj_qn,
                        'value_qn': value_qn,
                        'value_name': ev.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': getattr(ev, 'properties', {}) or {},
                    })

                # Tabular parts and their attributes
                for tab in obj.tabular_parts:
                    tab_qn = f"{obj_qn}/TabularPart/{tab.name}"
                    tabulars.append({
                        'obj_qn': obj_qn,
                        'tab_qn': tab_qn,
                        'tabular_name': tab.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'obj_name': obj.name,
                        'properties': tab.properties,
                    })
                    for a in tab.attributes:
                        attr_qn = f"{tab_qn}/Attribute/{a.name}"
                        tab_attrs.append({
                            'tab_qn': tab_qn,
                            'attr_qn': attr_qn,
                            'attr_name': a.name,
                            'project_name': project_name,
                            'config_name': config.name,
                                'category_name': cat.name,
                            'object_name': obj.name,
                            'tabular_name': tab.name,
                            'properties': a.to_dict(),
                        })

                # Object-level attributes
                for a in obj.attributes:
                    attr_qn = f"{obj_qn}/Attribute/{a.name}"
                    obj_attrs.append({
                        'obj_qn': obj_qn,
                        'attr_qn': attr_qn,
                        'attr_name': a.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': a.to_dict(),
                    })

                # Resources (Ресурсы)
                for r in getattr(obj, 'resources', []) or []:
                    res_qn = f"{obj_qn}/Resource/{r.name}"
                    resources.append({
                        'obj_qn': obj_qn,
                        'res_qn': res_qn,
                        'res_name': r.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': r.to_dict(),
                    })

                # Dimensions (Измерения)
                for d in getattr(obj, 'dimensions', []) or []:
                    dim_qn = f"{obj_qn}/Dimension/{d.name}"
                    dimensions.append({
                        'obj_qn': obj_qn,
                        'dim_qn': dim_qn,
                        'dim_name': d.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': d.to_dict(),
                    })

                # Accounting Flags (ПризнакиУчета)
                for f in getattr(obj, 'account_flags', []) or []:
                    flag_qn = f"{obj_qn}/AccountFlag/{f.name}"
                    account_flags.append({
                        'obj_qn': obj_qn,
                        'flag_qn': flag_qn,
                        'flag_name': f.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': f.to_dict(),
                    })

                # Subconto Accounting Flags (ПризнакиУчетаСубконто)
                for f in getattr(obj, 'subconto_flags', []) or []:
                    flag_qn = f"{obj_qn}/SubcontoFlag/{f.name}"
                    subconto_flags.append({
                        'obj_qn': obj_qn,
                        'flag_qn': flag_qn,
                        'flag_name': f.name,
                        'project_name': project_name,
                        'config_name': config.name,
                        'category_name': cat.name,
                        'object_name': obj.name,
                        'properties': f.to_dict(),
                    })

                # Document movements to registers (DO_MOVEMENTS_IN)
                if cat.name == "Документы":
                    moves = obj.properties.get("Движения")
                    if isinstance(moves, str) and moves.strip():
                        moves_list = [moves.strip()]
                    elif isinstance(moves, list):
                        moves_list = [str(m).strip() for m in moves if str(m).strip()]
                    else:
                        moves_list = []
                    if moves_list:
                        seen = set()
                        for mv in moves_list:
                            parts = mv.split('.', 1)
                            if len(parts) != 2:
                                continue
                            prefix, reg_name = parts[0].strip(), parts[1].strip()
                            # Only link to Accumulation and Information registers for now
                            if prefix not in ("РегистрНакопления", "РегистрСведений"):
                                continue
                            # Map singular prefix from metadata to actual graph category names (plural)
                            prefix_to_category = {
                                "РегистрНакопления": "РегистрыНакопления",
                                "РегистрСведений": "РегистрыСведений",
                            }
                            reg_category = prefix_to_category.get(prefix, prefix)
                            dedup_key = f"{reg_category}|{reg_name}"
                            if dedup_key in seen:
                                continue
                            seen.add(dedup_key)
                            movements.append({
                                'doc_qn': obj_qn,
                                'config_name': config.name,
                                        'reg_name': reg_name,
                                'reg_category': reg_category,
                                'Проведение': obj.properties.get("Проведение"),
                                'ЗаписьДвиженийПриПроведении': obj.properties.get("ЗаписьДвиженийПриПроведении"),
                                'УдалениеДвижений': obj.properties.get("УдалениеДвижений"),
                                'ВидРегистра': prefix,
                                'reg_qn': f"{project_name}/{config.name}/{reg_category}/{reg_name}",
                            })

                # HTTP services: UrlTemplates and Methods
                if cat.name == "HTTPСервисы":
                    try:
                        templates = obj.properties.get("ШаблоныURL") or []
                    except Exception:
                        templates = []
                    for t in templates:
                        if not isinstance(t, dict):
                            continue
                        t_name = t.get("Имя")
                        if not t_name:
                            continue
                        t_props = t.get("Свойства") or {}
                        template_qn = f"{obj_qn}/UrlTemplate/{t_name}"
                        url_templates.append({
                            'obj_qn': obj_qn,
                            'template_qn': template_qn,
                            'template_name': t_name,
                            'project_name': project_name,
                            'config_name': config.name,
                                'category_name': cat.name,
                            'object_name': obj.name,
                            'properties': t_props,
                        })
                        for m in t.get("Методы") or []:
                            if not isinstance(m, dict):
                                continue
                            m_name = m.get("Имя")
                            if not m_name:
                                continue
                            m_props = m.get("Свойства") or {}
                            method_qn = f"{template_qn}/Method/{m_name}"
                            url_methods.append({
                                'template_qn': template_qn,
                                'method_qn': method_qn,
                                'method_name': m_name,
                                'project_name': project_name,
                                'config_name': config.name,
                                        'category_name': cat.name,
                                'object_name': obj.name,
                                'properties': m_props,
                            })

                # Journal Graphs: only for category "ЖурналыДокументов"
                if cat.name == "ЖурналыДокументов":
                    graphs = obj.properties.get("ГрафыЖурнала") or []
                    for g in graphs:
                        try:
                            graph_name = g.get("Имя") or g.get("name") or ""
                        except Exception:
                            graph_name = ""
                        if not graph_name:
                            continue
                        graph_qn = f"{obj_qn}/Graph/{graph_name}"
                        props = {
                            k: v
                            for k, v in (g or {}).items()
                            if (
                                isinstance(v, (str, int, float, bool)) or v is None
                                or (isinstance(v, list) and all(isinstance(i, (str, int, float, bool)) or i is None for i in v))
                            )
                        }
                        journal_graphs.append({
                            'obj_qn': obj_qn,
                            'graph_qn': graph_qn,
                            'graph_name': graph_name,
                            'project_name': project_name,
                            'config_name': config.name,
                                'category_name': cat.name,
                            'object_name': obj.name,
                            'properties': props,
                        })

        # Enrich rows with meta_uuid from ConfigDumpInfo.xml (if mapping provided)
        try:
            self._enrich_guids(objects, tabulars, obj_attrs, tab_attrs, resources, dimensions, forms)
        except Exception as _e:
            logger.debug("GUID enrichment skipped: %s", _e)

        # Build USED_IN usage rows from collected item properties (types)
        usage_rows: List[Dict[str, Any]] = []
        usage_seen = set()
        ctx_to_label = {
            'AttributeType': 'Attribute',
            'TabularAttributeType': 'Attribute',
            'ResourceType': 'Resource',
            'DimensionType': 'Dimension',
            'AccountingFlagType': 'AccountingFlag',
            'SubcontoFlagType': 'DimensionAccountingFlag',
        }

        def _add_usages_from(rows, consumer_key, context):
            label = ctx_to_label.get(context)
            if not label:
                return
            for row in rows:
                props = row.get('properties') or {}
                tp = props.get("Тип")
                if not tp:
                    continue
                for cats, name, literal in self._extract_type_refs(tp):
                    for cat in cats or []:
                        target_qn = f"{project_name}/{row['config_name']}/{cat}/{name}"
                        dedup_key = (row[consumer_key], target_qn)
                        if dedup_key in usage_seen:
                            continue
                        usage_seen.add(dedup_key)
                        usage_rows.append({
                            'consumer_qn': row[consumer_key],
                            'consumer_label': label,
                            'config_name': row['config_name'],
                            'target_qn': target_qn,
                            'target_name': name,
                            'target_category': cat,
                            'context': context,
                            'prop_key': 'Тип',
                            'type_literal': literal,
                        })

        _add_usages_from(obj_attrs, 'attr_qn', 'AttributeType')
        _add_usages_from(tab_attrs, 'attr_qn', 'TabularAttributeType')
        _add_usages_from(resources, 'res_qn', 'ResourceType')
        _add_usages_from(dimensions, 'dim_qn', 'DimensionType')
        _add_usages_from(account_flags, 'flag_qn', 'AccountingFlagType')
        _add_usages_from(subconto_flags, 'flag_qn', 'SubcontoFlagType')

        # Inject ConsoleSearchable fields into row.properties for every node type
        # listed in app/graphdb/console_search.py. Single point keeps the loaders
        # untouched and survives any future row-shape changes.
        def _inject(rows: List[Dict[str, Any]], name_field: str, kind: str) -> None:
            for row in rows:
                props = row.get('properties') or {}
                if not isinstance(props, dict):
                    continue
                props.update(build_console_search(row.get(name_field), props, kind))
                row['properties'] = props

        _inject(objects, 'obj_name', 'object')
        _inject(forms, 'form_name', 'form')
        _inject(commands, 'cmd_name', 'command')
        _inject(layouts, 'layout_name', 'layout')
        _inject(tabulars, 'tabular_name', 'tabular_part')
        # Object attributes: distinguish "standard" attributes by the `Стандартный` flag,
        # matching current search semantics in app/console/analysis.py.
        for row in obj_attrs:
            props = row.get('properties') or {}
            if not isinstance(props, dict):
                continue
            is_standard = bool(props.get('Стандартный'))
            kind = 'standard_attribute' if is_standard else 'attribute'
            props.update(build_console_search(row.get('attr_name'), props, kind))
            row['properties'] = props
        _inject(tab_attrs, 'attr_name', 'tabular_part_attribute')
        _inject(resources, 'res_name', 'resource')
        _inject(dimensions, 'dim_name', 'dimension')
        _inject(enum_vals, 'value_name', 'enum_value')
        _inject(journal_graphs, 'graph_name', 'journal_graph')

        return {
            'categories': categories,
            'objects': objects,
            'forms': forms,
            'commands': commands,
            'layouts': layouts,
            'default_cleanup': default_cleanup,
            'tabulars': tabulars,
            'obj_attrs': obj_attrs,
            'tab_attrs': tab_attrs,
            'resources': resources,
            'dimensions': dimensions,
            'schemes': schemes,
            'enum_vals': enum_vals,
            'url_templates': url_templates,
            'url_methods': url_methods,
            'journal_graphs': journal_graphs,
            'movements': movements,
            'subsystem_edges': subsystem_edges,
            'account_flags': account_flags,
            'subconto_flags': subconto_flags,
            'usage_rows': usage_rows,
        }