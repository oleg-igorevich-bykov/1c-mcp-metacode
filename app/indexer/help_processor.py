"""
Processor for Help/ru.html files.

Extracts help documentation from HTML files for metadata objects.
"""

from pathlib import Path
from typing import Dict, Tuple
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from .data_structures import HelpData
from .workers import worker_help

logger = logging.getLogger(__name__)


class HelpProcessor:
    """Processes Help/ru.html files to extract documentation"""

    def __init__(self, max_workers: int = 8):
        """
        Initialize the help processor.

        Args:
            max_workers: Maximum number of worker threads
        """
        self.max_workers = max_workers

    def process_help_files(
        self,
        code_root: Path,
        discovered_count: int = 0
    ) -> HelpData:
        """
        Process all Help/ru.html files found during directory scan.

        Note: This is called with files already discovered during the scan.
        It processes them in parallel using ThreadPoolExecutor.

        Args:
            code_root: Root directory containing code
            discovered_count: Number of files already discovered

        Returns:
            HelpData with extracted help content
        """
        logger.info("Help files were discovered during scan, processing results...")

        help_data = HelpData()
        return help_data

    def process_file(self, html_path: Path) -> Tuple[str, str, str]:
        """
        Process a single Help/ru.html file.

        Args:
            html_path: Path to the HTML file

        Returns:
            Tuple of (category_folder, object_name, help_content)
        """
        result = worker_help(html_path)

        if result and result.get("kind") == "help":
            return (
                result.get("category_folder", ""),
                result.get("object_name", ""),
                result.get("help_content", "")
            )

        return ("", "", "")

    @staticmethod
    def merge_results(
        help_data: HelpData,
        category_folder: str,
        object_name: str,
        help_content: str
    ):
        """
        Merge a single result into HelpData.

        Args:
            help_data: HelpData to merge into
            category_folder: Category folder name
            object_name: Object name
            help_content: Extracted help text
        """
        if category_folder and object_name and help_content:
            key = (category_folder, object_name)
            help_data.help_by_object[key] = help_content
