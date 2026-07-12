"""
Single-pass code directory scanner.

CodeFileIndexer.scan(code_root) walks the configuration code tree exactly once
and classifies every file into typed lists (XML descriptors, BSL modules,
Form.xml payloads, predefined values, role rights, etc). All downstream stages
(DirectoryScanner, RoleRightsProcessor, ExtensionsLoader, XmlMetadataParser,
etc.) consume the resulting CodeFileIndex without performing their own walks.

Filtering rules:
  * metadata_xml_files: XML descriptor files for metadata objects (and their
    children: Forms/<F>.xml, Commands/<C>.xml, Templates/<T>.xml, etc.).
    Excludes payload XML from Ext/ subdirs (Form.xml, Predefined.xml,
    Rights.xml), ConfigDumpInfo.xml, and Help/*.
  * form_xml_files: list[FormXmlEntry] for every .../Forms|CommonForms/<F>/Ext/Form.xml.
    The descriptor companion path (Forms/<F>.xml) is recorded but NOT read here.
  * bsl_files: every .bsl, including configuration-level modules in
    code/Ext/<ModuleName>.bsl (no category whitelist applied for BSL).
  * extension_property_analysis_xml_files: only top-level <Category>/<Object>.xml
    descriptors (subset of metadata_xml_files) for the analyzer that builds
    controlled_properties / modified_properties on extension nodes.

The classifier reuses the path predicates from the existing DirectoryScanner.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from xml_metadata.folder_map import FOLDER_NAMES

logger = logging.getLogger(__name__)


@dataclass
class FormXmlEntry:
    """One Ext/Form.xml payload, with its descriptor pointer.

    object_name / form_name semantics MUST match xcf_utils.parse_path_triplet:
      * For CommonForms/<Name>/Ext/Form.xml:  object_name=<Name>, form_name="Форма"
      * For <Category>/<Owner>/Forms/<F>/Ext/Form.xml:
            object_name=<Owner>, form_name=<F>
    """

    form_xml_path: Path
    descriptor_xml_path: Optional[Path]
    category_folder: str
    object_name: str
    form_name: str


@dataclass
class CodeFileIndex:
    """Result of CodeFileIndexer.scan(code_root) — one root, one walk."""

    root: Path

    metadata_xml_files: List[Path] = field(default_factory=list)
    config_xml: Optional[Path] = None

    form_xml_files: List[FormXmlEntry] = field(default_factory=list)
    form_bin_files: List[Path] = field(default_factory=list)

    predefined_xml_files: List[Path] = field(default_factory=list)
    event_subscription_xml_files: List[Path] = field(default_factory=list)
    help_html_files: List[Path] = field(default_factory=list)
    rights_xml_files: List[Path] = field(default_factory=list)

    bsl_files: List[Path] = field(default_factory=list)

    config_dump_info: Optional[Path] = None

    # Subset of metadata_xml_files restricted to top-level descriptors
    # (<Category>/<ObjectName>.xml). Used by extension_properties_classifier
    # and extension_properties_extractor.
    extension_property_analysis_xml_files: List[Path] = field(default_factory=list)


@dataclass
class CodeFileScanConsumers:
    """Optional streaming callbacks fired by CodeFileIndexer.scan during the
    single os.walk, right after each file is classified. Lets downstream stages
    (XML parse, secondary parse, BSL queue) overlap with the walk instead of
    waiting for the full index to be built.

    Each callback fires exactly once per matching file. The full CodeFileIndex
    is still returned regardless; callbacks are purely additive. No callback for
    Rights.xml — role rights are parsed later from CodeFileIndex.rights_xml_files.
    """

    on_metadata_xml: Optional[Callable[[Path], None]] = None
    on_form_xml: Optional[Callable[["FormXmlEntry"], None]] = None
    on_predefined_xml: Optional[Callable[[Path], None]] = None
    on_event_subscription_xml: Optional[Callable[[Path], None]] = None
    on_help_html: Optional[Callable[[Path], None]] = None
    on_bsl_file: Optional[Callable[[Path], None]] = None
    on_form_bin: Optional[Callable[[Path], None]] = None


def _is_valid_form_xml_path(p: Path) -> bool:
    return (
        p.parent.name.lower() == "ext"
        and len(p.parents) >= 3
        and p.parent.parent.parent.name in ("Forms", "CommonForms")
    )


def _is_valid_form_bin_path(p: Path) -> bool:
    return (
        p.parent.name.lower() == "ext"
        and len(p.parents) >= 3
        and p.parents[2].name in ("Forms", "CommonForms")
    )


def _is_valid_help_html_path(p: Path) -> bool:
    # .../<Category>/<ObjectName>/Ext/Help/ru.html
    # NOT form-level help: .../Forms/<FormName>/Ext/Help/ru.html
    return (
        p.parent.name.lower() == "help"
        and len(p.parents) >= 4
        and p.parent.parent.name.lower() == "ext"
        and p.parents[3].name.lower() not in ("forms", "commonforms")
    )


def _build_form_entry(form_xml_path: Path) -> Optional[FormXmlEntry]:
    """Build a FormXmlEntry from a verified Form.xml path.

    Path shape: <code_root>/<Category>/(optional <Owner>/Forms/)<FormName>/Ext/Form.xml
    """
    if not _is_valid_form_xml_path(form_xml_path):
        return None

    form_dir = form_xml_path.parent.parent  # <FormName>/
    forms_or_common = form_dir.parent  # Forms/ or CommonForms/
    form_name_dir = form_dir.name

    if forms_or_common.name == "CommonForms":
        # CommonForms/<Name>/Ext/Form.xml -> object_name=<Name>, form_name="Форма"
        descriptor = forms_or_common / f"{form_name_dir}.xml"
        return FormXmlEntry(
            form_xml_path=form_xml_path,
            descriptor_xml_path=descriptor if descriptor.exists() else None,
            category_folder="CommonForms",
            object_name=form_name_dir,
            form_name="Форма",
        )

    # <Category>/<Owner>/Forms/<FormName>/Ext/Form.xml
    owner_dir = forms_or_common.parent  # <Owner>/
    category_dir = owner_dir.parent  # <Category>/
    descriptor = forms_or_common / f"{form_name_dir}.xml"
    return FormXmlEntry(
        form_xml_path=form_xml_path,
        descriptor_xml_path=descriptor if descriptor.exists() else None,
        category_folder=category_dir.name,
        object_name=owner_dir.name,
        form_name=form_name_dir,
    )


class CodeFileIndexer:
    """Walks a code root exactly once, returning a typed CodeFileIndex."""

    @staticmethod
    def scan(
        code_root: Path,
        consumers: Optional[CodeFileScanConsumers] = None,
    ) -> CodeFileIndex:
        """Single os.walk pass over code_root. No file content is read.

        If `consumers` is given, the matching callback fires once per file during
        the walk (streaming overlap). The full CodeFileIndex is always returned.
        """
        code_root = Path(code_root)
        index = CodeFileIndex(root=code_root)

        # Normalize to a non-None instance so call sites stay clean.
        cons = consumers if consumers is not None else CodeFileScanConsumers()

        def emit(cb: Optional[Callable], value) -> None:
            if cb is not None:
                cb(value)

        if not code_root.exists():
            logger.warning("CodeFileIndexer.scan: root does not exist: %s", code_root)
            return index

        # Special case (mirrors DirectoryScanner): if code/EventSubscriptions is
        # a symlink, os.walk(followlinks=False) won't descend into it. Pick up
        # its XMLs explicitly.
        event_subs_dir = code_root / "EventSubscriptions"
        if event_subs_dir.is_symlink() and event_subs_dir.is_dir():
            for xml_file in event_subs_dir.glob("*.xml"):
                # Dual-listing: the dedicated event subscription loader updates
                # existing MetadataObject nodes via MATCH, so the XML parser
                # must also see this file to create the corresponding
                # Project/Config/ПодпискиНаСобытия/<Name> node first.
                index.event_subscription_xml_files.append(xml_file)
                index.metadata_xml_files.append(xml_file)
                emit(cons.on_event_subscription_xml, xml_file)
                emit(cons.on_metadata_xml, xml_file)

        for raw_root, dirs, files in os.walk(code_root, followlinks=False):
            current = Path(raw_root)
            try:
                rel_parts = current.relative_to(code_root).parts
            except ValueError:
                continue

            for fname in files:
                file_path = current / fname
                lower = fname.lower()

                # ConfigDumpInfo.xml at the root
                if fname == "ConfigDumpInfo.xml" and len(rel_parts) == 0:
                    index.config_dump_info = file_path
                    continue

                # BSL files — no category filter (configuration modules live in code/Ext/)
                if lower.endswith(".bsl"):
                    index.bsl_files.append(file_path)
                    emit(cons.on_bsl_file, file_path)
                    continue

                # Form.xml payload (Forms/<F>/Ext/Form.xml or CommonForms/<F>/Ext/Form.xml)
                if fname == "Form.xml" and _is_valid_form_xml_path(file_path):
                    entry = _build_form_entry(file_path)
                    if entry is not None:
                        index.form_xml_files.append(entry)
                        emit(cons.on_form_xml, entry)
                    continue

                # Form.bin payload (mirrors Form.xml location)
                if fname == "Form.bin" and _is_valid_form_bin_path(file_path):
                    index.form_bin_files.append(file_path)
                    emit(cons.on_form_bin, file_path)
                    continue

                # Help/ru.html (object-level only, not form-level)
                if lower == "ru.html" and _is_valid_help_html_path(file_path):
                    index.help_html_files.append(file_path)
                    emit(cons.on_help_html, file_path)
                    continue

                if not lower.endswith(".xml"):
                    continue

                # Predefined.xml payload — always inside Ext/
                if lower == "predefined.xml" and "Ext" in rel_parts:
                    index.predefined_xml_files.append(file_path)
                    emit(cons.on_predefined_xml, file_path)
                    continue

                # Rights.xml payload — Roles/<Role>/Ext/Rights.xml
                # No streaming callback: role rights are parsed later from the index.
                if lower == "rights.xml" and "Ext" in rel_parts and "Roles" in rel_parts:
                    index.rights_xml_files.append(file_path)
                    continue

                # Skip any other XML living under an Ext/ subtree (lab classify_xml_path "ext_subtree")
                if "Ext" in rel_parts:
                    continue

                # Configuration.xml at the code root
                if fname == "Configuration.xml" and len(rel_parts) == 0:
                    index.config_xml = file_path
                    index.metadata_xml_files.append(file_path)
                    emit(cons.on_metadata_xml, file_path)
                    continue

                # EventSubscriptions/<Name>.xml — listed in BOTH places:
                #   * event_subscription_xml_files for the specialized loader
                #     (it updates an existing node via MATCH);
                #   * metadata_xml_files so XmlMetadataParser creates the
                #     Project/Config/ПодпискиНаСобытия/<Name> node first.
                # Stops further classification (do not fall into the Subsystems-aware
                # nested-descriptor branch below).
                if len(rel_parts) == 1 and rel_parts[0] == "EventSubscriptions":
                    index.event_subscription_xml_files.append(file_path)
                    index.metadata_xml_files.append(file_path)
                    emit(cons.on_event_subscription_xml, file_path)
                    emit(cons.on_metadata_xml, file_path)
                    continue

                # Metadata descriptor XML files
                # - top-level: <Category>/<Object>.xml  (len(rel_parts)==1)
                # - nested:    <Category>/<Object>/Forms|Commands|Templates|.../<Child>.xml
                # - Subsystems can nest themselves: Subsystems/<A>/Subsystems/<B>.xml
                if rel_parts and rel_parts[0] in FOLDER_NAMES:
                    # Top-level <Category>/<Object>.xml
                    if len(rel_parts) == 1:
                        index.metadata_xml_files.append(file_path)
                        index.extension_property_analysis_xml_files.append(file_path)
                        emit(cons.on_metadata_xml, file_path)
                        continue
                    # Nested descriptor XML.
                    # Accept any *.xml that is NOT in Ext/ subtree and lives under a known category root.
                    # The XML parser will further skip unknown shapes via parse_descriptor.
                    index.metadata_xml_files.append(file_path)
                    emit(cons.on_metadata_xml, file_path)
                    # Nested Subsystems (Subsystems/<A>/Subsystems/<B>.xml etc.) must
                    # also reach the extension analyzer. ExtensionPropertiesClassifier
                    # recognizes "Subsystem" as an object type and ExtensionsLoader
                    # builds a path-based QN specifically for nested subsystems.
                    # current.name == "Subsystems" matches any subsystem descriptor
                    # (top-level case already returned above via the len==1 branch).
                    if current.name == "Subsystems":
                        index.extension_property_analysis_xml_files.append(file_path)
                    continue

        logger.info(
            "CodeFileIndexer: root=%s metadata_xml=%d (top-level=%d) forms=%d form_bins=%d "
            "predefined=%d events=%d help=%d rights=%d bsl=%d",
            code_root,
            len(index.metadata_xml_files),
            len(index.extension_property_analysis_xml_files),
            len(index.form_xml_files),
            len(index.form_bin_files),
            len(index.predefined_xml_files),
            len(index.event_subscription_xml_files),
            len(index.help_html_files),
            len(index.rights_xml_files),
            len(index.bsl_files),
        )
        return index
