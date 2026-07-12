"""
Indexer facade - simple entry point that delegates to orchestrator.

This provides backward compatibility with the original Indexer interface.
"""

from pathlib import Path
from typing import Optional

from .indexing_result import IndexingResult
from .orchestrator import IndexerOrchestrator


class Indexer:
    """
    Main indexer facade - provides simple API for indexing operations.

    This is the main entry point for external code. It delegates all work
    to IndexerOrchestrator.
    """

    def __init__(self):
        """Initialize the indexer"""
        self.orchestrator = IndexerOrchestrator()

    def index_metadata(
        self, directory: Optional[Path] = None, clear_db: bool = False
    ) -> IndexingResult:
        """
        Parse metadata files and load them into Neo4j.

        Args:
            directory: Directory containing metadata files (uses config default if None)
            clear_db: Whether to clear the database before loading

        Returns:
            IndexingResult — dataclass with success flag, configurations, code_index,
            metadata_source, metadata_dir. Implements __bool__ → self.success so that
            existing `if success:` call sites continue to work.
        """
        return self.orchestrator.run_indexing(directory, clear_db)

    def verify_connection(self) -> bool:
        """
        Verify Neo4j connection.

        Returns:
            True if connection is successful, False otherwise
        """
        return self.orchestrator.verify_connection()

    def display_statistics(self):
        """Display statistics about the loaded data"""
        self.orchestrator.display_statistics()
