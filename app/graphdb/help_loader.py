"""
HelpLoaderMixin: loads Help/ru.html text content into MetadataObject nodes.
"""
from __future__ import annotations

from typing import Dict, Tuple
import logging

from config import settings
from .cypher_templates import CYPHER_HELP_UPDATE_OBJECT
from xcf_utils import ru_category_from_folder, compute_obj_qn

logger = logging.getLogger(__name__)

# Category mapping is centralized in xcf_utils.ru_category_from_folder


class HelpLoaderMixin:
    def load_help_content(
        self,
        help_by_object: Dict[Tuple[str, str], str],
        project_name: str,
        config_name: str,
    ) -> None:
        """
        Load help content (Справка) for metadata objects.

        Args:
            help_by_object: Dict mapping (category_folder, object_name) -> help_text
            project_name: Project name
            config_name: Configuration name
        """
        if not help_by_object:
            logger.info("No help content to load")
            return

        # Build update rows
        update_rows = []
        for (cat_folder, obj_name), help_text in help_by_object.items():
            # Map folder name to RU category name using shared utility
            category_ru = ru_category_from_folder(cat_folder)

            # Build qualified_name for the MetadataObject using canonical builder
            obj_qn = compute_obj_qn(project_name, config_name, category_ru, obj_name)

            update_rows.append({
                "obj_qn": obj_qn,
                "help_content": help_text,
            })

        if not update_rows:
            logger.warning("No valid help content rows to load after mapping")
            return

        bs = settings.neo4j_batch_size
        total_chunks = (len(update_rows) + bs - 1) // bs
        logger.info(
            "Loading help content: objects=%d, batch=%d, chunks=%d",
            len(update_rows),
            bs,
            total_chunks,
        )

        def _tx_help(tx, rows_chunk):
            tx.run(CYPHER_HELP_UPDATE_OBJECT, rows=rows_chunk)

        with self.driver.session(database=settings.neo4j_database) as session:
            i = 0
            for chunk in self._chunked(update_rows, bs):
                i += 1
                logger.info("Help content chunk %d/%d (size=%d)", i, total_chunks, len(chunk))
                self._write(session, _tx_help, chunk)

        logger.info("Loaded help content for %d metadata objects", len(update_rows))
