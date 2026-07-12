"""
Параллельный hashing файлов для baseline init и phase 2/3 diff.

ProcessPoolExecutor поверх простого worker, считающего stat() + sha256 для одного
файла. Управляется через `INCREMENTAL_ARTIFACT_WORKERS` (fallback на
BSL_PROCESS_WORKERS → PROCESS_WORKERS → 4). На вход — уже найденный список путей
из CodeFileIndex; никакого нового scan каталога.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileStat:
    size: int
    mtime_ns: int
    content_hash: str


def _hash_one(path_str: str) -> Optional[tuple]:
    """Worker function: stat + sha256. Возвращает (path_str, FileStat) или None."""
    try:
        p = Path(path_str)
        st = p.stat()
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return (path_str, st.st_size, st.st_mtime_ns, h.hexdigest())
    except OSError:
        return None


def _resolve_workers(workers: Optional[int]) -> int:
    if workers and workers > 0:
        return workers
    try:
        from config import settings  # type: ignore
        candidate = (
            getattr(settings, "INCREMENTAL_ARTIFACT_WORKERS", None)
            or getattr(settings, "BSL_PROCESS_WORKERS", None)
            or getattr(settings, "PROCESS_WORKERS", None)
        )
        if candidate and candidate > 0:
            return int(candidate)
    except Exception:
        pass
    return 4


def hash_files_parallel(
    paths: Iterable[Path],
    workers: Optional[int] = None,
) -> Dict[Path, FileStat]:
    """Параллельно посчитать (size, mtime_ns, content_hash) для всех путей.

    Файлы, на которых стрельнул OSError, в результат не попадают (потерянные/
    удалённые между сбором списка и hashing). Caller сам решает, как
    реагировать на отсутствие пути в map.

    Для очень коротких списков (< 4 файлов) использует прямой sequential путь
    без накладных расходов на стартап ProcessPoolExecutor.
    """
    path_list: List[Path] = list(paths)
    if not path_list:
        return {}

    n_workers = _resolve_workers(workers)
    if len(path_list) < 4 or n_workers <= 1:
        return _hash_sequential(path_list)

    out: Dict[Path, FileStat] = {}
    str_paths = [str(p) for p in path_list]
    try:
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            chunksize = max(1, len(str_paths) // (n_workers * 4))
            for res in ex.map(_hash_one, str_paths, chunksize=chunksize):
                if res is None:
                    continue
                path_str, size, mtime_ns, content_hash = res
                out[Path(path_str)] = FileStat(size, mtime_ns, content_hash)
    except Exception:
        logger.exception(
            "hash_files_parallel: process pool failed (n_paths=%d, workers=%d); "
            "falling back to sequential",
            len(path_list), n_workers,
        )
        return _hash_sequential(path_list)
    return out


def _hash_sequential(paths: List[Path]) -> Dict[Path, FileStat]:
    out: Dict[Path, FileStat] = {}
    for p in paths:
        res = _hash_one(str(p))
        if res is None:
            continue
        _, size, mtime_ns, content_hash = res
        out[p] = FileStat(size, mtime_ns, content_hash)
    return out
