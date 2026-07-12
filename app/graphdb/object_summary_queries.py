"""Cypher queries for the object_summary pipeline.

All functions take a Neo4j `driver` and execute their session inside.
Read-heavy queries return list[dict]; writes return nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def list_generation_candidates(
    driver, *, project_name: str, categories: List[str], limit: int = 1000,
    exclude_qns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """MetadataObjects in `categories` that have no summary path yet.

    `exclude_qns` lets S1 skip objects that already failed in the current
    run — otherwise the same `IS NULL` SELECT keeps returning them forever
    and the startup barrier never releases.
    Extension objects (config_name CONTAINS `$ext$`) are excluded here: the
    base phase only generates summaries for base configuration objects.
    Extension-object generation is handled separately by
    `list_extension_objects` when `OBJECT_SUMMARY_GENERATE_FOR_EXTENSIONS=true`.
    """
    if not categories:
        return []
    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(
            """
            MATCH (m:MetadataObject)
            WHERE m.project_name = $project_name
              AND m.category_name IN $categories
              AND (m.object_summary_path IS NULL OR m.object_summary_path = '')
              AND NOT m.config_name CONTAINS '$ext$'
              AND NOT m.qualified_name IN $exclude_qns
            RETURN m.qualified_name AS qualified_name,
                   m.category_name AS category,
                   m.name AS name,
                   m.config_name AS config_name
            ORDER BY m.category_name, m.name
            LIMIT $limit
            """,
            project_name=project_name, categories=list(categories), limit=int(limit),
            exclude_qns=list(exclude_qns or []),
        )
        return [dict(r) for r in rows]


def count_generation_candidates(
    driver, *, project_name: str, categories: List[str],
) -> int:
    """Mirror of `list_generation_candidates` WHERE without LIMIT/exclude_qns.

    Used by the S1 progress logger to compute the run-wide total once at the
    start of the phase. The WHERE clause must stay in sync with
    `list_generation_candidates`.
    """
    if not categories:
        return 0
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            """
            MATCH (m:MetadataObject)
            WHERE m.project_name = $project_name
              AND m.category_name IN $categories
              AND (m.object_summary_path IS NULL OR m.object_summary_path = '')
              AND NOT m.config_name CONTAINS '$ext$'
            RETURN count(m) AS n
            """,
            project_name=project_name, categories=list(categories),
        ).single()
        return int(rec["n"]) if rec else 0


def list_objects_with_summary_path(
    driver, *, project_name: str, batch_size: int = 500,
) -> Iterable[Dict[str, Any]]:
    """Stream existing (qualified_name, path) pairs in batches for S0 reconcile."""
    last_qn: Optional[str] = None
    while True:
        with driver.session(database=settings.neo4j_database) as session:
            rows = session.run(
                """
                MATCH (m:MetadataObject)
                WHERE m.project_name = $project_name
                  AND m.object_summary_path IS NOT NULL
                  AND m.object_summary_path <> ''
                  AND ($last_qn IS NULL OR m.qualified_name > $last_qn)
                RETURN m.qualified_name AS qualified_name,
                       m.category_name AS category,
                       m.name AS name,
                       m.config_name AS config_name,
                       m.object_summary_path AS path,
                       m.object_summary_search_text AS search_text,
                       m.object_summary_embedding IS NOT NULL AS has_embedding
                ORDER BY m.qualified_name
                LIMIT $batch
                """,
                project_name=project_name, last_qn=last_qn, batch=int(batch_size),
            )
            batch = [dict(r) for r in rows]
        if not batch:
            return
        for item in batch:
            yield item
        last_qn = batch[-1]["qualified_name"]
        if len(batch) < batch_size:
            return


def list_objects_needing_summary_embedding(
    driver, *, project_name: str, limit: int = 1000,
) -> List[Dict[str, Any]]:
    """Objects that have summary built but no embedding vector yet."""
    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(
            """
            MATCH (m:MetadataObject)
            WHERE m.project_name = $project_name
              AND m.object_summary_path IS NOT NULL
              AND m.object_summary_path <> ''
              AND m.object_summary_embedding IS NULL
            RETURN m.qualified_name AS qualified_name,
                   m.category_name AS category,
                   m.name AS name,
                   m.config_name AS config_name,
                   m.object_summary_path AS path
            LIMIT $limit
            """,
            project_name=project_name, limit=int(limit),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Evidence collection (one round-trip per object — minimal viable shape)
# ---------------------------------------------------------------------------

def fetch_object_identity(
    driver, *, project_name: str, qualified_name: str,
) -> Optional[Dict[str, Any]]:
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            RETURN m.qualified_name AS qualified_name,
                   m.category_name AS category,
                   m.name AS name,
                   m.config_name AS config_name,
                   coalesce(m.`Синоним`, '') AS synonym,
                   coalesce(m.`Комментарий`, '') AS comment,
                   coalesce(m.`Описание`, '') AS description,
                   coalesce(m.`Справка`, '') AS help,
                   coalesce(m.`Пояснение`, '') AS explanation
            LIMIT 1
            """,
            qn=qualified_name, project_name=project_name,
        ).single()
    return dict(rec) if rec else None


