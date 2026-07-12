"""
Data structures for passing data between indexer components.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Any, Tuple, Optional


@dataclass
class FormsData:
    """Data collected from Form.xml and Form.bin files"""
    form_updates: List[Dict[str, Any]] = field(default_factory=list)
    controls: List[Dict[str, Any]] = field(default_factory=list)
    root_rel: List[Dict[str, Any]] = field(default_factory=list)
    child_rel: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    event_rel: List[Dict[str, Any]] = field(default_factory=list)
    event_actions: List[Dict[str, Any]] = field(default_factory=list)
    form_attributes: List[Dict[str, Any]] = field(default_factory=list)
    form_commands: List[Dict[str, Any]] = field(default_factory=list)
    form_command_usages: List[Dict[str, Any]] = field(default_factory=list)
    data_bindings: List[Dict[str, Any]] = field(default_factory=list)
    form_content_hashes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        """Convert to dictionary format for Neo4j loader"""
        return {
            "form_updates": self.form_updates,
            "controls": self.controls,
            "root_rel": self.root_rel,
            "child_rel": self.child_rel,
            "events": self.events,
            "event_rel": self.event_rel,
            "event_actions": self.event_actions,
            "form_attributes": self.form_attributes,
            "form_commands": self.form_commands,
            "form_command_usages": self.form_command_usages,
            "data_bindings": self.data_bindings,
        }

    def merge(self, other: 'FormsData'):
        """Merge another FormsData into this one"""
        self.form_updates.extend(other.form_updates)
        self.controls.extend(other.controls)
        self.root_rel.extend(other.root_rel)
        self.child_rel.extend(other.child_rel)
        self.events.extend(other.events)
        self.event_rel.extend(other.event_rel)
        self.event_actions.extend(other.event_actions)
        self.form_attributes.extend(other.form_attributes)
        self.form_commands.extend(other.form_commands)
        self.form_command_usages.extend(other.form_command_usages)
        self.data_bindings.extend(other.data_bindings)
        self.form_content_hashes.extend(other.form_content_hashes)


@dataclass
class BSLFileArtifact:
    """Per-file BSL payload — единый контракт для full reload baseline и incremental.

    Заполняется BSL workers/scanner-ом на месте парсинга одного `.bsl` или `Form.bin`,
    где `rel_path`/`config_name`/`content_hash` известны точно. Передаётся в incremental
    state (`bsl_file_artifacts` SQLite таблица) и используется для scoped CALLS.
    """

    source_scope: str
    config_name: str
    rel_path: str
    content_hash: str
    routine_ids: List[str] = field(default_factory=list)
    module_ids: List[str] = field(default_factory=list)
    routines_index: List[Dict[str, Any]] = field(default_factory=list)
    callsites: List[Dict[str, Any]] = field(default_factory=list)
    form_links: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class BSLData:
    """Data collected from BSL files (.bsl and Form.bin)"""
    modules_by_id: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    routines_formbin: List[Dict[str, Any]] = field(default_factory=list)  # Full routine data from Form.bin
    routines_indexes: List[Dict[str, Any]] = field(default_factory=list)  # Lightweight indexes for callsites resolution
    declares: List[Dict[str, Any]] = field(default_factory=list)
    common_declares: List[Dict[str, Any]] = field(default_factory=list)
    callsites: List[Dict[str, Any]] = field(default_factory=list)
    form_links: List[Dict[str, Any]] = field(default_factory=list)
    form_routines: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # form_qn -> routines


@dataclass
class PredefinedData:
    """Data collected from Predefined.xml files"""
    items: List[Dict[str, Any]] = field(default_factory=list)
    relations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class HelpData:
    """Data collected from Help/ru.html files"""
    help_by_object: Dict[Tuple[str, str], str] = field(default_factory=dict)  # (category_folder, object_name) -> help_text


@dataclass
class ProcessingConfig:
    """Configuration for what to process during indexing"""
    enable_forms: bool = True
    enable_predefined: bool = True
    enable_bsl: bool = True
    enable_help: bool = True
    enable_extensions: bool = True
    enable_role_rights: bool = False
    enable_event_subscriptions: bool = True
    max_workers: int = 8
    max_in_flight: int = 32

    @classmethod
    def from_settings(cls, settings) -> 'ProcessingConfig':
        """Create configuration from global settings"""
        import os
        max_workers = min(8, (os.cpu_count() or 4))
        return cls(
            enable_forms=settings.load_forms_from_xml,
            enable_predefined=settings.load_predefined_values,
            enable_bsl=getattr(settings, "load_bsl_signatures", True),
            enable_help=getattr(settings, "load_help_from_html", True),
            enable_extensions=settings.load_extensions,
            enable_role_rights=getattr(settings, "load_role_rights", False),
            enable_event_subscriptions=getattr(settings, "load_event_subscriptions", True),
            max_workers=max_workers,
            max_in_flight=max_workers * 4,
        )


@dataclass
class ProcessingStatistics:
    """Statistics collected during processing"""
    discovered_forms: int = 0
    discovered_predef: int = 0
    discovered_bsl: int = 0
    discovered_form_bin: int = 0
    discovered_help: int = 0
    discovered_event_subs: int = 0

    parsed_forms: int = 0
    parsed_predef: int = 0
    parsed_bsl: int = 0
    parsed_form_bin: int = 0
    parsed_help: int = 0
    parsed_event_subs: int = 0

    def summary(self) -> str:
        """Get formatted summary of statistics"""
        return (
            f"Discovery: forms={self.discovered_forms}, predef={self.discovered_predef}, "
            f"bsl={self.discovered_bsl}, form_bin={self.discovered_form_bin}, "
            f"help={self.discovered_help}, event_subs={self.discovered_event_subs}\n"
            f"Parsed: forms={self.parsed_forms}, predef={self.parsed_predef}, "
            f"bsl={self.parsed_bsl}, form_bin={self.parsed_form_bin}, "
            f"help={self.parsed_help}, event_subs={self.parsed_event_subs}"
        )


@dataclass
class ExtensionScanResults:
    """Results from scanning a single extension directory"""
    # XML files for property analysis (existing functionality)
    xml_files_for_analysis: List[Any] = field(default_factory=list)  # List of Path objects

    # Predefined data from Ext/Predefined.xml files
    predef_data: PredefinedData = field(default_factory=PredefinedData)

    # Form.xml files found in Forms/ and CommonForms/: (form_xml_path, descriptor_xml_path, is_adopted)
    form_files: List[Tuple[Path, Path, bool]] = field(default_factory=list)

    # EventSubscription XML files from EventSubscriptions/ directory
    event_subscription_files: List[Path] = field(default_factory=list)

    # BSL files collected during the same os.walk pass (avoids a separate rglob)
    bsl_files: List[Path] = field(default_factory=list)

    # Help content collected from Ext/Help/ru.html files
    help_data: HelpData = field(default_factory=HelpData)
