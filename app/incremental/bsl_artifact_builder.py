"""
BSLFileArtifact builder — группирует уже агрегированный `BSLData` в per-file payload.

Используется и full reload baseline (одной утилитой обходим финальный BSLData и пишем
`bsl_file_artifacts` rows), и incremental (для перепаршенных в фазе 2/3 файлов).

Ключ группировки — `Routine.file_path` (POSIX-relative от data_directory, как у full-load
BSL scanner). Это тот же ключ, по которому Cypher-cleanup `delete_bsl_by_file_paths`
ищет `r.file_path`/`m.path`. Для callsites/form_links file_path выводится через карту
`routine_id → file_path` (callsites несут caller_id) или `module_id → file_path` для
form_links (где routine.module_id известен).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Set

from indexer.data_structures import BSLFileArtifact, BSLData

logger = logging.getLogger(__name__)

# Throttle прогресса группировки: логировать не реже одного раза в этот интервал
# ИЛИ каждые _PROGRESS_EVERY обработанных элементов (что наступит раньше).
_PROGRESS_INTERVAL_SEC = 10.0
_PROGRESS_EVERY = 100_000


class _PhaseProgress:
    """Throttled progress-логгер для одной фазы группировки (routines/callsites/...)."""

    def __init__(self, phase: str, total: int) -> None:
        self._phase = phase
        self._total = total
        self._count = 0
        self._start = time.monotonic()
        self._last_log = self._start

    def tick(self) -> None:
        self._count += 1
        now = time.monotonic()
        if (
            self._count % _PROGRESS_EVERY == 0
            or (now - self._last_log) >= _PROGRESS_INTERVAL_SEC
        ):
            self._last_log = now
            logger.info(
                "Artifact baseline grouping: phase=%s %d/%d elapsed=%.1fs",
                self._phase, self._count, self._total, now - self._start,
            )


def _form_bin_routine_to_index(r: Dict[str, Any]) -> Dict[str, Any]:
    """Lightweight projection полного Form.bin routine dict.

    Form.bin worker отдаёт routines в `routines_formbin` уже как full dicts
    (тот же scanner-payload, что и для .bsl), но `bsl_worker._streaming` для
    .bsl делает lightweight projection — мы повторяем его здесь, чтобы Form.bin
    routines попали в `routines_index` с теми же ключами.
    """
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
    return {
        "id": r.get("id"),
        "name": r.get("name"),
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
        "signature_hash": r.get("signature_hash", ""),
        "routine_state_hash": r.get("routine_state_hash", ""),
        "line": r.get("line", 0),
        "signature": r.get("signature", ""),
        "params_text": r.get("params_text", ""),
        "routine_type": r.get("routine_type", ""),
        "decorator_type": r.get("decorator_type", ""),
        "decorator_target": r.get("decorator_target", ""),
    }


def build_artifacts_from_bsl_data(
    *,
    bsl_data: BSLData,
    source_scope: str,
    file_path_to_hash: Dict[str, str],
    default_config_name: str = "",
) -> List[BSLFileArtifact]:
    """Сгруппировать `BSLData.routines_indexes/callsites/form_links` по `file_path`.

    `file_path_to_hash` — map от caller (POSIX-rel ключ → sha256 hex). Файлы без записи
    пропускаются (нет хеша → нет baseline). Группировка:
      - routines: по `routine.file_path`;
      - callsites: по `caller_id` через `routine_id → file_path` map;
      - form_links: через `routine_name` + `owner_qn` — берём первый match по имени
        (best-effort; для baseline-полноты пишем все form_links, у которых удалось
        найти связь с routine в том же файле).
    """
    routine_id_to_file: Dict[str, str] = {}
    artifacts: Dict[str, BSLFileArtifact] = {}
    seen_rids: Dict[str, Set[str]] = defaultdict(set)
    seen_mids: Dict[str, Set[str]] = defaultdict(set)

    def _ensure(fp: str, config_name: str) -> BSLFileArtifact:
        a = artifacts.get(fp)
        if a is None:
            a = BSLFileArtifact(
                source_scope=source_scope,
                config_name=config_name or default_config_name,
                rel_path=fp,
                content_hash=file_path_to_hash[fp],
            )
            artifacts[fp] = a
        return a

    # 1. Routines — основной источник file_path.
    # Включаем `.bsl` (через `routines_indexes`) И Form.bin (через `routines_formbin`).
    # Form.bin routines живут в полном payload (с body, hashes), поэтому из них
    # строим lightweight projection с теми же ключами, что bsl_worker формирует для
    # `routines_index` (см. [bsl_worker.py:240-271]). Без этого Form.bin baseline
    # пуст в `bsl_file_artifacts`, и routine-level diff для Form.bin не работает.
    chained_routines: List[Dict[str, Any]] = list(bsl_data.routines_indexes or [])
    for r in (bsl_data.routines_formbin or []):
        chained_routines.append(_form_bin_routine_to_index(r))

    callsites = bsl_data.callsites or []
    form_links = bsl_data.form_links or []
    logger.info(
        "Artifact baseline grouping start: scope=%s routines=%d callsites=%d form_links=%d hashed_files=%d",
        source_scope, len(chained_routines), len(callsites), len(form_links),
        len(file_path_to_hash),
    )
    t_group_start = time.monotonic()

    prog = _PhaseProgress("routines", len(chained_routines))
    for routine in chained_routines:
        prog.tick()
        fp = routine.get("file_path") or ""
        if not fp or fp not in file_path_to_hash:
            continue
        cfg = routine.get("config_name") or ""
        art = _ensure(fp, cfg)
        rid = routine.get("id")
        if rid:
            routine_id_to_file[rid] = fp
            if rid not in seen_rids[fp]:
                art.routine_ids.append(rid)
                seen_rids[fp].add(rid)
        mid = routine.get("module_id")
        if mid and mid not in seen_mids[fp]:
            art.module_ids.append(mid)
            seen_mids[fp].add(mid)
        art.routines_index.append(routine)

    # 2. Callsites — caller_id → routine.file_path.
    prog = _PhaseProgress("callsites", len(callsites))
    for callsite in callsites:
        prog.tick()
        caller_id = callsite.get("caller_id")
        if not caller_id:
            continue
        fp = routine_id_to_file.get(caller_id)
        if fp is None or fp not in file_path_to_hash:
            continue
        art = _ensure(fp, callsite.get("config_name") or "")
        art.callsites.append(callsite)

    # 3. Form links — link.routine_name + link.form_qn → routine_id → file_path.
    routine_name_owner_to_file: Dict[Any, str] = {}
    for routine in chained_routines:
        fp = routine.get("file_path") or ""
        if not fp:
            continue
        key = (routine.get("name") or "", routine.get("owner_qn") or "")
        routine_name_owner_to_file[key] = fp

    prog = _PhaseProgress("form_links", len(form_links))
    for link in form_links:
        prog.tick()
        form_qn = link.get("form_qn") or ""
        routine_name = link.get("routine_name") or ""
        fp = routine_name_owner_to_file.get((routine_name, form_qn))
        if fp is None or fp not in file_path_to_hash:
            continue
        art = _ensure(fp, link.get("config_name") or "")
        art.form_links.append(link)

    result = list(artifacts.values())
    logger.info(
        "Artifact baseline grouping done: scope=%s artifacts=%d elapsed=%.1fs",
        source_scope, len(result), time.monotonic() - t_group_start,
    )
    return result


def persist_artifacts(
    state: Any,
    artifacts: List[BSLFileArtifact],
) -> None:
    """Записать список `BSLFileArtifact` в SQLite через `IncrementalLoadingState`.

    Делает batch upsert через `upsert_bsl_file_artifacts_many`. Соединение в
    autocommit (`isolation_level=None`); если caller хочет атомарность нескольких
    persist-вызовов (например, base + extensions в baseline), он оборачивает их в
    `with state.transaction():` снаружи.
    """
    if not artifacts:
        return
    state.upsert_bsl_file_artifacts_many(artifacts)