def fetch_object_structure(
    driver, *, project_name: str, qualified_name: str,
) -> Dict[str, Any]:
    """Full structural slice of a MetadataObject.

    Returns attributes, resources, dimensions, tabular_parts (with attributes),
    forms, commands, layouts, enum_values, predefined and url_templates.

    Every attribute/resource/dimension row carries `ownership`,
    `modified_properties` and `controlled_properties` (empty for base-config
    objects, populated for extension objects). Optional sections that don't
    exist in the current graph (e.g. `Layout` for objects without templates)
    are returned as empty lists.
    """
    with driver.session(database=settings.neo4j_database) as session:
        attributes = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_ATTRIBUTE]->(a:Attribute)
            RETURN a.name AS name,
                   coalesce(a.`Синоним`, '') AS synonym,
                   coalesce(a.`Тип`, '') AS type,
                   coalesce(a.`Комментарий`, '') AS comment,
                   coalesce(a.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(a.modified_properties, []) AS modified_properties,
                   coalesce(a.controlled_properties, []) AS controlled_properties
            ORDER BY a.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        resources = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_RESOURCE]->(r:Resource)
            RETURN r.name AS name,
                   coalesce(r.`Синоним`, '') AS synonym,
                   coalesce(r.`Тип`, '') AS type,
                   coalesce(r.`Комментарий`, '') AS comment,
                   coalesce(r.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(r.modified_properties, []) AS modified_properties,
                   coalesce(r.controlled_properties, []) AS controlled_properties
            ORDER BY r.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        dimensions = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_DIMENSION]->(d:Dimension)
            RETURN d.name AS name,
                   coalesce(d.`Синоним`, '') AS synonym,
                   coalesce(d.`Тип`, '') AS type,
                   coalesce(d.`Комментарий`, '') AS comment,
                   coalesce(d.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(d.modified_properties, []) AS modified_properties,
                   coalesce(d.controlled_properties, []) AS controlled_properties
            ORDER BY d.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        tabular_parts = []
        tp_rows = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_TABULAR_PART]->(t:TabularPart)
            RETURN t.qualified_name AS tp_qn,
                   t.name AS name,
                   coalesce(t.`Синоним`, '') AS synonym,
                   coalesce(t.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(t.modified_properties, []) AS modified_properties,
                   coalesce(t.controlled_properties, []) AS controlled_properties
            ORDER BY t.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()
        for tp in tp_rows:
            tp_attrs = session.run(
                """
                MATCH (t:TabularPart {qualified_name: $tp_qn})-[:HAS_ATTRIBUTE]->(a:Attribute)
                RETURN a.name AS name,
                       coalesce(a.`Синоним`, '') AS synonym,
                       coalesce(a.`Тип`, '') AS type,
                       coalesce(a.`Комментарий`, '') AS comment,
                       coalesce(a.`ПринадлежностьОбъекта`, '') AS ownership,
                       coalesce(a.modified_properties, []) AS modified_properties,
                       coalesce(a.controlled_properties, []) AS controlled_properties
                ORDER BY a.name
                """,
                tp_qn=tp["tp_qn"],
            ).data()
            tabular_parts.append({
                "name": tp["name"],
                "synonym": tp["synonym"],
                "ownership": tp.get("ownership") or "",
                "modified_properties": tp.get("modified_properties") or [],
                "controlled_properties": tp.get("controlled_properties") or [],
                "attributes": tp_attrs,
            })

        forms = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_FORM]->(f:Form)
            RETURN f.name AS name,
                   coalesce(f.form_type, f.`ТипФормы`, '') AS type,
                   coalesce(f.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(f.modified_properties, []) AS modified_properties,
                   coalesce(f.controlled_properties, []) AS controlled_properties
            ORDER BY f.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        commands = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_COMMAND]->(c:Command)
            RETURN c.name AS name,
                   coalesce(c.`Синоним`, '') AS synonym,
                   coalesce(c.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(c.modified_properties, []) AS modified_properties,
                   coalesce(c.controlled_properties, []) AS controlled_properties
            ORDER BY c.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        layouts = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_LAYOUT]->(l:Layout)
            RETURN l.name AS name,
                   coalesce(l.`Синоним`, '') AS synonym,
                   coalesce(l.`ТипМакета`, l.layout_type, '') AS type,
                   coalesce(l.`Комментарий`, '') AS comment,
                   coalesce(l.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(l.modified_properties, []) AS modified_properties,
                   coalesce(l.controlled_properties, []) AS controlled_properties
            ORDER BY l.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        enum_values = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_ENUM_VALUE]->(v:EnumValue)
            RETURN v.name AS name,
                   coalesce(v.`Синоним`, '') AS synonym,
                   coalesce(v.`Комментарий`, '') AS comment,
                   coalesce(v.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(v.modified_properties, []) AS modified_properties,
                   coalesce(v.controlled_properties, []) AS controlled_properties
            ORDER BY v.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        predefined = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_PREDEFINED]->(p:PredefinedItem)
            RETURN p.name AS name,
                   coalesce(p.`Синоним`, '') AS synonym,
                   coalesce(p.`Комментарий`, '') AS comment,
                   coalesce(p.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(p.modified_properties, []) AS modified_properties,
                   coalesce(p.controlled_properties, []) AS controlled_properties
            ORDER BY p.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

        url_templates = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:HAS_URL_TEMPLATE]->(t:UrlTemplate)
            OPTIONAL MATCH (t)-[:HAS_URL_METHOD]->(method:UrlMethod)
            WITH t, collect({name: method.name, http_method: coalesce(method.`HTTPМетод`, method.http_method, '')}) AS methods
            RETURN t.name AS name,
                   coalesce(t.`Шаблон`, t.template, '') AS template,
                   methods,
                   coalesce(t.`ПринадлежностьОбъекта`, '') AS ownership,
                   coalesce(t.modified_properties, []) AS modified_properties,
                   coalesce(t.controlled_properties, []) AS controlled_properties
            ORDER BY t.name
            """,
            qn=qualified_name, project_name=project_name,
        ).data()

    return {
        "attributes": attributes,
        "resources": resources,
        "dimensions": dimensions,
        "tabular_parts": tabular_parts,
        "forms": forms,
        "commands": commands,
        "layouts": layouts,
        "enum_values": enum_values,
        "predefined": predefined,
        "url_templates": url_templates,
    }


def fetch_object_relationships(
    driver, *, project_name: str, qualified_name: str, max_per_dir: int = 300,
) -> Dict[str, List[Dict[str, Any]]]:
    """Four separate relation directions:

    * `affects`      — outgoing `DO_MOVEMENTS_IN` (documents posting to registers).
    * `affected_by`  — incoming `DO_MOVEMENTS_IN` (objects posting to this one;
                       for registers this is the list of source documents).
    * `uses`         — this object's attribute/tabular/resource/dimension type
                       references other MetadataObjects (via `USED_IN`).
    * `used_by`      — other MetadataObjects whose attribute/tabular/resource/
                       dimension type references this object.

    `affected_by` constrains `src.project_name = $project_name` because
    `DO_MOVEMENTS_IN` rels are upserted via `MATCH` on `qualified_name` only
    (`cypher_templates.CYPHER_UPSERT_DO_MOVEMENTS_IN`) — without the filter
    a register in project A could pick up source documents from project B.
    `used_by` (below) and `moved_by` in `console.analysis` follow the same
    project-scoping contract.
    """
    with driver.session(database=settings.neo4j_database) as session:
        affects = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
                  -[rel:DO_MOVEMENTS_IN]->(t:MetadataObject)
            RETURN t.category_name AS category,
                   t.name AS name,
                   t.qualified_name AS qualified_name,
                   'DO_MOVEMENTS_IN' AS relation
            ORDER BY category, name
            LIMIT $cap
            """,
            qn=qualified_name, project_name=project_name, cap=int(max_per_dir),
        ).data()

        affected_by = session.run(
            """
            MATCH (src:MetadataObject)-[rel:DO_MOVEMENTS_IN]
                  ->(m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            WHERE src.project_name = $project_name
            RETURN src.category_name AS category,
                   src.name AS name,
                   src.qualified_name AS qualified_name,
                   'DO_MOVEMENTS_IN' AS relation
            ORDER BY category, name
            LIMIT $cap
            """,
            qn=qualified_name, project_name=project_name, cap=int(max_per_dir),
        ).data()

        uses = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            CALL (m) {
              MATCH (m)-[:HAS_ATTRIBUTE]->(elem)<-[:USED_IN]-(target:MetadataObject)
              RETURN target, 'attribute' AS relation
              UNION
              MATCH (m)-[:HAS_TABULAR_PART]->(:TabularPart)-[:HAS_ATTRIBUTE]->(elem)<-[:USED_IN]-(target:MetadataObject)
              RETURN target, 'tabular_attribute' AS relation
              UNION
              MATCH (m)-[:HAS_RESOURCE]->(elem)<-[:USED_IN]-(target:MetadataObject)
              RETURN target, 'resource' AS relation
              UNION
              MATCH (m)-[:HAS_DIMENSION]->(elem)<-[:USED_IN]-(target:MetadataObject)
              RETURN target, 'dimension' AS relation
            }
            WITH target, relation
            WHERE target IS NOT NULL AND target.project_name = $project_name
              AND target.qualified_name <> $qn
            RETURN DISTINCT target.category_name AS category,
                   target.name AS name,
                   target.qualified_name AS qualified_name,
                   relation
            ORDER BY category, name
            LIMIT $cap
            """,
            qn=qualified_name, project_name=project_name, cap=int(max_per_dir),
        ).data()

        used_by = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})-[:USED_IN]->(elem)
            CALL (elem) {
              OPTIONAL MATCH (owner:MetadataObject)-[:HAS_ATTRIBUTE]->(elem)
              RETURN owner, 'attribute' AS relation
              UNION
              OPTIONAL MATCH (owner:MetadataObject)-[:HAS_TABULAR_PART]->(:TabularPart)-[:HAS_ATTRIBUTE]->(elem)
              RETURN owner, 'tabular_attribute' AS relation
              UNION
              OPTIONAL MATCH (owner:MetadataObject)-[:HAS_RESOURCE]->(elem)
              RETURN owner, 'resource' AS relation
              UNION
              OPTIONAL MATCH (owner:MetadataObject)-[:HAS_DIMENSION]->(elem)
              RETURN owner, 'dimension' AS relation
            }
            WITH owner, relation
            WHERE owner IS NOT NULL AND owner.project_name = $project_name
              AND owner.qualified_name <> $qn
            RETURN DISTINCT owner.category_name AS category,
                   owner.name AS name,
                   owner.qualified_name AS qualified_name,
                   relation
            ORDER BY category, name
            LIMIT $cap
            """,
            qn=qualified_name, project_name=project_name, cap=int(max_per_dir),
        ).data()

    return {"affects": affects, "affected_by": affected_by, "uses": uses, "used_by": used_by}


