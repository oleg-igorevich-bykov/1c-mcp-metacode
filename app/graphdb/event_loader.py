"""
EventSubscriptionsLoaderMixin: loads Event Subscriptions and links to metadata objects
and handler routines.
"""
from __future__ import annotations

from typing import Any, Dict, List
import logging

from config import settings
from parsers.event_subscription_parser import EventSubscription
from xcf_utils import normalize_event_name

from .cypher_templates import (
    CYPHER_EVENT_SUBSCRIPTION_UPSERT_NODE,
    CYPHER_EVENT_SUBSCRIPTION_LINK_TO_OBJECT,
    CYPHER_DELETE_EVENT_SUBSCRIPTION_LINKS,
    CYPHER_LINK_AFFECTED_EVENT_SUBSCRIPTIONS_TO_HANDLERS,
)

logger = logging.getLogger(__name__)


class EventSubscriptionPayloadTooLarge(Exception):
    """Raised by `replace_event_subscriptions` если payload не помещается в одну
    transaction. Caller (`_apply_event_subscriptions`) логирует и пропускает
    apply без partial cleanup."""


class EventSubscriptionsLoaderMixin:
    def load_event_subscriptions(self, subscriptions: List[EventSubscription], project_name: str, config_name: str) -> None:
        """
        Load Event Subscriptions into Neo4j with HAS_EVENT_SUBSCRIPTION relationships.

        Args:
            subscriptions: List of parsed EventSubscription objects
            project_name: Name of the project
            config_name: Name of the configuration
        """
        if not subscriptions:
            logger.info("No Event Subscriptions to load")
            return

        logger.info("Loading %d Event Subscriptions for config %s", len(subscriptions), config_name)

        # Подготовка данных для загрузки
        subscription_rows: List[Dict[str, Any]] = []
        relationship_rows: List[Dict[str, Any]] = []

        for sub in subscriptions:
            meta_qn = f"{project_name}/{config_name}/ПодпискиНаСобытия/{sub.name}"

            # Обогащаем существующий MetadataObject-узел подписки
            props = sub.to_dict()
            try:
                ru_event = normalize_event_name(props.get("Событие", ""))
            except Exception:
                ru_event = props.get("Событие", "")
            # Обновим свойство на русский вариант; оригинал сохраним при отличии
            props["Событие"] = ru_event
            if ru_event and ru_event != sub.event:
                props["event_en"] = sub.event

            # Нормализация обработчика: убрать префикс CommonModule.
            handler_val = props.get("Обработчик", "")
            if handler_val:
                hv = handler_val.strip()
                if hv.startswith("CommonModule."):
                    props["handler_prefixed"] = hv
                    props["Обработчик"] = hv[len("CommonModule."):]
            
            # Нормализация Источника: cfg:*.* -> Категория.ИмяОбъекта
            def _normalize_source_token(token: str) -> str:
                s = (token or "").strip()
                try:
                    if s.startswith('cfg:'):
                        parts = s.split('.')
                        if len(parts) >= 2:
                            category = parts[0].replace('cfg:', '').replace('Object', '').replace('Ref', '')
                            object_name = parts[1]
                            category_map = {
                                'Document': 'Документы',
                                'Catalog': 'Справочники',
                                'InformationRegister': 'РегистрыСведений',
                                'AccumulationRegister': 'РегистрыНакопления',
                                'ChartOfAccounts': 'ПланыСчетов',
                                'ChartOfCharacteristicTypes': 'ПланыВидовХарактеристик',
                                'BusinessProcess': 'БизнесПроцессы',
                                'Task': 'Задачи',
                            }
                            category_ru = category_map.get(category, category)
                            return f"{category_ru}.{object_name}"
                    return s
                except Exception:
                    return s

            src_raw = props.get("Источник") or sub.source_objects or []
            norm_src: List[str] = []
            _seen = set()
            for _tok in src_raw:
                _nt = _normalize_source_token(_tok)
                if _nt and _nt not in _seen:
                    _seen.add(_nt)
                    norm_src.append(_nt)
            # Перезапишем свойство на нормализованные значения
            props["Источник"] = norm_src

            subscription_rows.append({
                'meta_qn': meta_qn,
                'project_name': project_name,
                'config_name': config_name,
                'properties': props,
            })

            # Данные для связей с объектами метаданных
            for source_obj in sub.source_objects:
                # Нормализация типа объекта: cfg:DocumentObject.ИнвентаризацияКассы → Документы.ИнвентаризацияКассы
                if source_obj.startswith('cfg:'):
                    parts = source_obj.split('.')
                    if len(parts) >= 2:
                        # Извлечение категории и имени объекта
                        category = parts[0].replace('cfg:', '').replace('Object', '').replace('Ref', '')
                        object_name = parts[1]

                        # Маппинг cfg: типов в русские категории
                        category_map = {
                            'Document': 'Документы',
                            'Catalog': 'Справочники',
                            'InformationRegister': 'РегистрыСведений',
                            'AccumulationRegister': 'РегистрыНакопления',
                            'ChartOfAccounts': 'ПланыСчетов',
                            'ChartOfCharacteristicTypes': 'ПланыВидовХарактеристик',
                            'BusinessProcess': 'БизнесПроцессы',
                            'Task': 'Задачи',
                        }

                        category_ru = category_map.get(category, category)
                        target_qn = f"{project_name}/{config_name}/{category_ru}/{object_name}"

                        relationship_rows.append({
                            'meta_qn': meta_qn,
                            'target_qn': target_qn,
                            'target_name': object_name,
                            'target_category': category_ru,
                            'source_type': source_obj,
                        })

        # Загрузка в Neo4j
        with self.driver.session(database=settings.neo4j_database) as session:
            # Загрузка узлов EventSubscription
            if subscription_rows:
                bs = settings.neo4j_batch_size
                i = 0
                for chunk in self._chunked(subscription_rows, bs):
                    i += 1
                    logger.info("EventSubscription nodes chunk %d (size=%d)", i, len(chunk))
                    self._write(session, lambda tx, rows: tx.run(CYPHER_EVENT_SUBSCRIPTION_UPSERT_NODE, rows=rows), chunk)

            # Загрузка связей HAS_EVENT_SUBSCRIPTION
            if relationship_rows:
                bs = settings.neo4j_batch_size
                i = 0
                for chunk in self._chunked(relationship_rows, bs):
                    i += 1
                    logger.info("HAS_EVENT_SUBSCRIPTION rels chunk %d (size=%d)", i, len(chunk))
                    self._write(session, lambda tx, rows: tx.run(CYPHER_EVENT_SUBSCRIPTION_LINK_TO_OBJECT, rows=rows), chunk)

        logger.info("Loaded %d Event Subscriptions with %d relationships",
                   len(subscription_rows), len(relationship_rows))

    def replace_event_subscriptions(
        self,
        project_name: str,
        config_name: str,
        subscription_qns: List[str],
        subscriptions: List[Any],
    ) -> None:
        """Atomic replace: cleanup + node upsert + HAS_EVENT_SUBSCRIPTION reload +
        scoped handler relink в одной write transaction.

        Caller обязан передать УЖЕ распарсенные subscriptions (parse-first).
        Все Neo4j writes выполняются одной `session.execute_write` — partial-apply
        невозможен: если transaction коммитится, старый USES_HANDLER удалён и
        новый создан (или handler-Routine ещё нет; correctness страховка —
        config-level `link_event_subscriptions_to_handlers` в PostLinking phase 4,
        который запускается после BSL apply).

        Fail-fast при превышении payload (sum subscription_rows + relationship_rows
        > 50_000) с EventSubscriptionPayloadTooLarge — chunked-fallback нарушает
        atomic guarantee и не используется.
        """
        # Build payload outside transaction (parse-first).
        subscription_rows, relationship_rows = self._build_event_subscription_rows(
            subscriptions, project_name, config_name,
        )

        total_rows = len(subscription_qns) + len(subscription_rows) + len(relationship_rows)
        if total_rows > 50_000:
            raise EventSubscriptionPayloadTooLarge(
                f"Payload too large for atomic replace [{config_name}]: {total_rows} rows"
            )

        def _do_replace(tx: Any) -> None:
            if subscription_qns:
                tx.run(
                    CYPHER_DELETE_EVENT_SUBSCRIPTION_LINKS,
                    qns=list(subscription_qns),
                    project_name=project_name,
                    config_name=config_name,
                )
            if subscription_rows:
                tx.run(CYPHER_EVENT_SUBSCRIPTION_UPSERT_NODE, rows=subscription_rows)
            if relationship_rows:
                tx.run(CYPHER_EVENT_SUBSCRIPTION_LINK_TO_OBJECT, rows=relationship_rows)
            # Scoped handler relink — оптимизация (correctness — config-level pass
            # в PostLinking phase 4). При same-cycle Routine introduction новая
            # Routine может ещё не существовать; тогда этот MERGE не создаст
            # ребро, но config-level pass позже доделает.
            if subscription_qns:
                tx.run(
                    CYPHER_LINK_AFFECTED_EVENT_SUBSCRIPTIONS_TO_HANDLERS,
                    qns=list(subscription_qns),
                    project_name=project_name,
                    config_name=config_name,
                )

        with self.driver.session(database=settings.neo4j_database) as session:
            session.execute_write(_do_replace)

        logger.info(
            "replace_event_subscriptions [%s]: cleanup_qns=%d upsert_rows=%d rel_rows=%d",
            config_name, len(subscription_qns), len(subscription_rows), len(relationship_rows),
        )

    def _build_event_subscription_rows(
        self,
        subscriptions: List[Any],
        project_name: str,
        config_name: str,
    ) -> tuple:
        """Подготовить subscription_rows + relationship_rows для atomic replace.

        Логика построения совпадает с `load_event_subscriptions`, но возвращает
        rows вместо немедленной записи в Neo4j.
        """
        subscription_rows: List[Dict[str, Any]] = []
        relationship_rows: List[Dict[str, Any]] = []
        if not subscriptions:
            return subscription_rows, relationship_rows

        for sub in subscriptions:
            meta_qn = f"{project_name}/{config_name}/ПодпискиНаСобытия/{sub.name}"

            props = sub.to_dict()
            try:
                ru_event = normalize_event_name(props.get("Событие", ""))
            except Exception:
                ru_event = props.get("Событие", "")
            props["Событие"] = ru_event
            if ru_event and ru_event != sub.event:
                props["event_en"] = sub.event

            handler_val = props.get("Обработчик", "")
            if handler_val:
                hv = handler_val.strip()
                if hv.startswith("CommonModule."):
                    props["handler_prefixed"] = hv
                    props["Обработчик"] = hv[len("CommonModule."):]

            def _normalize_source_token(token: str) -> str:
                s = (token or "").strip()
                try:
                    if s.startswith('cfg:'):
                        parts = s.split('.')
                        if len(parts) >= 2:
                            category = parts[0].replace('cfg:', '').replace('Object', '').replace('Ref', '')
                            object_name = parts[1]
                            category_map = {
                                'Document': 'Документы',
                                'Catalog': 'Справочники',
                                'InformationRegister': 'РегистрыСведений',
                                'AccumulationRegister': 'РегистрыНакопления',
                                'ChartOfAccounts': 'ПланыСчетов',
                                'ChartOfCharacteristicTypes': 'ПланыВидовХарактеристик',
                                'BusinessProcess': 'БизнесПроцессы',
                                'Task': 'Задачи',
                            }
                            category_ru = category_map.get(category, category)
                            return f"{category_ru}.{object_name}"
                    return s
                except Exception:
                    return s

            src_raw = props.get("Источник") or sub.source_objects or []
            norm_src: List[str] = []
            _seen = set()
            for _tok in src_raw:
                _nt = _normalize_source_token(_tok)
                if _nt and _nt not in _seen:
                    _seen.add(_nt)
                    norm_src.append(_nt)
            props["Источник"] = norm_src

            subscription_rows.append({
                'meta_qn': meta_qn,
                'project_name': project_name,
                'config_name': config_name,
                'properties': props,
            })

            for source_obj in sub.source_objects:
                if source_obj.startswith('cfg:'):
                    parts = source_obj.split('.')
                    if len(parts) >= 2:
                        category = parts[0].replace('cfg:', '').replace('Object', '').replace('Ref', '')
                        object_name = parts[1]
                        category_map = {
                            'Document': 'Документы',
                            'Catalog': 'Справочники',
                            'InformationRegister': 'РегистрыСведений',
                            'AccumulationRegister': 'РегистрыНакопления',
                            'ChartOfAccounts': 'ПланыСчетов',
                            'ChartOfCharacteristicTypes': 'ПланыВидовХарактеристик',
                            'BusinessProcess': 'БизнесПроцессы',
                            'Task': 'Задачи',
                        }
                        category_ru = category_map.get(category, category)
                        target_qn = f"{project_name}/{config_name}/{category_ru}/{object_name}"
                        relationship_rows.append({
                            'meta_qn': meta_qn,
                            'target_qn': target_qn,
                            'target_name': object_name,
                            'target_category': category_ru,
                            'source_type': source_obj,
                        })

        return subscription_rows, relationship_rows

    def link_event_subscriptions_to_handlers(self, project_name: str, config_name: str) -> None:
        """
        Создать связи между подписками на события и процедурами обработчиков.

        Args:
            project_name: Имя проекта
            config_name: Имя конфигурации
        """
        logger.info("Linking Event Subscriptions to their handlers...")

        # Найти все подписки и создать связи с процедурами по имени обработчика
        with self.driver.session(database=settings.neo4j_database) as session:
            result = session.run("""
                MATCH (es:MetadataObject)
                WHERE es.project_name = $project_name
                  AND es.config_name = $config_name
                  AND es.category_name = 'ПодпискиНаСобытия'
                  AND es.`Обработчик` IS NOT NULL
                WITH es, split(es.Обработчик, '.') AS hp
                WITH es,
                     CASE WHEN size(hp) >= 3 AND hp[0] = 'CommonModule' THEN hp[1]
                          WHEN size(hp) >= 2 THEN hp[size(hp) - 2]
                          ELSE '' END AS module_name,
                     CASE WHEN size(hp) >= 1 THEN hp[size(hp) - 1] ELSE '' END AS routine_name
                WHERE module_name <> '' AND routine_name <> ''
                MATCH (r:Routine {name: routine_name, project_name: $project_name, config_name: $config_name})
                WHERE r.file_path CONTAINS module_name
                MERGE (es)-[:USES_HANDLER]->(r)
                RETURN count(*) AS linked_count
            """, project_name=project_name, config_name=config_name)

            linked_count = result.single()["linked_count"]
            logger.info("Created %d links between Event Subscriptions and their handlers", linked_count)