"""
PredefinedLoaderMixin: loads Predefined.xml parsed items and hierarchy into Neo4j.

Ports logic from the monolithic loader:
- load_predefined(items, relations, project_name, config_name)
"""
from __future__ import annotations

from typing import Any, Dict, List
import logging

from config import settings
from .console_search import build_console_search
from .cypher_templates import (
    CYPHER_PREDEFINED_UPSERT_ITEM,
    CYPHER_PREDEFINED_LINK_CHILD,
)

logger = logging.getLogger(__name__)


class PredefinedLoaderMixin:
    def load_predefined(self, items: List[Dict[str, Any]], relations: List[Dict[str, Any]], project_name: str, config_name: str) -> None:
        """Load Predefined.xml parsed items into Neo4j as PredefinedItem nodes and hierarchy edges."""
        if not items and not relations:
            logger.info("No predefined items to load")
            return

        # Prepare item rows
        predef_rows: List[Dict[str, Any]] = []
        for it in items or []:
            try:
                cat = it.get('category_name')
                obj = it.get('object_name')
                local_id = it.get('local_id') or ''
                props = it.get('properties') or {}
                if not cat or not obj or not local_id:
                    continue
                obj_qn = f"{project_name}/{config_name}/{cat}/{obj}"
                predef_qn = f"{obj_qn}/Predef/{local_id}"
                # Predefined name lives in `Имя` property; fall back to local_id like
                # the upsert template does (cypher_templates.py CYPHER_PREDEFINED_UPSERT_ITEM).
                predef_name = props.get('Имя') or local_id
                merged_props = dict(props)
                merged_props.update(build_console_search(predef_name, props, 'predefined'))
                predef_rows.append({
                    'obj_qn': obj_qn,
                    'predef_qn': predef_qn,
                    'category_name': cat,
                    'object_name': obj,
                    'project_name': project_name,
                    'config_name': config_name,
                    'properties': merged_props,
                })
            except Exception as e:
                logger.warning("Skip invalid predefined item: %s", e)

        # Prepare relation rows (HAS_CHILD for ChartOfAccounts hierarchy)
        rel_rows: List[Dict[str, Any]] = []
        for rl in relations or []:
            try:
                cat = rl.get('category_name')
                obj = rl.get('object_name')
                parent_local = rl.get('parent_local_id')
                local_id = rl.get('local_id')
                if not cat or not obj or not parent_local or not local_id:
                    continue
                rel_rows.append({
                    'category_name': cat,
                    'object_name': obj,
                    'project_name': project_name,
                    'config_name': config_name,
                    'parent_local_id': parent_local,
                    'local_id': local_id,
                })
            except Exception as e:
                logger.warning("Skip invalid predefined relation: %s", e)

        bs = settings.neo4j_batch_size
        items_chunks_total = (len(predef_rows) + bs - 1) // bs if predef_rows else 0
        rel_chunks_total = (len(rel_rows) + bs - 1) // bs if rel_rows else 0
        logger.info(
            "Loading Predefined: items=%d, relations=%d, batch=%d, chunks(items=%d, relations=%d)",
            len(predef_rows), len(rel_rows), bs, items_chunks_total, rel_chunks_total
        )

        def _tx_items(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_PREDEFINED_UPSERT_ITEM, rows=rows_chunk)

        def _tx_rels(tx, rows_chunk: List[Dict[str, Any]]):
            tx.run(CYPHER_PREDEFINED_LINK_CHILD, rows=rows_chunk)

        with self.driver.session(database=settings.neo4j_database) as session:
            if predef_rows:
                i = 0
                for chunk in self._chunked(predef_rows, bs):
                    i += 1
                    logger.info("Predefined items chunk %d/%d (size=%d)", i, items_chunks_total, len(chunk))
                    self._write(session, _tx_items, chunk)
            if rel_rows:
                i = 0
                for chunk in self._chunked(rel_rows, bs):
                    i += 1
                    logger.info("Predefined relations chunk %d/%d (size=%d)", i, rel_chunks_total, len(chunk))
                    self._write(session, _tx_rels, chunk)

        logger.info("Loaded predefined items: %d, relations: %d", len(items or []), len(relations or []))