def fetch_object_bsl_routines(
    driver, *, project_name: str, qualified_name: str, limit: int = 200,
) -> List[Dict[str, Any]]:
    """All routines reachable from the object: object/form/command modules.

    Returns `body_sample` (truncated to 5000 chars), `signature`, `directives`,
    and `decorator_type/decorator_target` so the selector can recognise
    extension decorators (`Перед/После/Вместо/ИзменениеИКонтроль`) and route
    them with `extension_decorator` priority.
    """
    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            CALL (m) {
              OPTIONAL MATCH (m)-[:DECLARES]->(r0:Routine)
              RETURN r0 AS r, null AS mod
              UNION
              MATCH (m)-[:HAS_MODULE]->(mod1:Module)-[:DECLARES]->(r1:Routine)
              RETURN r1 AS r, mod1 AS mod
              UNION
              MATCH (m)-[:HAS_FORM]->(:Form)-[:HAS_MODULE]->(mod2:Module)-[:DECLARES]->(r2:Routine)
              RETURN r2 AS r, mod2 AS mod
              UNION
              MATCH (m)-[:HAS_COMMAND]->(:Command)-[:HAS_MODULE]->(mod3:Module)-[:DECLARES]->(r3:Routine)
              RETURN r3 AS r, mod3 AS mod
            }
            WITH DISTINCT r, mod
            WHERE r IS NOT NULL
            RETURN r.id AS routine_id,
                   coalesce(r.name, '') AS name,
                   coalesce(r.routine_type, '') AS kind,
                   coalesce(r.export, false) AS is_export,
                   coalesce(r.directives, []) AS directives,
                   coalesce(r.signature, '') AS signature,
                   coalesce(r.decorator_type, '') AS decorator_type,
                   coalesce(r.decorator_target, '') AS decorator_target,
                   coalesce(r.owner_qn, '') AS owner_qn,
                   coalesce(mod.name, '') AS module_name,
                   coalesce(mod.module_type, r.module_type, '') AS module_type,
                   substring(coalesce(r.body, ''), 0, 5000) AS body_sample
            ORDER BY module_type, name, routine_id
            LIMIT $limit
            """,
            project_name=project_name, qn=qualified_name, limit=int(limit),
        )
        return [dict(r) for r in rows]


def fetch_object_bsl_handlers(
    driver, *, project_name: str, qualified_name: str, limit: int = 200,
) -> List[Dict[str, Any]]:
    """Event handlers attached to this object.

    Sources:
      * `command`      — `Command-[:HAS_HANDLER]->Routine`
      * `form_event`   — `Form-[:HAS_EVENT]->FormEvent-[:HAS_EVENT_ACTION]->FormEventAction-[:HAS_HANDLER]->Routine`
      * `form_control` — `Form-[:HAS_CONTROL|HAS_CHILD*]->FormControl-[:HAS_EVENT]->FormEvent
                          -[:HAS_EVENT_ACTION]->FormEventAction-[:HAS_HANDLER]->Routine`
        Recursive `HAS_CHILD*` covers nested controls (groups, pages).
      * `url`          — `MetadataObject-[:HAS_URL_TEMPLATE]->UrlTemplate-[:HAS_URL_METHOD]
                          ->UrlMethod-[:HAS_HANDLER]->Routine`
    """
    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            CALL (m) {
              MATCH (m)-[:HAS_COMMAND]->(cmd:Command)-[:HAS_HANDLER]->(r:Routine)
              RETURN 'command' AS handler_kind,
                     coalesce(cmd.name, '') AS owner,
                     '' AS event,
                     r AS r
              UNION
              MATCH (m)-[:HAS_FORM]->(f:Form)
                     -[:HAS_EVENT]->(fe:FormEvent)
                     -[:HAS_EVENT_ACTION]->(:FormEventAction)
                     -[:HAS_HANDLER]->(r:Routine)
              RETURN 'form_event' AS handler_kind,
                     coalesce(f.name, '') AS owner,
                     coalesce(fe.name, '') AS event,
                     r AS r
              UNION
              MATCH (m)-[:HAS_FORM]->(f:Form)
                     -[:HAS_CONTROL|HAS_CHILD*]->(fc:FormControl)
                     -[:HAS_EVENT]->(fe:FormEvent)
                     -[:HAS_EVENT_ACTION]->(:FormEventAction)
                     -[:HAS_HANDLER]->(r:Routine)
              RETURN 'form_control' AS handler_kind,
                     coalesce(f.name, '') + '.' + coalesce(fc.name, '') AS owner,
                     coalesce(fe.name, '') AS event,
                     r AS r
              UNION
              MATCH (m)-[:HAS_URL_TEMPLATE]->(t:UrlTemplate)
                     -[:HAS_URL_METHOD]->(um:UrlMethod)
                     -[:HAS_HANDLER]->(r:Routine)
              RETURN 'url' AS handler_kind,
                     coalesce(t.name, '') + '.' + coalesce(um.name, '') AS owner,
                     coalesce(um.name, '') AS event,
                     r AS r
            }
            WITH DISTINCT handler_kind, owner, event, r
            WHERE r IS NOT NULL
            RETURN handler_kind,
                   owner,
                   event,
                   r.id AS routine_id,
                   coalesce(r.name, '') AS routine_name,
                   coalesce(r.module_name, '') AS module_name
            ORDER BY handler_kind, owner, event, routine_name
            LIMIT $limit
            """,
            project_name=project_name, qn=qualified_name, limit=int(limit),
        )
        return [dict(r) for r in rows]


