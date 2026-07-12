"""
SslApiMarkerMixin: маркировка Routine.is_ssl_api для full и incremental.

Перенесён сюда из app/indexer/bsl_processor.py.mark_ssl_api_routines, чтобы:
- сохранить direction `indexer/incremental -> graphdb`;
- incremental мог переиспользовать тот же helper без захода в indexer;
- логика collect/clear/mark была централизована.

API:
- _collect_ssl_owners(session, project_name) -> Set[str]
- refresh_ssl_api_for_routines(session, project_name, routine_ids) -> int   # scoped, three-state
- refresh_ssl_api_for_project(session, project_name) -> int                 # full, three-state
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Set

from .cypher_templates import CYPHER_MARK_SSL_API_ROUTINES_BY_OWNERS

logger = logging.getLogger(__name__)


class SslApiMarkerMixin:
    """Mixin для маркировки Routine.is_ssl_api на основе подсистем СтандартныеПодсистемы."""

    def _collect_ssl_owners(self, session, project_name: str) -> Set[str]:
        """Собрать owner_qn-ы объектов из СтандартныеПодсистемы.

        Эта логика дословно перенесена из bsl_processor.mark_ssl_api_routines, чтобы
        и full-load (через thin wrapper) и incremental получили один path.
        """
        from graphdb.types import SINGULAR_TO_PLURAL

        owners_qn_set: Set[str] = set()

        subs_query = """
        MATCH (ss:MetadataObject {project_name: $project_name, category_name: 'Подсистемы'})
        WHERE ss.`ПутьПодсистемы` IS NOT NULL AND 'СтандартныеПодсистемы' IN ss.`ПутьПодсистемы`
        RETURN ss.`Состав` AS comp
        """

        res = session.run(subs_query, project_name=project_name)
        lookup_rows: List[dict] = []
        if res:
            for rec in res:
                comp = rec.get("comp")
                items: List[str] = []
                if isinstance(comp, list):
                    for it in comp:
                        try:
                            s = str(it).strip()
                            if s:
                                items.append(s)
                        except Exception:
                            continue
                else:
                    try:
                        s = str(comp or "").strip()
                        if s.startswith("[") and s.endswith("]"):
                            s = s[1:-1]
                        parts = [p.strip() for p in s.split(",") if p and p.strip()]
                        items.extend(parts)
                    except Exception:
                        pass

                for raw in items:
                    if "." not in raw:
                        continue
                    sing, obj_name = raw.split(".", 1)
                    sing = sing.strip()
                    obj_name = obj_name.strip()
                    if not sing or not obj_name:
                        continue
                    plural_cat = SINGULAR_TO_PLURAL.get(sing, sing)
                    lookup_rows.append({"cat": plural_cat, "name": obj_name})

        if not lookup_rows:
            return owners_qn_set

        try:
            owner_res = session.run(
                """
                UNWIND $rows AS row
                MATCH (owner:MetadataObject {
                    project_name: $project_name,
                    category_name: row.cat,
                    name: row.name
                })
                RETURN owner.qualified_name AS qn
                """,
                rows=lookup_rows,
                project_name=project_name,
            )
            for rec in owner_res:
                qn = rec.get("qn")
                if qn:
                    owners_qn_set.add(str(qn))
        except Exception as e:
            logger.error("Batch owner QN lookup failed: %s", e, exc_info=True)
            raise

        return owners_qn_set

    def refresh_ssl_api_for_project(self, session, project_name: str) -> int:
        """Project-wide SSL refresh: three-state collect-first + transactional clear+mark.

        (a) owner collection failed → не трогаем граф (текущий full-load fail mode).
        (b) owners empty (валидный пересчёт) → clear старых true flags в tx, return 0.
        (c) owners non-empty → одна tx: clear+mark; mark failure откатит clear.
        """
        logger.info("Marking SSL API routines based on Subsystems.'Состав'...")
        try:
            owners = self._collect_ssl_owners(session, project_name)
        except Exception as e:
            logger.error("Failed to mark SSL API routines: %s", e, exc_info=True)
            return 0

        marked = 0
        try:
            with session.begin_transaction() as tx:
                tx.run(
                    """
                    MATCH (r:Routine {project_name: $project_name})
                    WHERE r.is_ssl_api = true
                    SET r.is_ssl_api = false
                    """,
                    project_name=project_name,
                )
                if owners:
                    rec = tx.run(
                        CYPHER_MARK_SSL_API_ROUTINES_BY_OWNERS,
                        owners_qn=list(sorted(owners)),
                        project_name=project_name,
                    ).single()
                    marked = rec["marked_count"] if rec else 0
                tx.commit()
        except Exception as e:
            logger.error("Owner-based SSL API marking failed: %s", e, exc_info=True)
            return 0

        logger.info(
            "Marked SSL API routines: %d (from %d SSL owners)", marked, len(owners)
        )
        return marked

    def refresh_ssl_api_for_routines(
        self, session, project_name: str, routine_ids: Iterable[str]
    ) -> int:
        """Scoped SSL refresh для affected routine ids: same three-state pattern."""
        ids = list(routine_ids)
        if not ids:
            return 0
        try:
            owners = self._collect_ssl_owners(session, project_name)
        except Exception as e:
            logger.error(
                "SSL API refresh (scoped): owner collection failed: %s", e, exc_info=True
            )
            return 0

        marked = 0
        try:
            with session.begin_transaction() as tx:
                tx.run(
                    """
                    UNWIND $ids AS rid
                    MATCH (r:Routine {id: rid, project_name: $project_name})
                    SET r.is_ssl_api = false
                    """,
                    ids=ids,
                    project_name=project_name,
                )
                if owners:
                    rec = tx.run(
                        """
                        UNWIND $ids AS rid
                        MATCH (r:Routine {id: rid, project_name: $project_name})
                        WHERE r.owner_qn IN $owners
                          AND r.area_path IS NOT NULL
                          AND (r.area_path = 'ПрограммныйИнтерфейс'
                               OR r.area_path STARTS WITH 'ПрограммныйИнтерфейс.')
                        SET r.is_ssl_api = true
                        RETURN count(r) AS marked
                        """,
                        ids=ids,
                        owners=list(sorted(owners)),
                        project_name=project_name,
                    ).single()
                    marked = rec["marked"] if rec else 0
                tx.commit()
        except Exception as e:
            logger.error(
                "SSL API refresh (scoped): reset/mark failed: %s", e, exc_info=True
            )
            return 0

        logger.info(
            "SSL API refresh: mode=scoped affected_routines=%d marked=%d",
            len(ids),
            marked,
        )
        return marked
