"""
Routine-level diff builder для incremental phase 2/3 BSL apply.

Pure функция `build_delta(old_artifacts, parsed, deleted_paths) -> BslApplyDelta`
без побочных эффектов: на вход — old `BSLFileArtifact`-style dicts (как отдаёт
`state.get_bsl_file_artifact`) и `ParsedBslFile` для added/changed файлов; на
выход — flat plan для `_apply_bsl` orchestrator-а.

7 diff классов per routine:
    unchanged, line_only, doc_changed, body_changed, signature_changed, added, deleted.

Diff правила (см. plan):
    unchanged          → routine_state_hash совпал → ничего
    line_only          → body/doc/signature_hash совпали, line/file_path изменились
                          → upsert routine (line/file_path), embeddings сохраняются
    doc_changed        → doc_hash отличается → upsert + clear doc embedding для rid
    body_changed       → body_hash отличается → upsert + clear code embedding для rid
                          + caller'ом будет сам rid в scoped CALLS rebuild
    signature_changed  → signature_hash отличается → upsert + оба clear по соответ.
                          hash + старый rid в calls_class_a_old_targets
    added              → новая rid → upsert
    deleted            → rid отсутствует в new → routine_ids_to_delete + class (a)

Module-уровень: если у файла не осталось routines, его module_id_to_delete; иначе
MERGE через load_bsl_signatures (DECLARES сохраняется).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from indexer.data_structures import BSLFileArtifact

logger = logging.getLogger(__name__)


RoutineDeltaCls = Literal[
    "unchanged",
    "line_only",
    "doc_changed",
    "body_changed",
    "signature_changed",
    "added",
    "deleted",
]


@dataclass
class RoutineDelta:
    rid: str
    cls: RoutineDeltaCls
    new: Optional[Dict[str, Any]] = None  # routine dict из ParsedBslFile (None для deleted)
    old: Optional[Dict[str, Any]] = None  # routine dict из old artifact (None для added)


@dataclass
class BslFileDelta:
    rel_path: str
    routine_deltas: List[RoutineDelta] = field(default_factory=list)
    new_artifact: Optional[BSLFileArtifact] = None
    new_module_ids: List[str] = field(default_factory=list)
    removed_module_ids: List[str] = field(default_factory=list)


@dataclass
class CodeSearchDelta:
    added_or_changed_routine_ids: Set[str] = field(default_factory=set)
    deleted_routine_ids: Set[str] = field(default_factory=set)
    metadata_only_routine_ids: Set[str] = field(default_factory=set)
    affected_rel_paths: Set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not (
            self.added_or_changed_routine_ids
            or self.deleted_routine_ids
            or self.metadata_only_routine_ids
            or self.affected_rel_paths
        )


@dataclass
class BslApplyDelta:
    file_deltas: List[BslFileDelta] = field(default_factory=list)
    routines_to_upsert: List[Dict[str, Any]] = field(default_factory=list)
    modules_to_upsert: List[Dict[str, Any]] = field(default_factory=list)
    declares_to_upsert: List[Dict[str, Any]] = field(default_factory=list)
    common_declares_to_upsert: List[Dict[str, Any]] = field(default_factory=list)
    routine_ids_to_delete: List[str] = field(default_factory=list)
    module_ids_to_delete: List[str] = field(default_factory=list)
    doc_embeddings_to_clear: List[str] = field(default_factory=list)
    code_embeddings_to_clear: List[str] = field(default_factory=list)
    routine_doc_repass_ids: Set[str] = field(default_factory=set)
    calls_class_a_old_targets: List[str] = field(default_factory=list)
    calls_affected_callers: List[str] = field(default_factory=list)
    new_routine_targets: Set[Tuple[Any, ...]] = field(default_factory=set)
    code_search_delta: CodeSearchDelta = field(default_factory=CodeSearchDelta)


def _should_clear_doc_embedding(old_idx_r: Dict[str, Any], new_idx_r: Dict[str, Any]) -> bool:
    """True если doc_description_embedding нужно инвалидировать.

    При наличии fine-grained hash сравниваем только его (только doc_description).
    При отсутствии hash в старом sidecar (строки до деплоя) — консервативный
    fallback через doc_hash: то же поведение, что было до внедрения нового поля.
    """
    old_hash = old_idx_r.get("doc_description_embedding_hash") or ""
    new_hash = new_idx_r.get("doc_description_embedding_hash") or ""
    if old_hash:
        return old_hash != new_hash
    # Fallback: old sidecar row без нового поля — используем doc_hash (консервативно).
    return old_idx_r.get("doc_hash") != new_idx_r.get("doc_hash")


def _routine_target_key(r: Dict[str, Any]) -> Tuple[Any, ...]:
    """Target tuple для scoped CALLS class (c) prefilter.

    Формат совпадает с `artifact_sync._routine_target` (см.
    `_callsite_matches_any` в artifact_sync.py:309-311):
    `(qualifier_short, manager_qualifier, name_lower, module_type, a_min, a_max)`.

    Делегируем в `artifact_sync._routine_target`, чтобы один источник истины и
    обновления категорий/manager-qualifier не разъезжались.
    """
    from .artifact_sync import _routine_target as _at_routine_target  # local: cycle-safe
    return _at_routine_target(r)


def _classify(old: Dict[str, Any], new: Dict[str, Any]) -> RoutineDeltaCls:
    """7-классовая классификация per routine (по hashes)."""
    if old.get("routine_state_hash") == new.get("routine_state_hash"):
        return "unchanged"
    body_changed = old.get("body_hash") != new.get("body_hash")
    doc_changed = old.get("doc_hash") != new.get("doc_hash")
    sig_changed = old.get("signature_hash") != new.get("signature_hash")
    if sig_changed:
        return "signature_changed"
    if body_changed:
        return "body_changed"
    if doc_changed:
        return "doc_changed"
    # routine_state_hash отличается, но body/doc/signature совпали → line/file_path.
    # rel_path входит в FTS payload (worker `_build_search_text` repeats it
    # × _METADATA_WEIGHT into chunk_search_text, and `_build_fields` writes it
    # into bsl_code_unit_fields.path). Pure metadata UPDATE without rebuilding
    # FTS would leave stale path tokens → promote to body_changed equivalent.
    if (old.get("file_path") or old.get("rel_path") or "") != (
        new.get("file_path") or new.get("rel_path") or ""
    ):
        return "body_changed"
    return "line_only"


def build_delta(
    old_artifacts: Dict[str, Dict[str, Any]],
    parsed: List[Any],
    deleted_paths: List[str],
) -> BslApplyDelta:
    """Pure функция: построить flat apply plan.

    `old_artifacts` — map `rel_path → state.get_bsl_file_artifact(scope, key)` dict
    (с ключами `routine_ids`, `routines_index`, ...). Если для changed файла
    artifact отсутствует — все routines файла классифицируются как added.

    `parsed` — `List[ParsedBslFile]` для added/changed файлов (тип передаём как
    Any, чтобы не плодить import цикл; pyright проверять не будет).

    `deleted_paths` — rel_paths удалённых файлов.
    """
    delta = BslApplyDelta()

    # ---- 1. Added/changed files: per-routine diff ----
    for pbf in parsed:
        rel = pbf.file_path
        old_art = old_artifacts.get(rel)
        old_routines_by_id: Dict[str, Dict[str, Any]] = {}
        old_module_ids: Set[str] = set()
        if old_art is not None:
            for r in old_art.get("routines_index", []) or []:
                rid = r.get("id")
                if rid:
                    old_routines_by_id[rid] = r
            old_module_ids = set(old_art.get("module_ids", []) or [])

        new_routines = pbf.routines or []
        new_index = pbf.routines_index or []
        new_routines_by_id: Dict[str, Dict[str, Any]] = {}
        for r in new_index:
            rid = r.get("id")
            if rid:
                new_routines_by_id[rid] = r

        # full routine dicts по rid (для load_bsl_signatures)
        new_full_by_id: Dict[str, Dict[str, Any]] = {}
        for r in new_routines:
            rid = r.get("id")
            if rid:
                new_full_by_id[rid] = r

        file_delta = BslFileDelta(rel_path=rel)
        any_real_change = False

        # Added/unchanged/line_only/doc_changed/body_changed/signature_changed
        for rid, new_idx_r in new_routines_by_id.items():
            old_idx_r = old_routines_by_id.get(rid)
            if old_idx_r is None:
                cls = "added"
            else:
                cls = _classify(old_idx_r, new_idx_r)
            file_delta.routine_deltas.append(
                RoutineDelta(rid=rid, cls=cls, new=new_idx_r, old=old_idx_r)
            )
            if cls == "unchanged":
                continue
            any_real_change = True
            full = new_full_by_id.get(rid)
            if full is not None:
                delta.routines_to_upsert.append(full)
            # CALLS / code embedding impact (unchanged semantics):
            if cls == "body_changed":
                delta.code_embeddings_to_clear.append(rid)
                delta.calls_affected_callers.append(rid)
            elif cls == "signature_changed":
                delta.code_embeddings_to_clear.append(rid)
                # старый target → class (a) callers lookup
                delta.calls_class_a_old_targets.append(rid)
                delta.calls_affected_callers.append(rid)
                # новый target
                delta.new_routine_targets.add(_routine_target_key(new_idx_r))
            elif cls == "added":
                delta.calls_affected_callers.append(rid)
                delta.new_routine_targets.add(_routine_target_key(new_idx_r))
            # doc_changed, line_only: no CALLS/code impact

            # Doc embedding invalidation — ортогональная проверка, независимая от cls.
            # Чистим embedding только если изменился именно doc_description,
            # а не doc_params_text / doc_return_text / signature / body.
            if cls not in ("added", "line_only") and old_idx_r is not None:
                if _should_clear_doc_embedding(old_idx_r, new_idx_r):
                    delta.doc_embeddings_to_clear.append(rid)
            elif cls == "added":
                if full and (full.get("doc_description") or "").strip():
                    delta.routine_doc_repass_ids.add(rid)

        # Deleted routines (rid в old, но нет в new)
        for rid, old_idx_r in old_routines_by_id.items():
            if rid in new_routines_by_id:
                continue
            file_delta.routine_deltas.append(
                RoutineDelta(rid=rid, cls="deleted", new=None, old=old_idx_r)
            )
            any_real_change = True
            delta.routine_ids_to_delete.append(rid)
            delta.calls_class_a_old_targets.append(rid)

        # Modules: upsert все, что описаны в parsed (для added — создание; для
        # changed — MERGE, без удаления DECLARES). Если file pbf.module None
        # (CommonModule), модуль не upsert-ится.
        if pbf.module:
            delta.modules_to_upsert.append(pbf.module)
            mid = pbf.module.get("id")
            if mid:
                file_delta.new_module_ids.append(mid)
                if mid in old_module_ids:
                    old_module_ids.discard(mid)
        # Module osiротевшие (были в old artifact, но не оказались в new) → delete.
        # Этот случай возможен, когда в файле всё было удалено или module identity
        # изменилась (редко, но для покрытия).
        for stale_mid in old_module_ids:
            delta.module_ids_to_delete.append(stale_mid)
            file_delta.removed_module_ids.append(stale_mid)

        # Declares / common_declares — collect только для files, у которых были
        # реальные изменения (added/upsert routines). Для full-unchanged файла
        # нет смысла переписывать DECLARES (loader делает MERGE, но это шум).
        if any_real_change:
            # Учитываем только declares для rid'ов, которые мы upsert-ли в этот пас.
            upserted_rids = {
                rd.rid for rd in file_delta.routine_deltas
                if rd.cls in ("line_only", "doc_changed", "body_changed", "signature_changed", "added")
            }
            for d in pbf.declares or []:
                if d.get("routine_id") in upserted_rids:
                    delta.declares_to_upsert.append(d)
            for d in pbf.common_declares or []:
                if d.get("routine_id") in upserted_rids:
                    delta.common_declares_to_upsert.append(d)

        # Build new_artifact для persist (sidecar обновляется всегда — нужен
        # актуальный snapshot для scoped CALLS feeding следующего cycle).
        new_artifact = BSLFileArtifact(
            source_scope="",  # source_scope заполняется в orchestrator (зависит от phase 2/3 scope)
            config_name=_first_config_name(pbf),
            rel_path=rel,
            content_hash=pbf.content_hash,
            routine_ids=[r.get("id") for r in new_routines if r.get("id")],
            module_ids=[pbf.module["id"]] if pbf.module and pbf.module.get("id") else [],
            routines_index=list(new_index),
            callsites=list(pbf.callsites or []),
            form_links=list(pbf.form_links or []),
        )
        file_delta.new_artifact = new_artifact
        delta.file_deltas.append(file_delta)

        # CodeSearchDelta accumulation
        for rd in file_delta.routine_deltas:
            if rd.cls in ("body_changed", "signature_changed", "added"):
                delta.code_search_delta.added_or_changed_routine_ids.add(rd.rid)
            elif rd.cls == "deleted":
                delta.code_search_delta.deleted_routine_ids.add(rd.rid)
            elif rd.cls == "line_only":
                delta.code_search_delta.metadata_only_routine_ids.add(rd.rid)
        if any_real_change:
            delta.code_search_delta.affected_rel_paths.add(rel)

    # ---- 2. Deleted files ----
    for rel in deleted_paths:
        old_art = old_artifacts.get(rel)
        if old_art is None:
            # Нет sidecar baseline → деgrade-gracefully: file_path-based delete
            # выполнит orchestrator через delete_bsl_by_file_paths. Здесь мы
            # ничего не можем добавить в delta (нет routine_ids).
            continue
        file_delta = BslFileDelta(rel_path=rel)
        old_rids = list(old_art.get("routine_ids", []) or [])
        old_mids = list(old_art.get("module_ids", []) or [])
        for rid in old_rids:
            delta.routine_ids_to_delete.append(rid)
            delta.calls_class_a_old_targets.append(rid)
            file_delta.routine_deltas.append(
                RoutineDelta(rid=rid, cls="deleted", new=None, old=None)
            )
            delta.code_search_delta.deleted_routine_ids.add(rid)
        for mid in old_mids:
            delta.module_ids_to_delete.append(mid)
            file_delta.removed_module_ids.append(mid)
        delta.code_search_delta.affected_rel_paths.add(rel)
        delta.file_deltas.append(file_delta)

    return delta


def _first_config_name(pbf: Any) -> str:
    """Берём config_name из первого routine (все routines одного файла имеют один cfg)."""
    if pbf.routines:
        return pbf.routines[0].get("config_name") or ""
    return ""