_TEXT_CALL_PATTERN = re.compile(
    r"(?<![A-Za-zА-Яа-яЁё0-9_.])([A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]*)\s*\("
)


def recover_text_call_edges(
    routines: List[Dict[str, Any]], *, limit: int = 2000,
) -> List[Dict[str, Any]]:
    """Recover local `Routine→Routine` edges by scanning `body_sample` for `<name>(`.

    Used when the graph `CALLS` relation is incomplete (typically when the BSL
    parser missed an unprefixed local call). Only edges where both endpoints
    live in the same module scope (`module_type` + `module_name`/`owner_qn`)
    are emitted. Unqualified call `Helper()` from an ObjectModule routine must
    resolve to a `Helper` in the same ObjectModule, never to a same-named
    routine of a different form/module — otherwise text recovery would invent
    cross-module flows.

    If multiple routines share the name within the same scope, the edge is
    skipped (ambiguity is treated as "do not recover").
    """
    if not routines:
        return []

    def scope_key(routine: Dict[str, Any]) -> Tuple[str, str, str]:
        return (
            str(routine.get("module_type") or "").strip(),
            str(routine.get("module_name") or "").strip(),
            str(routine.get("owner_qn") or "").strip(),
        )

    by_scope_name: Dict[Tuple[Tuple[str, str, str], str], List[Dict[str, Any]]] = {}
    for routine in routines:
        name = str(routine.get("name") or "").strip()
        if not name:
            continue
        by_scope_name.setdefault((scope_key(routine), name), []).append(routine)

    edges: List[Dict[str, Any]] = []
    seen: set = set()
    for src in routines:
        src_id = str(src.get("routine_id") or "").strip()
        if not src_id:
            continue
        body = src.get("body_sample") or src.get("body") or ""
        if not body:
            continue
        src_name = str(src.get("name") or "").strip()
        src_scope = scope_key(src)
        for match in _TEXT_CALL_PATTERN.finditer(body):
            callee_name = match.group(1)
            if callee_name == src_name:
                continue
            candidates = by_scope_name.get((src_scope, callee_name)) or []
            if len(candidates) != 1:
                # Either no candidate in scope, or ambiguous — skip rather than
                # invent a cross-module edge.
                continue
            target = candidates[0]
            tgt_id = str(target.get("routine_id") or "").strip()
            if not tgt_id or tgt_id == src_id:
                continue
            key = (src_id, tgt_id)
            if key in seen:
                continue
            seen.add(key)
            edges.append({
                "source_id": src_id, "source_name": src_name,
                "target_id": tgt_id, "target_name": callee_name,
            })
            if len(edges) >= int(limit):
                return edges
    return edges


