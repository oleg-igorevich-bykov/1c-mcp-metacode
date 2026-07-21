"""
Metadata loader for base 1C configuration metadata.

Supports two sources, selected by settings.metadata_source:
  - "xml": parses code/<...>.xml directly via XmlMetadataParser, consuming a
           ready file list from CodeFileIndex (no extra os.walk inside). Default,
           and the only source supported with settings.project_layout="vanessa".
  - "txt": parses metadata/*.txt via MetadataParser. Only available with
           settings.project_layout="legacy" (see config.py validation).
"""

from pathlib import Path
from typing import List, Dict, Optional
import logging

from parsers.metadata_parser import MetadataParser
from dumpinfo_loader import load_dumpinfo_map

logger = logging.getLogger(__name__)


class MetadataLoader:
    """Loads base metadata from .txt files or code/*.xml descriptors."""

    def __init__(self):
        """Initialize the metadata loader"""
        self.parser = MetadataParser()

    def validate_metadata_directory(self, metadata_dir: Path) -> bool:
        """
        Validate that the metadata directory exists and contains exactly one .txt file.

        Args:
            metadata_dir: Directory to validate

        Returns:
            True if valid, False otherwise
        """
        if not metadata_dir.exists():
            logger.error("Directory does not exist: %s", metadata_dir)
            return False

        # Enforce single .txt file per configuration
        txt_files = list(metadata_dir.glob("*.txt"))
        if not txt_files:
            logger.error("❌ No .txt metadata files found in %s", metadata_dir)
            return False

        if len(txt_files) > 1:
            logger.warning(
                "⚠️ Multiple .txt metadata files found in %s; only one configuration is supported.",
                metadata_dir
            )
            for fp in txt_files:
                logger.warning("   - %s", fp)
            logger.warning("Exiting due to multiple configuration files present.")
            return False

        return True

    def load_configurations(
        self,
        metadata_dir: Path,
        code_index=None,
        source: str = "txt",
        *,
        is_extension: bool = False,
    ) -> List:
        """
        Parse metadata and return Configuration objects.

        Args:
            metadata_dir: Directory containing .txt files (used when source="txt").
            code_index: CodeFileIndex for this code root (required for source="xml").
            source: "txt" | "xml".
            is_extension: When True and source="xml", XmlMetadataParser appends
                "$ext$" to the configuration name and stamps
                ПринадлежностьОбъекта="Собственный" on every node without
                explicit <ObjectBelonging>. For source="txt" the flag is
                ignored — ExtensionsLoader does the rename itself.

        Returns:
            List of Configuration objects (typically of length 1).
        """
        if source == "txt":
            logger.info("Parsing metadata files (txt) from: %s", metadata_dir)
            if not self.validate_metadata_directory(metadata_dir):
                return []
            configurations = self.parser.parse_directory(metadata_dir)
        elif source == "xml":
            if code_index is None or getattr(code_index, "config_xml", None) is None:
                logger.error(
                    "XML mode: Configuration.xml not found in code index for root=%s",
                    getattr(code_index, "root", None),
                )
                return []
            logger.info(
                "Parsing metadata files (xml) from code index: root=%s (%d descriptors)%s",
                code_index.root,
                len(code_index.metadata_xml_files),
                " [extension]" if is_extension else "",
            )
            from xml_metadata import XmlMetadataParser  # local import to avoid mp-spawn cycles
            from config import settings, resolve_xml_standard_attributes_mode

            xml_materialize, xml_preserve = resolve_xml_standard_attributes_mode(
                settings.xml_standard_attributes_mode
            )
            configurations = XmlMetadataParser(
                materialize_standard_attributes=xml_materialize,
                preserve_listed_standard_attributes=xml_preserve,
            ).parse_files(
                code_index.metadata_xml_files,
                code_index.root,
                is_extension=is_extension,
            )
        else:
            logger.error("Unknown metadata_source: %r (expected 'txt' or 'xml')", source)
            return []

        if not configurations:
            logger.warning("No configurations found to index")
            return []

        logger.info("Found %d configuration(s)", len(configurations))
        for config in configurations:
            total_objects = sum(len(cat.metadata_objects) for cat in config.categories)
            logger.info(
                "  - %s: %d objects in %d categories",
                config.name, total_objects, len(config.categories)
            )

        return configurations

    def load_guid_map(self, code_directory: Path, load_metadata_guids: bool) -> Dict[str, str]:
        """
        Load GUID map from ConfigDumpInfo.xml if enabled.

        Args:
            code_directory: Directory containing code
            load_metadata_guids: Whether to load GUIDs

        Returns:
            Dictionary mapping GUIDs to metadata paths (empty if disabled or error)
        """
        guid_map = {}

        if not load_metadata_guids:
            logger.debug("GUID map loading disabled in settings")
            return guid_map

        try:
            guid_map = load_dumpinfo_map(code_directory)
            logger.info("Loaded GUID map with %d entries", len(guid_map))
        except Exception as e:
            logger.warning("Failed to load GUID map (ConfigDumpInfo.xml): %s", e)

        return guid_map
