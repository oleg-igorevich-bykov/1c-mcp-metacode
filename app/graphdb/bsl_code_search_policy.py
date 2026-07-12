"""Search-visible coverage policy for BSL code search.

Pure functions for excluded-categories and regulated-reports policy: input
normalization, regulated-report detection, owner_categories split into
included/excluded subsets, coverage payload/fingerprint and delta between
two coverage states.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from .category_canon import CANONICAL_CATEGORIES


_REGULATED_REPORTS_REL_PATH_SUBSTR = "Reports/Регламентированн"
_CANONICAL_REPORTS_CATEGORY = "Отчеты"


def _norm_yo(s: Optional[str]) -> str:
    """ё → е and surrounding whitespace stripped (preserves case)."""
    return (s or "").strip().replace("ё", "е").replace("Ё", "Е")


# canonical lookup keyed by yo-normalized canonical name.
_CANONICAL_BY_NORM = {_norm_yo(c): c for c in CANONICAL_CATEGORIES}


def normalize_excluded_categories(raw: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """strip + dedupe + canonical mapping for ё/е variants.

    Order of first appearance is preserved. When an entry, after ё/е
    normalization, matches a known `CANONICAL_CATEGORIES` name, the
    stored value becomes the canonical spelling — otherwise the
    original input is kept. This is what makes SQL/Cypher
    `owner_category NOT IN (...)` actually match the indexed values
    (Phase A writes the canonical name with `е`).
    """
    if not raw:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if not item:
            continue
        s = str(item).strip()
        if not s:
            continue
        key = _norm_yo(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(_CANONICAL_BY_NORM.get(key, s))
    return tuple(out)


def is_regulated_report(owner_category: Optional[str], rel_path: Optional[str]) -> bool:
    """Strict detector: owner_category=="Отчеты" AND rel_path under Reports/Регламентированн*.

    `rel_path` in this project is stored as a POSIX-relative path without a
    leading slash; we look for the substring "Reports/Регламентированн"
    accordingly. owner_category is matched via ё/е normalization to tolerate
    "Отчёты" vs "Отчеты".
    """
    if owner_category is None or rel_path is None:
        return False
    if _norm_yo(owner_category) != _norm_yo(_CANONICAL_REPORTS_CATEGORY):
        return False
    return _REGULATED_REPORTS_REL_PATH_SUBSTR in rel_path


def split_owner_categories(
    requested: Optional[Sequence[str]],
    excluded: Sequence[str],
) -> Tuple[list[str], list[str]]:
    """Split a positive owner_categories request into (included, intersected_excluded).

    Comparison via ё/е normalization. When a requested name matches a
    known `CANONICAL_CATEGORIES` entry, the canonical spelling is
    substituted before it goes into either subset — downstream consumers
    (`owner_categories` positive SQL/Cypher filters, runtime notice
    payload) compare exact strings against indexed values written in
    canonical form. Unknown categories pass through unchanged.

    Order from `requested` is preserved within each subset. If
    `requested` is None/empty, returns ([], []) — callers treat that as
    default-scope.
    """
    if not requested:
        return [], []
    excluded_norm = {_norm_yo(x) for x in excluded if x}
    included: list[str] = []
    intersected: list[str] = []
    for item in requested:
        if not item:
            continue
        key = _norm_yo(item)
        canonical = _CANONICAL_BY_NORM.get(key, str(item).strip() or item)
        if key in excluded_norm:
            intersected.append(canonical)
        else:
            included.append(canonical)
    return included, intersected


VISIBILITY_POLICY_VERSION = 1


def coverage_policy(
    excluded_owner_categories: Sequence[str],
    exclude_regulated_reports: bool,
) -> dict:
    """Canonical coverage payload dict, suitable for JSON serialization.

    Input is normalized through `normalize_excluded_categories` so the
    stored payload and the runtime filters use the same canonical
    spelling (otherwise `coverage_delta` would see fake hidden/visible
    deltas across restarts).

    `visibility_policy_version` bumps the fingerprint when the
    visibility-schema implementation changes (currently 1 — introduces
    `code_embedding_visible` on vector nodes). The first start after a
    version bump trips `coverage_changed=True` with empty category /
    regulated deltas; the indexer routes that through the hidden-only
    coverage path, which performs a one-time visibility backfill via
    `_sync_code_embedding_visibility`.
    """
    normalized = list(normalize_excluded_categories(excluded_owner_categories))
    return {
        "excluded_owner_categories": sorted(normalized),
        "regulated_reports_excluded": bool(exclude_regulated_reports),
        "visibility_policy_version": VISIBILITY_POLICY_VERSION,
    }


def coverage_fingerprint(policy: Mapping) -> str:
    """sha256 over the canonical JSON of a coverage policy payload."""
    blob = json.dumps(dict(policy), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CoverageDelta:
    newly_hidden_categories: Tuple[str, ...]
    newly_visible_categories: Tuple[str, ...]
    regulated_newly_hidden: bool
    regulated_newly_visible: bool

    @property
    def has_visible(self) -> bool:
        return bool(self.newly_visible_categories) or self.regulated_newly_visible

    @property
    def has_hidden(self) -> bool:
        return bool(self.newly_hidden_categories) or self.regulated_newly_hidden

    @property
    def is_empty(self) -> bool:
        return not (self.has_visible or self.has_hidden)


def coverage_delta(prev: Optional[Mapping], new: Mapping) -> CoverageDelta:
    """Diff two coverage policies in both directions.

    `prev=None` means no stored policy yet (first run): everything that is
    active in `new` counts as newly_hidden. There is nothing for Phase B to
    do in that case because the iterator filter already applied the policy
    during the initial pass.
    """
    prev_cats = set((prev or {}).get("excluded_owner_categories") or ())
    new_cats = set((new or {}).get("excluded_owner_categories") or ())
    prev_reg = bool((prev or {}).get("regulated_reports_excluded") or False)
    new_reg = bool((new or {}).get("regulated_reports_excluded") or False)

    return CoverageDelta(
        newly_hidden_categories=tuple(sorted(new_cats - prev_cats)),
        newly_visible_categories=tuple(sorted(prev_cats - new_cats)),
        regulated_newly_hidden=(not prev_reg) and new_reg,
        regulated_newly_visible=prev_reg and (not new_reg),
    )