def fetch_object_bsl_call_edges(
    driver, *, project_name: str, qualified_name: str, limit: int = 2000,
    routines_for_text_recovery: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """`Routine-[:CALLS]->Routine` edges between routines reachable from the object.

    Both endpoints must belong to one of the object's modules (object/form/
    command). Cross-object calls are dropped here — they don't help the LLM
    follow the local entry-point graph.

    When `routines_for_text_recovery` is provided, also append local calls
    recovered via regex scan of `body_sample` (`recover_text_call_edges`) —
    the graph `CALLS` relation can be incomplete for unprefixed local calls.
    """
    edges: List[Dict[str, Any]] = []
    seen: set = set()

    def add_edge(src_id: str, src_name: str, tgt_id: str, tgt_name: str) -> None:
        if not src_id or not tgt_id or src_id == tgt_id:
            return
        key = (src_id, tgt_id)
        if key in seen:
            return
        seen.add(key)
        edges.append({
            "source_id": src_id, "source_name": src_name,
            "target_id": tgt_id, "target_name": tgt_name,
        })

    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            CALL (m) {
              MATCH (m)-[:HAS_MODULE|HAS_FORM|HAS_COMMAND]->()-[:HAS_MODULE*0..1]->(:Module)-[:DECLARES]->(r:Routine)
              RETURN r
              UNION
              MATCH (m)-[:DECLARES]->(r:Routine)
              RETURN r
            }
            WITH collect(DISTINCT r) AS routines
            UNWIND routines AS src
            MATCH (src)-[:CALLS]->(tgt:Routine)
            WHERE tgt IN routines AND src <> tgt
            RETURN src.id AS source_id,
                   coalesce(src.name, '') AS source_name,
                   tgt.id AS target_id,
                   coalesce(tgt.name, '') AS target_name
            ORDER BY source_name, target_name
            LIMIT $limit
            """,
            project_name=project_name, qn=qualified_name, limit=int(limit),
        )
        for r in rows:
            add_edge(r["source_id"], r["source_name"], r["target_id"], r["target_name"])

    for recovered in recover_text_call_edges(routines_for_text_recovery or [], limit=limit):
        add_edge(
            recovered["source_id"], recovered["source_name"],
            recovered["target_id"], recovered["target_name"],
        )
        if len(edges) >= int(limit):
            return edges
    return edges


def fetch_extension_context(
    driver, *, project_name: str, qualified_name: str,
    max_bsl_routines: int = 200,
) -> Dict[str, Any]:
    """Surface extension changes as full content evidence.

    For each extension object that adopts the base via `ADOPTED_FROM`, return
    the same `structure`, `bsl_routines`, `bsl_handlers` and `bsl_call_edges`
    payload as for the base object, so the profile builder can show LLM the
    actual own/modified elements and the actual extension BSL (decorators
    `Перед/После/Вместо/ИзменениеИКонтроль`) instead of binary `has_*` flags.

    Returns:
      {
        "mode": "none" | "base_with_extension" | "extension",
        "summary": str,
        "extensions": [
          {config_name, qualified_name, structure, bsl_routines,
           bsl_handlers, bsl_call_edges}
        ],
      }
    """
    with driver.session(database=settings.neo4j_database) as session:
        base_row = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            OPTIONAL MATCH (m)-[:ADOPTED_FROM]->(base:MetadataObject)
            RETURN coalesce(base.config_name, '') AS base_config_name
            LIMIT 1
            """,
            qn=qualified_name, project_name=project_name,
        ).single()
        base_config_name = (base_row["base_config_name"] if base_row else "") or ""

        ext_rows = session.run(
            """
            MATCH (ext:MetadataObject {project_name: $project_name})
                  -[:ADOPTED_FROM]->(m:MetadataObject {qualified_name: $qn})
            RETURN coalesce(ext.config_name, '') AS config_name,
                   coalesce(ext.qualified_name, '') AS qualified_name
            ORDER BY config_name
            """,
            project_name=project_name, qn=qualified_name,
        ).data()

    extensions: List[Dict[str, Any]] = []
    for row in ext_rows:
        ext_qn = row.get("qualified_name") or ""
        if not ext_qn:
            continue
        ext_structure = fetch_object_structure(
            driver, project_name=project_name, qualified_name=ext_qn,
        )
        ext_routines = fetch_object_bsl_routines(
            driver, project_name=project_name, qualified_name=ext_qn,
            limit=max_bsl_routines,
        )
        ext_handlers = fetch_object_bsl_handlers(
            driver, project_name=project_name, qualified_name=ext_qn,
            limit=max_bsl_routines,
        )
        ext_call_edges = fetch_object_bsl_call_edges(
            driver, project_name=project_name, qualified_name=ext_qn,
            routines_for_text_recovery=ext_routines,
        )
        extensions.append({
            "config_name": row.get("config_name") or "",
            "qualified_name": ext_qn,
            "structure": ext_structure,
            "bsl_routines": ext_routines,
            "bsl_handlers": ext_handlers,
            "bsl_call_edges": ext_call_edges,
        })

    if base_config_name:
        mode = "extension"
        summary = f"Объект расширения заимствует базовый объект из {base_config_name}."
    elif extensions:
        mode = "base_with_extension"
        names = ", ".join(e["config_name"] for e in extensions if e.get("config_name"))
        summary = "Базовый объект изменён расширениями: " + names
    else:
        mode = "none"
        summary = ""

    return {"mode": mode, "summary": summary, "extensions": extensions}


