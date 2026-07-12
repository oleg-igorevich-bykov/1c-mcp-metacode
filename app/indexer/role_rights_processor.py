"""
Processor for Role Rights (Rights.xml files).

Handles parsing of role permissions and rights.
"""

from pathlib import Path
from typing import List, Dict, Any
import logging

from parsers.role_rights_parser import RoleRightsParser

logger = logging.getLogger(__name__)


class RoleRightsProcessor:
    """Processes Rights.xml files for roles"""

    def __init__(self):
        """Initialize the role rights processor"""
        self.parser = RoleRightsParser()

    def process_role_rights(
        self,
        code_root: Path,
        project_name: str,
        cfg_name: str,
        rights_xml_files: List[Path] | None = None,
    ) -> List[Dict[str, Any]]:
        """
        Parse Rights.xml files for roles.

        When ``rights_xml_files`` is provided (new pipeline: list pre-collected
        by CodeFileIndexer), the parser consumes the list directly without
        re-walking the filesystem. Otherwise it falls back to parse_all(),
        which globs Roles/*/Ext/Rights.xml under code_root.
        """
        try:
            if rights_xml_files is not None:
                logger.info(
                    "Parsing Role rights (Rights.xml) from %d pre-collected files",
                    len(rights_xml_files),
                )
                rr_rows = self.parser.parse_files(rights_xml_files, project_name, cfg_name)
            else:
                logger.info("Parsing Role rights (Rights.xml) from %s", code_root)
                rr_rows = self.parser.parse_all(code_root, project_name, cfg_name)

            if rr_rows:
                logger.info("Parsed %d role rights entries", len(rr_rows))
            else:
                logger.info("No Rights.xml entries discovered")

            return rr_rows

        except Exception as e:
            logger.error("Failed to parse role rights: %s", str(e))
            return []
