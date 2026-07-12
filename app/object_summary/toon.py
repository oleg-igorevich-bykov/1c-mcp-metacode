"""Normalise an `object_profile` dict for TOON encoding and emit a TOON string.

The runtime format for the LLM is TOON. JSON is not used: TOON gives shorter
token counts on the repeated-row shapes we get from grouped relationships,
routines and form controls.

`normalize_for_toon` restores tabular shape — every dict in a list gets the
union of keys so the TOON encoder can emit a table instead of a sparse YAML.
Empty cells become empty strings (TOON skips them on the wire but the shape
must be uniform).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)

try:
    from toon_format import encode as _toon_encode
    _TOON_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on deployment
    _TOON_AVAILABLE = False
    _toon_encode = None  # type: ignore[assignment]


_PRIMITIVE = (str, int, float, bool, type(None))


def _is_primitive(value: Any) -> bool:
    return isinstance(value, _PRIMITIVE)


def _is_primitive_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_primitive(item) for item in value)


def _join_primitives(value: List[Any]) -> str:
    return "|".join("" if item is None else str(item) for item in value)


def _ordered_union_keys(items: Iterable[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []
    seen: set[str] = set()
    for item in items:
        for key in item:
            if key in seen:
                continue
            seen.add(key)
            keys.append(key)
    return keys


def normalize_for_toon(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize_for_toon(item) for key, item in value.items()}

    if isinstance(value, list):
        normalized = [normalize_for_toon(item) for item in value]
        if not normalized or not all(isinstance(item, dict) for item in normalized):
            return normalized

        keys = _ordered_union_keys(normalized)
        if not keys:
            return normalized

        rows: List[Dict[str, Any]] = []
        for item in normalized:
            row: Dict[str, Any] = {}
            can_flatten = True
            for key in keys:
                cell = item.get(key, "")
                if _is_primitive_list(cell):
                    cell = _join_primitives(cell)
                if not _is_primitive(cell):
                    can_flatten = False
                row[key] = cell
            rows.append(row)
        return rows if can_flatten else normalized

    return value


def encode_profile(
    profile: Dict[str, Any], *, delimiter: str = ",", indent: int = 2
) -> str:
    """Return the TOON string for `profile`.

    Falls back to a compact JSON dump if `toon_format` is not importable in
    this environment — better than crashing the pipeline, and the LLM still
    receives valid structured data. Callers that need TOON guaranteed should
    check `_TOON_AVAILABLE`.
    """
    normalised = normalize_for_toon(profile)
    if _TOON_AVAILABLE and _toon_encode is not None:
        return _toon_encode(normalised, {"indent": indent, "delimiter": delimiter})

    logger.warning("toon_format is not available; falling back to compact JSON for object_profile")
    import json
    return json.dumps(normalised, ensure_ascii=False, separators=(",", ":"))