def list_extension_objects(
    driver, *, project_name: str, extension_names: Optional[List[str]] = None,
    only_own: bool = True, categories: Optional[List[str]] = None, limit: int = 1000,
    exclude_qns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Objects living inside extension configurations.

    `extension_names=["*"]` (or None) — any extension. Otherwise only the
    listed `config_name` values. `only_own=True` excludes ADOPTED_FROM
    objects (those are covered by `extension_context` of the base object).
    """
    use_names_filter = bool(extension_names) and extension_names != ["*"]
    # "Собственный" объект расширения: либо явный признак
    # ПринадлежностьОбъекта = 'Собственный', либо отсутствие ADOPTED_FROM.
    own_clause = (
        "AND (m.`ПринадлежностьОбъекта` = 'Собственный' "
        "     OR NOT EXISTS { MATCH (m)-[:ADOPTED_FROM]->(:MetadataObject) })"
        if only_own
        else ""
    )
    names_clause = "AND m.config_name IN $ext_names" if use_names_filter else ""
    cats_clause = "AND m.category_name IN $categories" if categories else ""
    cypher = f"""
        MATCH (m:MetadataObject)
        WHERE m.project_name = $project_name
          AND m.config_name CONTAINS '$ext$'
          {names_clause}
          {cats_clause}
          {own_clause}
          AND (m.object_summary_path IS NULL OR m.object_summary_path = '')
          AND NOT m.qualified_name IN $exclude_qns
        RETURN m.qualified_name AS qualified_name,
               m.category_name AS category,
               m.name AS name,
               m.config_name AS config_name
        ORDER BY m.config_name, m.category_name, m.name
        LIMIT $limit
    """
    params: Dict[str, Any] = {
        "project_name": project_name,
        "limit": int(limit),
        "exclude_qns": list(exclude_qns or []),
    }
    if use_names_filter:
        params["ext_names"] = list(extension_names)
    if categories:
        params["categories"] = list(categories)
    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(cypher, **params)
        return [dict(r) for r in rows]


def count_extension_objects(
    driver, *, project_name: str, extension_names: Optional[List[str]] = None,
    only_own: bool = True, categories: Optional[List[str]] = None,
) -> int:
    """Mirror of `list_extension_objects` WHERE without LIMIT/exclude_qns."""
    use_names_filter = bool(extension_names) and extension_names != ["*"]
    own_clause = (
        "AND (m.`ПринадлежностьОбъекта` = 'Собственный' "
        "     OR NOT EXISTS { MATCH (m)-[:ADOPTED_FROM]->(:MetadataObject) })"
        if only_own
        else ""
    )
    names_clause = "AND m.config_name IN $ext_names" if use_names_filter else ""
    cats_clause = "AND m.category_name IN $categories" if categories else ""
    cypher = f"""
        MATCH (m:MetadataObject)
        WHERE m.project_name = $project_name
          AND m.config_name CONTAINS '$ext$'
          {names_clause}
          {cats_clause}
          {own_clause}
          AND (m.object_summary_path IS NULL OR m.object_summary_path = '')
        RETURN count(m) AS n
    """
    params: Dict[str, Any] = {"project_name": project_name}
    if use_names_filter:
        params["ext_names"] = list(extension_names)
    if categories:
        params["categories"] = list(categories)
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(cypher, **params).single()
        return int(rec["n"]) if rec else 0


def collect_evidence(
    driver, *, project_name: str, qualified_name: str, max_relationships: int = 200,
    max_bsl_routines: int = 200,
) -> Optional[Dict[str, Any]]:
    """Single entry point used by the pipeline."""
    identity = fetch_object_identity(driver, project_name=project_name, qualified_name=qualified_name)
    if not identity:
        return None
    structure = fetch_object_structure(driver, project_name=project_name, qualified_name=qualified_name)
    relationships = fetch_object_relationships(
        driver, project_name=project_name, qualified_name=qualified_name,
        max_per_dir=max_relationships,
    )
    bsl_routines = fetch_object_bsl_routines(
        driver, project_name=project_name, qualified_name=qualified_name,
        limit=max_bsl_routines,
    )
    bsl_handlers = fetch_object_bsl_handlers(
        driver, project_name=project_name, qualified_name=qualified_name,
        limit=max_bsl_routines,
    )
    bsl_call_edges = fetch_object_bsl_call_edges(
        driver, project_name=project_name, qualified_name=qualified_name,
        routines_for_text_recovery=bsl_routines,
    )
    extension_context = fetch_extension_context(
        driver, project_name=project_name, qualified_name=qualified_name,
        max_bsl_routines=max_bsl_routines,
    )
    return {
        "identity": identity,
        "structure": structure,
        "relationships": relationships,
        "bsl_routines": bsl_routines,
        "bsl_handlers": bsl_handlers,
        "bsl_call_edges": bsl_call_edges,
        "extension_context": extension_context,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def set_summary_path_and_search_text(
    driver, *, project_name: str, qualified_name: str, path: str, search_text: str,
) -> None:
    with driver.session(database=settings.neo4j_database) as session:
        session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            SET m.object_summary_path = $path,
                m.object_summary_search_text = $text
            """,
            qn=qualified_name, project_name=project_name, path=path, text=search_text,
        )


