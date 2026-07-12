"""
IndexingResult dataclass — contract расширения для baseline init после full reload.

Возвращается из `IndexerOrchestrator.run_indexing` и `Indexer.index_metadata`
вместо `bool`. `__bool__` сохраняет совместимость с существующими `if success:` /
`if not success: sys.exit(1)` в main.py.

`LoadedExtensionSnapshot` — per-extension snapshot, нужный `_init_incremental_baseline`
для построения baseline state каждого расширения в его собственном scope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional


@dataclass(slots=True)
class LoadedExtensionSnapshot:
    """Per-extension snapshot для baseline init инкрементальной загрузки.

    `ext_graph_config_name` — имя конфигурации с `$ext$`-суффиксом, под которым
    graph nodes реально загружены (`ExtensionsLoader` восстанавливает raw
    `configuration.name` ПОСЛЕ graph load, поэтому `configuration.name` к моменту
    baseline init уже raw — использовать его для QN-сборки нельзя).
    """

    ext_dir_name: str
    ext_graph_config_name: str
    base_config_name: str
    source: str  # "txt" | "xml"
    ext_metadata_dir: Optional[Path] = None  # для TXT
    ext_code_dir: Optional[Path] = None  # для XML
    ext_code_index: Optional[Any] = None  # CodeFileIndex для XML
    configuration: Any = None  # parsed Configuration (raw name к моменту baseline)
    # Финальный BSLData для расширения (для `bsl_file_artifacts` baseline). None если
    # BSL не парсился для этого расширения (например, `LOAD_BSL_SIGNATURES=false`).
    bsl_data: Optional[Any] = None


@dataclass(slots=True)
class IndexingResult:
    success: bool = False
    configurations: List[Any] = field(default_factory=list)
    code_index: Optional[Any] = None
    metadata_source: str = "txt"
    metadata_dir: Optional[Path] = None
    extensions: List[LoadedExtensionSnapshot] = field(default_factory=list)
    # Финальный агрегированный BSLData после full-load (заполняется orchestrator-ом).
    # Используется `_init_artifact_baseline` чтобы записать `bsl_file_artifacts` rows
    # симметрично incremental phase 2/3.
    bsl_data: Optional[Any] = None

    def __bool__(self) -> bool:
        return self.success
