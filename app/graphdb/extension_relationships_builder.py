"""
Builder for relationships between extension and base configuration nodes.
Implements QN-based matching strategy for MetadataObject and child entities.
Supports multiple relationship types (ADOPTED_FROM, etc.).
"""

import logging
from typing import List, Dict, Any, Optional
from config import settings

logger = logging.getLogger(__name__)


class ExtensionRelationshipsBuilder:
    """Builds relationships between extension and base nodes (ADOPTED_FROM, etc.)"""

    def __init__(self, neo4j_loader):
        """
        Initialize builder with Neo4j loader instance

        Args:
            neo4j_loader: Instance of Neo4jLoader with database connection
        """
        self.loader = neo4j_loader
        self.project_name = settings.project_name

    def build_adopted_from_for_extension(
        self,
        ext_config_qn: str,
        base_config_qn: str
    ) -> Dict[str, int]:
        """
        Build all ADOPTED_FROM relationships for an extension.

        Args:
            ext_config_qn: Qualified name of extension configuration (with $ext$ marker)
            base_config_qn: Qualified name of base configuration

        Returns:
            Dictionary with counts: {node_type: count_created}
        """
        logger.info("[ADOPTED_FROM] Building for extension: %s", ext_config_qn)

        stats = {}

        # Extract config names from QNs
        # ext_config_qn format: "Project/ConfigName$ext$ExtName"
        # base_config_qn format: "Project/ConfigName"
        ext_config_name = ext_config_qn.split("/")[-1]  # "ConfigName$ext$ExtName"
        base_config_name = base_config_qn.split("/")[-1]  # "ConfigName"

        # Process each node type
        node_types = [
            "MetadataObject",
            "Attribute",
            "TabularPart",
            "Dimension",
            "Resource",
            "Layout",
            "Command",
            "EnumValue",
            "Form",
            # FormAttribute handled by build_adopted_from_for_formattributes (QN + ext_source + own marking)
            # FormControl handled by build_adopted_from_for_formcontrols (ctrl_id matching)
            # FormEvent handled by build_adopted_from_for_formevents (form-level QN + control ADOPTED_FROM)
            # PredefinedItem loaded after this call — handled separately after load_predefined
            "Characteristic",
            "AccountingFlag",
            "DimensionAccountingFlag",
            "UrlTemplate",
            "UrlMethod",
            "JournalGraph",
        ]

        for node_type in node_types:
            try:
                count = self._build_adopted_from_for_type(
                    node_type,
                    ext_config_name,
                    base_config_name
                )
                stats[node_type] = count

                if count > 0:
                    logger.info("[ADOPTED_FROM]   ✓ %s: %d created", node_type, count)

            except Exception as e:
                logger.error("[ADOPTED_FROM]   ✗ %s: failed - %s", node_type, str(e))
                stats[node_type] = 0

        total = sum(stats.values())
        logger.info("[ADOPTED_FROM] Total created: %d across %d node types", total, len(node_types))

        return stats

    # 15 labels metadata-level builder обходит в full режиме. Scoped builder
    # использует тот же список — иначе scoped path не эквивалентен full builder.
    _SCOPED_NODE_TYPES = (
        "MetadataObject",
        "Attribute",
        "TabularPart",
        "Dimension",
        "Resource",
        "Layout",
        "Command",
        "EnumValue",
        "Form",
        "Characteristic",
        "AccountingFlag",
        "DimensionAccountingFlag",
        "UrlTemplate",
        "UrlMethod",
        "JournalGraph",
    )

    def build_adopted_from_for_qns(
        self,
        ext_config_qn: str,
        base_config_qn: str,
        *,
        exact_qns_by_label: Dict[str, List[str]],
        prefix_qns: List[str],
    ) -> Dict[str, int]:
        """Scoped ADOPTED_FROM refresh: только для exact QN и prefix-subtree.

        Параметры:
        - `exact_qns_by_label[label]` — list точечных QN extension-узлов.
        - `prefix_qns` — плоский label-agnostic список prefix-ов (без trailing '/').
          Для каждого prefix builder обходит ВСЕ 15 metadata-level labels с условием
          `(n.qualified_name = p OR n.qualified_name STARTS WITH p + '/')`.
          Это покрывает И parent-узел prefix-а (TabularPart, UrlTemplate, MetadataObject
          для added/adopted object), И все его metadata-level descendants.

        Для label `MetadataObject` обязательно сохраняется exclude_clause:
        EventSubscription (`category_name = 'ПодпискиНаСобытия'`) строится отдельным
        dedicated path-ом `build_adopted_from_for_eventsubscriptions` — generic builder
        её не должен трогать ни в full, ни в scoped режиме.

        DELETE-then-MERGE гарантирует, что stale-edges от уже удалённых базовых
        объектов очищаются перед пересозданием. Идемпотентно: повторный запуск
        на тех же impacts даёт identical конечное состояние.
        """
        if not exact_qns_by_label and not prefix_qns:
            return {}

        stats: Dict[str, int] = {}
        # Никаких per-label try/except — scoped builder обязан fail-fast.
        # Внешний `_refresh_extension_links_scoped` ловит исключение и делает
        # fallback на full `_refresh_extension_links`. Если глотать exception
        # здесь, fallback contract никогда не сработает.
        for label in self._SCOPED_NODE_TYPES:
            exact_list = list(exact_qns_by_label.get(label) or [])
            if not exact_list and not prefix_qns:
                continue
            created = self._scoped_adopted_from_for_label(
                label=label,
                ext_config_qn=ext_config_qn,
                base_config_qn=base_config_qn,
                exact_qns=exact_list,
                prefix_qns=prefix_qns,
            )
            if created:
                stats[label] = created

        total = sum(stats.values())
        if total or prefix_qns or any(exact_qns_by_label.values()):
            exact_n = sum(len(v) for v in exact_qns_by_label.values())
            created_str = ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
            logger.info(
                "ADOPTED_FROM scoped refresh: ext=%s exact=%d prefix=%d%s",
                ext_config_qn,
                exact_n,
                len(prefix_qns),
                f" created: {created_str}" if created_str else "",
            )
        return stats

    def _scoped_adopted_from_for_label(
        self,
        *,
        label: str,
        ext_config_qn: str,
        base_config_qn: str,
        exact_qns: List[str],
        prefix_qns: List[str],
    ) -> int:
        """Один label: DELETE существующих ADOPTED_FROM этих узлов, затем MERGE новых.

        Cypher с двумя ветвями WHERE (exact IN или prefix match), фильтр
        `ПринадлежностьОбъекта`, exclude_clause для MetadataObject.
        """
        ext_config_name = ext_config_qn.split("/")[-1]
        base_config_name = base_config_qn.split("/")[-1]

        exclude_clause = (
            "AND (n.category_name <> 'ПодпискиНаСобытия' OR n.category_name IS NULL)"
            if label == "MetadataObject"
            else ""
        )

        delete_query = f"""
        MATCH (n:{label})-[r:ADOPTED_FROM]->()
        WHERE n.project_name = $project_name
          AND n.config_name = $ext_config_name
          AND (
                n.qualified_name IN $exact
             OR any(p IN $prefixes WHERE n.qualified_name = p
                                        OR n.qualified_name STARTS WITH p + '/')
          )
          {exclude_clause}
        DELETE r
        """

        merge_query = f"""
        MATCH (n:{label})
        WHERE n.project_name = $project_name
          AND n.config_name = $ext_config_name
          AND (
                n.qualified_name IN $exact
             OR any(p IN $prefixes WHERE n.qualified_name = p
                                        OR n.qualified_name STARTS WITH p + '/')
          )
          AND (n.`ПринадлежностьОбъекта` IS NULL
               OR n.`ПринадлежностьОбъекта` <> 'Собственный')
          {exclude_clause}
        WITH n,
             replace(n.qualified_name,
                     '/' + $ext_config_name + '/',
                     '/' + $base_config_name + '/') AS base_qn
        MATCH (base:{label} {{qualified_name: base_qn}})
        MERGE (n)-[:ADOPTED_FROM]->(base)
        RETURN count(*) AS created
        """

        params = dict(
            ext_config_name=ext_config_name,
            base_config_name=base_config_name,
            project_name=self.project_name,
            exact=exact_qns,
            prefixes=prefix_qns,
        )
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            session.run(delete_query, **params)
            rec = session.run(merge_query, **params).single()
        return rec["created"] if rec else 0

    def build_predefineditem_adopted_from_for_owner_qns(
        self,
        ext_config_name: str,
        base_config_name: str,
        owner_qns: List[str],
    ) -> int:
        """Scoped rebuild ADOPTED_FROM для PredefinedItem под указанными owner_qn.

        Используется incremental после Predefined.xml apply — пересобирает только под
        затронутыми owner-ами, без full extension rebuild.

        owner_qns — qualified_name MetadataObject в extension config_name.
        """
        if not owner_qns:
            return 0
        delete_query = """
        UNWIND $owners AS oqn
        MATCH (n:PredefinedItem)-[r:ADOPTED_FROM]->(:PredefinedItem)
        WHERE n.project_name = $project_name
          AND n.config_name = $ext_config_name
          AND n.qualified_name STARTS WITH oqn + '/Predef/'
        DELETE r
        """
        merge_query = """
        UNWIND $owners AS oqn
        MATCH (n:PredefinedItem)
        WHERE n.project_name = $project_name
          AND n.config_name = $ext_config_name
          AND n.qualified_name STARTS WITH oqn + '/Predef/'
          AND (n.`ПринадлежностьОбъекта` IS NULL OR n.`ПринадлежностьОбъекта` <> 'Собственный')
        WITH n, replace(n.qualified_name,
                        '/' + $ext_config_name + '/',
                        '/' + $base_config_name + '/') AS base_qn
        MATCH (base:PredefinedItem {qualified_name: base_qn})
        MERGE (n)-[:ADOPTED_FROM]->(base)
        RETURN count(*) AS created
        """
        params = dict(
            owners=list(owner_qns),
            ext_config_name=ext_config_name,
            base_config_name=base_config_name,
            project_name=self.project_name,
        )
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            session.run(delete_query, **params)
            rec = session.run(merge_query, **params).single()
        return rec["created"] if rec else 0

    def _build_adopted_from_for_type(
        self,
        node_type: str,
        ext_config_name: str,
        base_config_name: str
    ) -> int:
        """
        Build ADOPTED_FROM for a specific node type via a single Cypher query.
        Eliminates Python round-trip: read → build base_qn → write is now one query.
        """
        exclude_clause = (
            "AND (n.category_name <> 'ПодпискиНаСобытия' OR n.category_name IS NULL)"
            if node_type == "MetadataObject" else ""
        )
        query = f"""
        MATCH (n:{node_type})
        WHERE n.config_name = $ext_config_name
          AND n.project_name = $project_name
          AND (n.`ПринадлежностьОбъекта` IS NULL OR n.`ПринадлежностьОбъекта` <> 'Собственный')
          {exclude_clause}
        WITH n,
             replace(n.qualified_name,
                     '/' + $ext_config_name + '/',
                     '/' + $base_config_name + '/') AS base_qn
        MATCH (base:{node_type} {{qualified_name: base_qn}})
        MERGE (n)-[:ADOPTED_FROM]->(base)
        RETURN count(*) AS created
        """
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            rec = session.run(
                query,
                ext_config_name=ext_config_name,
                base_config_name=base_config_name,
                project_name=self.project_name,
            ).single()
        return rec["created"] if rec else 0


    def build_adopted_from_for_eventsubscriptions(
        self,
        ext_config_qn: str,
        base_config_qn: str,
    ) -> int:
        """Build ADOPTED_FROM for extension EventSubscription MetadataObject nodes."""
        ext_config_name = ext_config_qn.split("/")[-1]
        base_config_name = base_config_qn.split("/")[-1]

        query = """
        MATCH (es:MetadataObject)
        WHERE es.config_name = $ext_config_name
          AND es.project_name = $project_name
          AND es.category_name = 'ПодпискиНаСобытия'
          AND es.`ПринадлежностьОбъекта` IN ['Adopted', 'Заимствованный']
        WITH es,
             replace(es.qualified_name,
                     '/' + $ext_config_name + '/',
                     '/' + $base_config_name + '/') AS base_qn
        MATCH (base:MetadataObject {qualified_name: base_qn})
        MERGE (es)-[:ADOPTED_FROM]->(base)
        RETURN count(*) AS created
        """
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            rec = session.run(
                query,
                ext_config_name=ext_config_name,
                base_config_name=base_config_name,
                project_name=self.project_name,
            ).single()
        return rec["created"] if rec else 0

    def build_adopted_from_for_formcontrols(
        self,
        ext_config_name: str,
        base_config_name: str,
        form_pairs: Optional[List[Any]] = None,
    ) -> int:
        """Build ADOPTED_FROM from extension FormControls to base FormControls (Form + CommonForms).

        Если `form_pairs` передан (List[FormRebuildPair]) — scope ограничивается
        этими формами. None сохраняет config-level поведение (для full-load).
        """
        from .cypher_templates import (
            CYPHER_ADOPTED_FROM_FORMCONTROL,
            CYPHER_ADOPTED_FROM_FORMCONTROL_COMMONFORM,
        )
        scope_ext_qns, scope_ext_owner_qns, scoped = _split_form_pairs(form_pairs)
        run_regular = (not scoped) or bool(scope_ext_qns)
        run_common = (not scoped) or bool(scope_ext_owner_qns)
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            created = 0
            if run_regular:
                rec = session.run(
                    CYPHER_ADOPTED_FROM_FORMCONTROL,
                    ext_config_name=ext_config_name,
                    base_config_name=base_config_name,
                    project_name=self.project_name,
                    scope_ext_qns=scope_ext_qns,
                ).single()
                created += rec["created"] if rec else 0
            if run_common:
                rec_cf = session.run(
                    CYPHER_ADOPTED_FROM_FORMCONTROL_COMMONFORM,
                    ext_config_name=ext_config_name,
                    base_config_name=base_config_name,
                    project_name=self.project_name,
                    scope_ext_owner_qns=scope_ext_owner_qns,
                ).single()
                created += rec_cf["created"] if rec_cf else 0

        return created

    def build_adopted_from_for_formattributes(
        self,
        ext_config_name: str,
        base_config_name: str,
        form_pairs: Optional[List[Any]] = None,
    ) -> int:
        """
        Build ADOPTED_FROM from extension FormAttributes to base FormAttributes,
        set ext_source on matched attributes, then mark remaining as 'own'.
        Covers both regular Form-based forms and CommonForms (via MetadataObject).
        Если `form_pairs` передан — scope ограничивается этими формами **во всех**
        Cypher passes (включая MARK_OWN_* и MODIFIED_PROPS), иначе secondary
        passes вернули бы config-level amplification через back door.
        """
        from .cypher_templates import (
            CYPHER_ADOPTED_FROM_FORMATTRIBUTE,
            CYPHER_MARK_OWN_FORMATTRIBUTES,
            CYPHER_SET_FORMATTRIBUTE_MODIFIED_PROPS,
            CYPHER_ADOPTED_FROM_FORMATTRIBUTE_COMMONFORM,
            CYPHER_MARK_OWN_FORMATTRIBUTES_COMMONFORM,
            CYPHER_SET_FORMATTRIBUTE_MODIFIED_PROPS_COMMONFORM,
        )
        scope_ext_qns, scope_ext_owner_qns, scoped = _split_form_pairs(form_pairs)
        run_regular = (not scoped) or bool(scope_ext_qns)
        run_common = (not scoped) or bool(scope_ext_owner_qns)
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            created = 0
            if run_regular:
                created += (session.run(
                    CYPHER_ADOPTED_FROM_FORMATTRIBUTE,
                    ext_config_name=ext_config_name,
                    base_config_name=base_config_name,
                    project_name=self.project_name,
                    scope_ext_qns=scope_ext_qns,
                ).single() or {}).get("created", 0)
                session.run(
                    CYPHER_MARK_OWN_FORMATTRIBUTES,
                    ext_config_name=ext_config_name,
                    project_name=self.project_name,
                    scope_ext_qns=scope_ext_qns,
                )
                session.run(
                    CYPHER_SET_FORMATTRIBUTE_MODIFIED_PROPS,
                    project_name=self.project_name,
                    scope_ext_qns=scope_ext_qns,
                )

            # CommonForms variant (MetadataObject as form container)
            if run_common:
                cf_created = (session.run(
                    CYPHER_ADOPTED_FROM_FORMATTRIBUTE_COMMONFORM,
                    ext_config_name=ext_config_name,
                    base_config_name=base_config_name,
                    project_name=self.project_name,
                    scope_ext_owner_qns=scope_ext_owner_qns,
                ).single() or {}).get("created", 0)
                created += cf_created
                session.run(
                    CYPHER_MARK_OWN_FORMATTRIBUTES_COMMONFORM,
                    ext_config_name=ext_config_name,
                    project_name=self.project_name,
                    scope_ext_owner_qns=scope_ext_owner_qns,
                )
                session.run(
                    CYPHER_SET_FORMATTRIBUTE_MODIFIED_PROPS_COMMONFORM,
                    project_name=self.project_name,
                    scope_ext_owner_qns=scope_ext_owner_qns,
                )

        return created

    def build_adopted_from_for_formcommands(
        self,
        ext_config_name: str,
        base_config_name: str,
        form_pairs: Optional[List[Any]] = None,
    ) -> int:
        """Build ADOPTED_FROM from extension form Commands to base form Commands, then mark remaining as own.
        Covers both regular Form-based forms and CommonForms (via MetadataObject)."""
        from .cypher_templates import (
            CYPHER_ADOPTED_FROM_FORM_COMMAND,
            CYPHER_MARK_OWN_FORM_COMMANDS,
            CYPHER_ADOPTED_FROM_FORM_COMMAND_COMMONFORM,
            CYPHER_MARK_OWN_FORM_COMMANDS_COMMONFORM,
        )
        scope_ext_qns, scope_ext_owner_qns, scoped = _split_form_pairs(form_pairs)
        run_regular = (not scoped) or bool(scope_ext_qns)
        run_common = (not scoped) or bool(scope_ext_owner_qns)
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            created = 0
            if run_regular:
                created += (session.run(
                    CYPHER_ADOPTED_FROM_FORM_COMMAND,
                    ext_config_name=ext_config_name,
                    base_config_name=base_config_name,
                    project_name=self.project_name,
                    scope_ext_qns=scope_ext_qns,
                ).single() or {}).get("created", 0)
                session.run(
                    CYPHER_MARK_OWN_FORM_COMMANDS,
                    ext_config_name=ext_config_name,
                    project_name=self.project_name,
                    scope_ext_qns=scope_ext_qns,
                )

            # CommonForms variant
            if run_common:
                cf_created = (session.run(
                    CYPHER_ADOPTED_FROM_FORM_COMMAND_COMMONFORM,
                    ext_config_name=ext_config_name,
                    base_config_name=base_config_name,
                    project_name=self.project_name,
                    scope_ext_owner_qns=scope_ext_owner_qns,
                ).single() or {}).get("created", 0)
                created += cf_created
                session.run(
                    CYPHER_MARK_OWN_FORM_COMMANDS_COMMONFORM,
                    ext_config_name=ext_config_name,
                    project_name=self.project_name,
                    scope_ext_owner_qns=scope_ext_owner_qns,
                )

        return created

    def build_adopted_from_for_formevents(
        self,
        ext_config_name: str,
        base_config_name: str,
        form_pairs: Optional[List[Any]] = None,
    ) -> int:
        """Build ADOPTED_FROM from extension FormEvents to base FormEvents.
        Covers form-level, CommonForm-level, and control-level events.
        """
        from .cypher_templates import (
            CYPHER_ADOPTED_FROM_FORMEVENT,
            CYPHER_ADOPTED_FROM_FORMEVENT_COMMONFORM,
            CYPHER_ADOPTED_FROM_FORMEVENT_CONTROL,
            CYPHER_ADOPTED_FROM_FORMEVENT_CONTROL_COMMONFORM,
        )
        scope_ext_qns, scope_ext_owner_qns, scoped = _split_form_pairs(form_pairs)
        run_regular = (not scoped) or bool(scope_ext_qns)
        run_common = (not scoped) or bool(scope_ext_owner_qns)
        regular_params = dict(
            ext_config_name=ext_config_name,
            base_config_name=base_config_name,
            project_name=self.project_name,
            scope_ext_qns=scope_ext_qns,
        )
        # CONTROL вариант не принимает base_config_name (ADOPTED_FROM строится
        # через control link, не через config replacement). Передаём только
        # ext_config_name + project_name + scope_ext_qns.
        regular_ctrl_params = dict(
            ext_config_name=ext_config_name,
            project_name=self.project_name,
            scope_ext_qns=scope_ext_qns,
        )
        common_params = dict(
            ext_config_name=ext_config_name,
            base_config_name=base_config_name,
            project_name=self.project_name,
            scope_ext_owner_qns=scope_ext_owner_qns,
        )
        common_ctrl_params = dict(
            ext_config_name=ext_config_name,
            project_name=self.project_name,
            scope_ext_owner_qns=scope_ext_owner_qns,
        )
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            created = 0
            if run_regular:
                created += (session.run(CYPHER_ADOPTED_FROM_FORMEVENT, **regular_params).single() or {}).get("created", 0)
                created += (session.run(CYPHER_ADOPTED_FROM_FORMEVENT_CONTROL, **regular_ctrl_params).single() or {}).get("created", 0)
            if run_common:
                created += (session.run(CYPHER_ADOPTED_FROM_FORMEVENT_COMMONFORM, **common_params).single() or {}).get("created", 0)
                created += (session.run(CYPHER_ADOPTED_FROM_FORMEVENT_CONTROL_COMMONFORM, **common_ctrl_params).single() or {}).get("created", 0)
        return created

    def build_extends_action_for_formevent_actions(
        self,
        ext_config_name: str,
        base_config_name: str,
        form_pairs: Optional[List[Any]] = None,
    ) -> int:
        """Build EXTENDS_ACTION from extension FormEventActions to base FormEventActions.
        Covers form-level, CommonForm-level, and control-level event actions.
        """
        from .cypher_templates import (
            CYPHER_EXTENDS_ACTION_FORMEVENT,
            CYPHER_EXTENDS_ACTION_FORMEVENT_COMMONFORM,
            CYPHER_EXTENDS_ACTION_FORMEVENT_CONTROL,
            CYPHER_EXTENDS_ACTION_FORMEVENT_CONTROL_COMMONFORM,
        )
        scope_ext_qns, scope_ext_owner_qns, scoped = _split_form_pairs(form_pairs)
        run_regular = (not scoped) or bool(scope_ext_qns)
        run_common = (not scoped) or bool(scope_ext_owner_qns)
        # EXTENDS_ACTION templates не используют base_config_name — связи строятся
        # через event ADOPTED_FROM. Передаём только нужные параметры.
        regular_params = dict(
            ext_config_name=ext_config_name,
            project_name=self.project_name,
            scope_ext_qns=scope_ext_qns,
        )
        common_params = dict(
            ext_config_name=ext_config_name,
            project_name=self.project_name,
            scope_ext_owner_qns=scope_ext_owner_qns,
        )
        with self.loader.driver.session(database=settings.neo4j_database) as session:
            created = 0
            if run_regular:
                created += (session.run(CYPHER_EXTENDS_ACTION_FORMEVENT, **regular_params).single() or {}).get("created", 0)
                created += (session.run(CYPHER_EXTENDS_ACTION_FORMEVENT_CONTROL, **regular_params).single() or {}).get("created", 0)
            if run_common:
                created += (session.run(CYPHER_EXTENDS_ACTION_FORMEVENT_COMMONFORM, **common_params).single() or {}).get("created", 0)
                created += (session.run(CYPHER_EXTENDS_ACTION_FORMEVENT_CONTROL_COMMONFORM, **common_params).single() or {}).get("created", 0)
            return created