def set_summary_search_text(
    driver, *, project_name: str, qualified_name: str, search_text: str,
) -> None:
    with driver.session(database=settings.neo4j_database) as session:
        session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            SET m.object_summary_search_text = $text
            """,
            qn=qualified_name, project_name=project_name, text=search_text,
        )


def set_summary_embedding(
    driver, *, project_name: str, qualified_name: str, embedding: List[float],
) -> None:
    with driver.session(database=settings.neo4j_database) as session:
        session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            SET m.object_summary_embedding = $vec
            """,
            qn=qualified_name, project_name=project_name, vec=list(embedding),
        )


def publish_summary_atomic(
    driver, *, project_name: str, qualified_name: str,
    path: str, search_text: str, clear_embedding: bool,
) -> None:
    """Atomically publish a new summary in one Cypher operation.

    Sets `object_summary_path` and `object_summary_search_text` in the same
    statement and (if `clear_embedding=True`) `object_summary_embedding = NULL`,
    so search and embedding state stay coherent with the file pointed to by
    `path`. Used by manual refresh/create runner — see object_summary_pipeline
    `run_single_object_summary_job` §6.1.
    """
    sets = [
        "m.object_summary_path = $path",
        "m.object_summary_search_text = $text",
    ]
    if clear_embedding:
        sets.append("m.object_summary_embedding = NULL")
    with driver.session(database=settings.neo4j_database) as session:
        session.run(
            f"""
            MATCH (m:MetadataObject {{qualified_name: $qn, project_name: $project_name}})
            SET {", ".join(sets)}
            """,
            qn=qualified_name, project_name=project_name, path=path, text=search_text,
        )


def publish_summary_from_disk_if_missing(
    driver, *, project_name: str, qualified_name: str,
    path: str, search_text: str,
) -> str:
    """Conditional publish for S0 disk bootstrap.

    Writes `object_summary_path`, `object_summary_search_text` and resets
    `object_summary_embedding` to NULL only when the row is unpublished
    (`object_summary_path` is NULL or empty). Embedding is left for S2 to
    fill. Returns one of:

      "published"          — row matched and was updated;
      "already_published"  — row matched but had a non-empty path; Phase B
                             of the existing reconcile owns any derived
                             repair for already-published rows;
      "object_not_found"   — no MetadataObject with this qualified_name in
                             the current project.
    """
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            WITH m,
                 CASE WHEN m.object_summary_path IS NULL OR m.object_summary_path = ''
                      THEN 1 ELSE 0 END AS would_update
            FOREACH (_ IN CASE WHEN would_update = 1 THEN [1] ELSE [] END |
                SET m.object_summary_path = $path,
                    m.object_summary_search_text = $text,
                    m.object_summary_embedding = NULL
            )
            RETURN count(m) AS matched,
                   sum(would_update) AS updated
            """,
            qn=qualified_name, project_name=project_name,
            path=path, text=search_text,
        ).single()
        if rec is None:
            return "object_not_found"
        matched = int(rec["matched"] or 0)
        if matched == 0:
            return "object_not_found"
        updated = int(rec["updated"] or 0)
        return "published" if updated > 0 else "already_published"


def get_generation_object_by_qn(
    driver, *, project_name: str, qualified_name: str,
) -> Optional[Dict[str, Any]]:
    """Return minimal object identity record for the manual generation runner."""
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            RETURN m.qualified_name AS qualified_name,
                   m.category_name  AS category,
                   m.name           AS name,
                   m.config_name    AS config_name,
                   m.object_summary_path AS object_summary_path
            LIMIT 1
            """,
            qn=qualified_name, project_name=project_name,
        ).single()
        if rec is None:
            return None
        return {
            "qualified_name": rec["qualified_name"],
            "category": rec["category"],
            "name": rec["name"],
            "config_name": rec["config_name"],
            "object_summary_path": rec["object_summary_path"],
        }


