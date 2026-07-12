"""
Indexer package for loading 1C metadata into Neo4j.

This package provides a modular architecture for processing various
types of 1C metadata files and loading them into a Neo4j graph database.

Main entry point:
    Indexer - Simple facade for indexing operations

Components:
    - MetadataLoader: Loads base configuration metadata
    - FormsProcessor: Processes Form.xml and Form.bin files
    - BSLProcessor: Processes BSL code with multiprocessing
    - CallsitesResolver: Resolves function/procedure calls
    - PredefinedProcessor: Processes predefined values
    - HelpProcessor: Processes help documentation
    - RoleRightsProcessor: Processes role permissions
    - ExtensionsLoader: Loads 1C extensions
    - DirectoryScanner: Performs streaming directory scan
    - IndexerOrchestrator: Coordinates all indexing stages
"""

from .indexer_facade import Indexer
from .orchestrator import IndexerOrchestrator
from .metadata_loader import MetadataLoader
from .forms_processor import FormsProcessor
from .bsl_processor import BSLProcessor
from .callsites_resolver import CallsitesResolver
from .predefined_processor import PredefinedProcessor
from .help_processor import HelpProcessor
from .role_rights_processor import RoleRightsProcessor
from .extensions_loader import ExtensionsLoader
from .scanner import DirectoryScanner, DirectoryScanSession
from .code_file_index import CodeFileIndexer, CodeFileScanConsumers
from .statistics import IndexingStatistics
from .data_structures import (
    FormsData,
    BSLData,
    PredefinedData,
    HelpData,
    ProcessingConfig,
    ProcessingStatistics,
)

__all__ = [
    # Main entry point
    "Indexer",
    # Core orchestration
    "IndexerOrchestrator",
    # Processors
    "MetadataLoader",
    "FormsProcessor",
    "BSLProcessor",
    "CallsitesResolver",
    "PredefinedProcessor",
    "HelpProcessor",
    "RoleRightsProcessor",
    "ExtensionsLoader",
    "DirectoryScanner",
    "DirectoryScanSession",
    "CodeFileIndexer",
    "CodeFileScanConsumers",
    "IndexingStatistics",
    # Data structures
    "FormsData",
    "BSLData",
    "PredefinedData",
    "HelpData",
    "ProcessingConfig",
    "ProcessingStatistics",
]
