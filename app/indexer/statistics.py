"""
Statistics collection and display for indexing process.
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


class IndexingStatistics:
    """Tracks and displays statistics for the indexing process"""

    def __init__(self, loader):
        """
        Initialize statistics tracker.

        Args:
            loader: Neo4jLoader instance
        """
        self.loader = loader

    def display_statistics(self, settings):
        """
        Display statistics about the loaded data from Neo4j.

        Args:
            settings: Settings object with project_name
        """
        try:
            stats = self.loader.get_statistics()
            logger.info("Database Statistics (project: %s):", settings.project_name)
            logger.info("=" * 80)
            for node_type, count in stats.items():
                logger.info("  %-20s : %6d", node_type, count)
            logger.info("=" * 80)
        except Exception as e:
            logger.error("Could not retrieve statistics: %s", str(e))