def is_object_own_in_extension(
    driver, *, project_name: str, qualified_name: str,
) -> bool:
    """Check whether the object is "own" in its extension.

    Mirrors the same predicate used by `list_extension_objects` for
    `only_own=True`: either explicit `ПринадлежностьОбъекта = 'Собственный'`
    or absence of ADOPTED_FROM relation.
    """
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            RETURN m.`ПринадлежностьОбъекта` AS scope,
                   EXISTS { MATCH (m)-[:ADOPTED_FROM]->(:MetadataObject) } AS adopted
            LIMIT 1
            """,
            qn=qualified_name, project_name=project_name,
        ).single()
        if rec is None:
            return False
        scope = rec["scope"]
        adopted = bool(rec["adopted"])
        if isinstance(scope, str) and scope.strip() == "Собственный":
            return True
        return not adopted


def clear_summary_fields(
    driver, *, project_name: str, qualified_name: str,
    clear_path: bool = True, clear_embedding: bool = True, clear_search_text: bool = True,
) -> None:
    sets: List[str] = []
    if clear_path:
        sets.append("m.object_summary_path = NULL")
    if clear_embedding:
        sets.append("m.object_summary_embedding = NULL")
    if clear_search_text:
        sets.append("m.object_summary_search_text = NULL")
    if not sets:
        return
    with driver.session(database=settings.neo4j_database) as session:
        session.run(
            f"""
            MATCH (m:MetadataObject {{qualified_name: $qn, project_name: $project_name}})
            SET {", ".join(sets)}
            """,
            qn=qualified_name, project_name=project_name,
        )


# ---------------------------------------------------------------------------
# Search (vector / fulltext primitives used by the search service)
# ---------------------------------------------------------------------------

def vector_search_summary(
    driver,
    *,
    project_name: str,
    embedding: List[float],
    categories: Optional[List[str]] = None,
    config_name: Optional[str] = None,
    top_k: int = 25,
) -> List[Dict[str, Any]]:
    where_clauses = ["m.project_name = $project_name", "m.object_summary_path IS NOT NULL"]
    params: Dict[str, Any] = {
        "project_name": project_name,
        "embedding": list(embedding),
        "top_k": int(top_k),
    }
    if categories:
        where_clauses.append("m.category_name IN $categories")
        params["categories"] = list(categories)
    if config_name:
        where_clauses.append("m.config_name = $config_name")
        params["config_name"] = config_name
    where_sql = " AND ".join(where_clauses)
    cypher = f"""
        CALL db.index.vector.queryNodes('vec_object_summary_embedding', $top_k, $embedding)
        YIELD node AS m, score
        WHERE {where_sql}
        RETURN m.qualified_name AS qualified_name,
               m.category_name AS category,
               m.name AS name,
               m.config_name AS config_name,
               m.object_summary_path AS path,
               score
        ORDER BY score DESC
        LIMIT $top_k
    """
    with driver.session(database=settings.neo4j_database) as session:
        rows = session.run(cypher, **params)
        return [dict(r) for r in rows]


def fulltext_search_summary(
    driver,
    *,
    project_name: str,
    query: str,
    categories: Optional[List[str]] = None,
    config_name: Optional[str] = None,
    top_k: int = 25,
) -> List[Dict[str, Any]]:
    """Plain-text fulltext search with the same Lucene-safety cycle as
    `metadata_search_service.search_by_description_fulltext`:

    raw user input may contain Lucene reserved characters (`(`, `:`, `+`,
    quotes, ...). We feed it through `build_fulltext_query_candidates` which
    yields a list of safe escalations (escaped → bare). Parse errors are
    detected via `is_lucene_fulltext_parse_error` and we try the next
    candidate instead of returning an empty result.
    """
    from neo4j.exceptions import Neo4jError

    from graphdb.fulltext_query import (
        build_fulltext_query_candidates,
        is_lucene_fulltext_parse_error,
    )

    candidates = build_fulltext_query_candidates(query)
    if not candidates:
        return []

    where_clauses = ["m.project_name = $project_name", "m.object_summary_path IS NOT NULL"]
    params: Dict[str, Any] = {
        "project_name": project_name,
        "top_k": int(top_k),
    }
    if categories:
        where_clauses.append("m.category_name IN $categories")
        params["categories"] = list(categories)
    if config_name:
        where_clauses.append("m.config_name = $config_name")
        params["config_name"] = config_name
    where_sql = " AND ".join(where_clauses)
    cypher = f"""
        CALL db.index.fulltext.queryNodes('ftx_object_summary_search_text', $q)
        YIELD node AS m, score
        WHERE {where_sql}
        RETURN m.qualified_name AS qualified_name,
               m.category_name AS category,
               m.name AS name,
               m.config_name AS config_name,
               m.object_summary_path AS path,
               score
        ORDER BY score DESC
        LIMIT $top_k
    """

    last_parse_error: Optional[BaseException] = None
    with driver.session(database=settings.neo4j_database) as session:
        for candidate in candidates:
            attempt_params = dict(params)
            attempt_params["q"] = candidate
            try:
                rows = session.run(cypher, **attempt_params)
                return [dict(r) for r in rows]
            except Neo4jError as e:
                if is_lucene_fulltext_parse_error(e):
                    logger.warning(
                        "Lucene parse error on object_summary fulltext candidate %r: %s",
                        candidate, e,
                    )
                    last_parse_error = e
                    continue
                raise
    logger.warning(
        "All object_summary fulltext candidates failed Lucene parse for query %r (last error: %s)",
        query, last_parse_error,
    )
    return []


def get_object_summary_path_by_qn(
    driver, *, project_name: str, qualified_name: str,
) -> Optional[str]:
    with driver.session(database=settings.neo4j_database) as session:
        rec = session.run(
            """
            MATCH (m:MetadataObject {qualified_name: $qn, project_name: $project_name})
            RETURN m.object_summary_path AS path
            LIMIT 1
            """,
            qn=qualified_name, project_name=project_name,
        ).single()
    return rec["path"] if rec else None
