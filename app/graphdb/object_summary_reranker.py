"""
Object summary rerank document builder for `find_objects_by_summary`.

The HTTP transport, retry loop and singleton live in `graphdb.reranker`.
This module owns only the object_summary-specific document format:
object metadata prefix + plain-text body from `human_summary`.

The rerank document contract is intentionally independent from the embedding
document contract (`object_summary.render.build_embedding_document`) — both
are ranking stages with their own freedom to evolve. Format helpers are kept
local to this module so the search layer never depends on private API of
the render layer.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_object_summary_rerank_document(
    summary_payload: Optional[Dict[str, Any]],
    *,
    category: str = "",
    name: str = "",
    config_name: str = "",
) -> str:
    """
    Build the document string sent to the reranker for one object summary candidate.

    Returns an empty string if the payload is missing/empty/malformed — the
    caller drops such candidates from the rerank batch and routes them to the
    hybrid tail with their original score.
    """
    if not isinstance(summary_payload, dict) or not summary_payload:
        return ""

    human = summary_payload.get("human_summary")
    if not isinstance(human, dict):
        return ""

    body_blocks: List[str] = []

    title = _clean(human.get("title"))
    if title:
        body_blocks.append(title)

    for key in ("core_idea", "data_scope"):
        text = _clean(human.get(key))
        if text:
            body_blocks.append(text)

    for key in ("capabilities", "usage_scenarios"):
        items = _titled_items_plain(human.get(key))
        if items:
            body_blocks.append("\n".join(items))

    effects = _clean(human.get("effects"))
    if effects:
        body_blocks.append(effects)

    if not body_blocks:
        # Prefix-only documents (object metadata without summary content) are
        # not usable for rerank — the caller must drop such candidates and
        # route them to head_without_rerank_score with their hybrid score.
        return ""

    blocks: List[str] = []
    prefix = _build_prefix(category=category, name=name, config_name=config_name)
    if prefix:
        blocks.append(prefix)
    blocks.extend(body_blocks)

    return "\n\n".join(blocks).strip()


def _build_prefix(*, category: str, name: str, config_name: str) -> str:
    cat = _clean(category)
    nm = _clean(name)
    cfg = _clean(config_name)
    if not cat and not nm:
        return ""
    base = f"{cat}.{nm}" if cat and nm else (cat or nm)
    return f"{base} ({cfg})" if cfg else base


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _titled_items_plain(items: Any) -> List[str]:
    out: List[str] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        description = _clean(item.get("description"))
        if title and description:
            out.append(f"{title}. {description}")
        elif title:
            out.append(title)
        elif description:
            out.append(description)
    return out
