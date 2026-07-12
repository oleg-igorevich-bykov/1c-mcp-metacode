"""
Parse-only BSL pipeline для incremental phase 2/3.

Контракт: workers НЕ открывают Neo4jLoader. Worker делает один pass через
filesystem:
  raw = read(path)
  content_hash = sha256(raw)
  if path.name == "Form.bin":
      code_chunks = FormBinParser(code_root).parse(path)
      parsed = scan_bsl_from_form_bin(code_chunks[0], ...)
  else:
      parsed = parse_bsl_from_bytes(raw, ...)

Главный процесс получает `ParsedBslFile` payload и сам принимает решения о
graph apply / routine-level delta. Lease.heartbeat() вызывается только в main
thread между poll-итерациями `ProcessPoolExecutor`.
"""

from __future__ import annotations

import hashlib
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ParsedBslFile:
    file_path: str                              # POSIX rel (Routine.file_path / Module.path)
    content_hash: str                           # sha256 raw file bytes
    module: Optional[Dict[str, Any]] = None     # для load_bsl_signatures
    routines: List[Dict[str, Any]] = field(default_factory=list)
    declares: List[Dict[str, Any]] = field(default_factory=list)
    common_declares: List[Dict[str, Any]] = field(default_factory=list)
    routines_index: List[Dict[str, Any]] = field(default_factory=list)
    callsites: List[Dict[str, Any]] = field(default_factory=list)
    form_links: List[Dict[str, Any]] = field(default_factory=list)
    abs_path: Optional[str] = None              # absolute path (для stat в orchestrator)


def _worker_parse_one(args: Tuple[str, str, str, str]) -> Optional[Dict[str, Any]]:
    """Worker function: один файл per call.

    Args: (path_str, code_root_str, project_name, config_name).
    Возвращает dict с payload или None при ошибке.
    """
    path_str, code_root_str, project_name, cfg_name = args
    try:
        from pathlib import Path as _P
        from bsl_signature_scanner import (
            parse_bsl_from_bytes,
            scan_bsl_from_form_bin,
        )

        path = _P(path_str)
        code_root = _P(code_root_str)
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        h = hashlib.sha256(raw).hexdigest()

        if path.name.lower() == "form.bin":
            # Form.bin: бинарный XCF, нужен FormBinParser → scan_bsl_from_form_bin.
            try:
                from parsers.form_bin_parser import FormBinParser

                parser = FormBinParser(code_root)
                code_chunks, _module_path_line = parser.parse(path)
            except Exception:
                logger.exception("parse_only worker: FormBinParser failed for %s", path)
                return None
            if not code_chunks or not code_chunks[0]:
                # Пустой Form.bin: возвращаем skeleton без routines (как делает текущий worker)
                return {
                    "abs_path": str(path),
                    "content_hash": h,
                    "kind": "bsl",
                    "module": None,
                    "routines": [],
                    "declares": [],
                    "common_declares": [],
                    "callsites": [],
                }
            parsed = scan_bsl_from_form_bin(
                code_chunks[0], path, code_root, project_name, cfg_name
            )
        else:
            parsed = parse_bsl_from_bytes(
                raw, path, code_root, project_name, cfg_name
            )

        if not parsed:
            return None
        out = dict(parsed)
        out["abs_path"] = str(path)
        out["content_hash"] = h
        return out
    except Exception:
        logger.exception("parse_only worker: parse failed for %s", path_str)
        return None