def _split_form_pairs(form_pairs: Optional[List[Any]]) -> tuple:
    """Разделить FormRebuildPair[] на (scope_ext_qns, scope_ext_owner_qns, scoped).

    - `form_pairs=None` → ([], [], False) → full-load (config-level).
    - `form_pairs=[regular_only]` → (qns, [], True) → запустить только regular passes.
    - `form_pairs=[common_only]` → ([], owner_qns, True) → запустить только common passes.
    - `form_pairs=[mixed]` → (qns, owner_qns, True) → обе ветки scoped.

    `scoped=True` сигнализирует caller-у, что пустой scope-список одного типа
    означает «у этой rebuild нет форм этого типа», а не «full-load filter off».
    Без этого флага common-form templates применяли бы `size($scope)=0` как
    "no filter" и трогали бы unrelated forms другого типа в extension config.
    """
    if not form_pairs:
        return [], [], False
    scope_ext_qns: List[str] = []
    scope_ext_owner_qns: List[str] = []
    for p in form_pairs:
        is_common = getattr(p, "is_common_form", False)
        if is_common:
            owner = getattr(p, "ext_form_owner_qn", None)
            if owner:
                scope_ext_owner_qns.append(owner)
        else:
            ext_qn = getattr(p, "ext_form_qn", None)
            if ext_qn:
                scope_ext_qns.append(ext_qn)
    return scope_ext_qns, scope_ext_owner_qns, True
