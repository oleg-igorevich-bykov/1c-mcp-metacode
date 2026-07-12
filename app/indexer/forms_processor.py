"""
Processor for Form.xml and Form.bin files.

Handles parsing of forms, controls, events, attributes, commands, and data bindings.
"""

from pathlib import Path
from typing import Dict, Any, List
import logging

from .data_structures import FormsData
from parsers.form_xml_parser import FormXmlParser

logger = logging.getLogger(__name__)


class FormsProcessor:
    """Processes Form.xml and Form.bin files"""

    def __init__(self):
        """Initialize the forms processor"""
        self.parser = FormXmlParser()

    def process_forms(
        self,
        code_root: Path,
        discovered_count: int = 0
    ) -> FormsData:
        """
        Process all Form.xml files.

        Note: Processing is handled during the streaming scan.
        This method is kept for compatibility.

        Args:
            code_root: Root directory containing code
            discovered_count: Number of files already discovered

        Returns:
            FormsData with all parsed form data
        """
        logger.info("Form files were discovered during scan, processing results...")

        forms_data = FormsData()
        return forms_data

    def merge_form_result(
        self,
        forms_data: FormsData,
        result: Dict[str, Any]
    ):
        """
        Merge a single form parsing result into FormsData.

        Args:
            forms_data: FormsData to merge into
            result: Result dictionary from worker_form_xml
        """
        if not result or result.get("kind") != "form":
            return

        rows = result.get("rows") or {}

        for key in [
            "form_updates", "controls", "root_rel", "child_rel",
            "events", "event_rel", "event_actions", "form_attributes", "form_commands",
            "form_command_usages", "data_bindings"
        ]:
            data = rows.get(key, [])
            if data:
                getattr(forms_data, key).extend(data)


    def has_data(self, forms_data: FormsData) -> bool:
        """
        Check if FormsData contains any data.

        Args:
            forms_data: FormsData to check

        Returns:
            True if any data exists
        """
        return any([
            forms_data.form_updates,
            forms_data.controls,
            forms_data.root_rel,
            forms_data.child_rel,
            forms_data.events,
            forms_data.event_rel,
            forms_data.event_actions,
            forms_data.form_attributes,
            forms_data.form_commands,
            forms_data.form_command_usages,
            forms_data.data_bindings,
        ])