def _build_routines_index(routines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Lightweight payload, идентичный bsl_worker.routines_index (см.
    [bsl_worker.py:240-271]). Дублируется здесь для parse-only worker,
    чтобы не открывать дополнительные dependencies."""
    routines_index: List[Dict[str, Any]] = []
    for r in routines:
        params = r.get("params_json") or []
        min_arity = 0
        for p in params:
            if isinstance(p, dict):
                default_present = p.get("default_present", False)
                markers = p.get("markers_raw", [])
                is_optional = default_present or any(
                    "необязатель" in str(m).casefold() for m in markers
                )
                if not is_optional:
                    min_arity += 1
            else:
                min_arity += 1
        routines_index.append({
            "id": r["id"],
            "name": r["name"],
            "module_id": r.get("module_id"),
            "owner_qn": r.get("owner_qn"),
            "module_type": r.get("module_type"),
            "min_arity": min_arity,
            "max_arity": len(params),
            "directives": r.get("directives", []),
            "export": r.get("export", False),
            "params_json": params,
            "file_path": r.get("file_path"),
            "config_name": r.get("config_name"),
            "body_hash": r.get("body_hash", ""),
            "doc_hash": r.get("doc_hash", ""),
            "doc_description_embedding_hash": r.get("doc_description_embedding_hash", ""),
            "signature_hash": r.get("signature_hash", ""),
            "routine_state_hash": r.get("routine_state_hash", ""),
            "line": r.get("line", 0),
            "signature": r.get("signature", ""),
            "params_text": r.get("params_text", ""),
            "routine_type": r.get("routine_type", ""),
            "decorator_type": r.get("decorator_type", ""),
            "decorator_target": r.get("decorator_target", ""),
        })
    return routines_index


def _build_form_links(module: Optional[Dict[str, Any]], routines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Compact form_links: form_qn → routine_name (как в bsl_worker.py:256-264)."""
    form_links: List[Dict[str, Any]] = []
    mod = module or {}
    if mod.get("owner_label") == "Form" and mod.get("owner_qn"):
        fq = mod.get("owner_qn")
        for rr in routines:
            rn = rr.get("name")
            if rn:
                form_links.append({"form_qn": fq, "routine_name": rn})
    return form_links


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


def parse_bsl_files_parallel(
    paths: List[Path],
    *,
    code_root: Path,
    project_name: str,
    cfg_name: str,
    workers: Optional[int] = None,
    lease: Optional[Any] = None,
    poll_interval_sec: float = 0.5,
) -> List[ParsedBslFile]:
    """Параллельно распарсить список .bsl и Form.bin путей. Без graph writes.

    `lease.heartbeat()` вызывается **только в main thread** между poll-итерациями
    process pool — соблюдает jazzy-puzzling-pelican invariant
    (`state.py:108-113`: lease ownership instance scheduler-а).

    На пустой список возвращает []. Файлы, для которых worker не смог
    распарсить, в результат не попадают.
    """
    if not paths:
        return []

    n_workers = _resolve_workers(workers)
    args_list = [(str(p), str(code_root), project_name, cfg_name) for p in paths]

    if len(paths) < 4 or n_workers <= 1:
        return _parse_sequential(args_list)

    results: List[ParsedBslFile] = []
    try:
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            futures = {ex.submit(_worker_parse_one, a): a[0] for a in args_list}
            pending = set(futures)
            while pending:
                # Wait с timeout, чтобы heartbeat-ить lease между poll-итерациями.
                done, pending = _wait_with_heartbeat(pending, lease, poll_interval_sec)
                for fut in done:
                    res = fut.result()
                    pbf = _payload_to_parsed_bsl_file(res)
                    if pbf is not None:
                        results.append(pbf)
    except Exception:
        logger.exception(
            "parse_bsl_files_parallel: process pool failed; falling back to sequential"
        )
        return _parse_sequential(args_list)
    return results


def _wait_with_heartbeat(pending, lease, poll_interval_sec: float):
    """Возвращает (done, still_pending) после короткого ожидания.

    Между итерациями вызывает lease.heartbeat() (main-thread only). Лога ошибок
    heartbeat избегаем намеренно: LockLease сам защищён от исключений по
    контракту.
    """
    from concurrent.futures import wait, FIRST_COMPLETED

    done, still_pending = wait(pending, timeout=poll_interval_sec, return_when=FIRST_COMPLETED)
    if lease is not None and hasattr(lease, "heartbeat"):
        try:
            lease.heartbeat()
        except Exception:
            pass
    return done, still_pending


def _parse_sequential(args_list: List[Tuple[str, str, str, str]]) -> List[ParsedBslFile]:
    out: List[ParsedBslFile] = []
    for a in args_list:
        res = _worker_parse_one(a)
        pbf = _payload_to_parsed_bsl_file(res)
        if pbf is not None:
            out.append(pbf)
    return out


def _payload_to_parsed_bsl_file(payload: Optional[Dict[str, Any]]) -> Optional[ParsedBslFile]:
    """Worker payload → `ParsedBslFile` (включая routines_index и form_links)."""
    if not payload:
        return None
    routines = payload.get("routines", []) or []
    module = payload.get("module")
    file_path = (
        (routines[0].get("file_path") if routines else None)
        or (module.get("path") if isinstance(module, dict) else None)
        or ""
    )
    return ParsedBslFile(
        file_path=file_path,
        content_hash=payload.get("content_hash", ""),
        module=module,
        routines=routines,
        declares=payload.get("declares", []) or [],
        common_declares=payload.get("common_declares", []) or [],
        routines_index=_build_routines_index(routines),
        callsites=payload.get("callsites", []) or [],
        form_links=_build_form_links(module, routines),
        abs_path=payload.get("abs_path"),
    )
