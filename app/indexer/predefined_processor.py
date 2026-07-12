"""
Processor for Predefined.xml files.

Handles predefined values for catalogs, charts of accounts, etc.
"""

from pathlib import Path
from typing import List, Dict, Any
import logging

from .data_structures import PredefinedData
from parsers.predefined_parser import PredefinedParser

logger = logging.getLogger(__name__)


class PredefinedProcessor:
    """Processes Predefined.xml files"""

    def __init__(self):
        """Initialize the predefined processor"""
        self.parser = PredefinedParser()

    def process_predefined_files(
        self,
        code_root: Path,
        discovered_count: int = 0
    ) -> PredefinedData:
        """
        Process all Predefined.xml files.

        Note: Processing is handled during the streaming scan.
        This method is kept for compatibility and future use.

        Args:
            code_root: Root directory containing code
            discovered_count: Number of files already discovered

        Returns:
            PredefinedData with items and relations
        """
        logger.info("Predefined files were discovered during scan, processing results...")

        predef_data = PredefinedData()
        return predef_data

    def merge_result(
        self,
        predef_data: PredefinedData,
        items: List[Dict[str, Any]],
        relations: List[Dict[str, Any]]
    ):
        """
        Merge parsing results into PredefinedData.

        Args:
            predef_data: PredefinedData to merge into
            items: Predefined items to add
            relations: Relations to add
        """
        if items:
            predef_data.items.extend(items)
        if relations:
            predef_data.relations.extend(relations)
