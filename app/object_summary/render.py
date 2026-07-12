"""Render the validated `object_summary` payload into derived artifacts.

Three derived builders sit here. Their output is tracked by version constants
in `constants.py`; bumping a builder version triggers a cheap S0 rebuild
without re-calling the LLM (see `object_summary_pipeline` S0 phase B).

  * `render_markdown(summary)` — full human-readable `summary.md`.
  * `build_embedding_document(summary)` — text fed into the vector embedder.
    Limitations and search terms are excluded by design — they only pollute
    the vector space.
  * `build_search_text(summary)` — text fed into the Neo4j fulltext index.
    Uses `title + phrases + keywords` so search terms are the search corpus,
    not the human prose.

All three skip empty sections — paired guarantee with the soft minimums in
`contract.py`. A thin profile produces a short summary; no "## Возможности"
heading appears in markdown if `capabilities` is empty.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


_HUMAN_SECTIONS = [
    ("core_idea", "Назначение"),
    ("data_scope", "Состав данных"),
    ("capabilities", "Возможности"),
    ("usage_scenarios", "Сценарии использования"),
    ("effects", "Результат"),
    ("uncertainties", "Ограничения"),
]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _human(summary: Dict[str, Any]) -> Dict[str, Any]:
    raw = summary.get("human_summary")
    return raw if isinstance(raw, dict) else {}


def _search(summary: Dict[str, Any]) -> Dict[str, Any]:
    raw = summary.get("search_terms")
    return raw if isinstance(raw, dict) else {}


def _titled_items_md(items: Any) -> List[str]:
    lines: List[str] = []
    if not isinstance(items, list):
        return lines
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        description = _clean(item.get("description"))
        if title and description:
            lines.append(f"- **{title}.** {description}")
        elif title:
            lines.append(f"- **{title}.**")
        elif description:
            lines.append(f"- {description}")
    return lines


def _string_list_md(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    for item in items or []:
        text = _clean(item)
        if text:
            out.append(f"- {text}")
    return out


def render_markdown(summary: Dict[str, Any]) -> str:
    """Render a human-readable `summary.md`. Skips empty sections."""
    human = _human(summary)
    search = _search(summary)

    lines: List[str] = []
    title = _clean(human.get("title"))
    if title:
        lines.append(f"# {title}")
        lines.append("")

    for key, heading in _HUMAN_SECTIONS:
        if key in ("capabilities", "usage_scenarios"):
            rendered = _titled_items_md(human.get(key))
            if rendered:
                lines.append(f"## {heading}")
                lines.append("")
                lines.extend(rendered)
                lines.append("")
            continue
        text = _clean(human.get(key))
        if not text:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        lines.append(text)
        lines.append("")

    rendered_phrases = _string_list_md(search.get("phrases", []))
    if rendered_phrases:
        lines.append("## Поисковые фразы")
        lines.append("")
        lines.extend(rendered_phrases)
        lines.append("")

    rendered_keywords = _string_list_md(search.get("keywords", []))
    if rendered_keywords:
        lines.append("## Поисковые ключевые слова")
        lines.append("")
        lines.extend(rendered_keywords)
        lines.append("")

    return ("\n".join(lines).rstrip() + "\n") if lines else ""


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


def build_embedding_document(summary: Dict[str, Any]) -> str:
    """Plain-text document used for the summary embedding.

    Includes: title, core_idea, data_scope, capabilities, usage_scenarios,
    effects. Excludes uncertainties and search terms — those distort the
    vector space toward limitation phrases and bare keywords.
    """
    human = _human(summary)
    blocks: List[str] = []

    title = _clean(human.get("title"))
    if title:
        blocks.append(title)

    for key in ("core_idea", "data_scope"):
        text = _clean(human.get(key))
        if text:
            blocks.append(text)

    for key in ("capabilities", "usage_scenarios"):
        items = _titled_items_plain(human.get(key))
        if items:
            blocks.append("\n".join(items))

    effects = _clean(human.get("effects"))
    if effects:
        blocks.append(effects)

    return "\n\n".join(blocks).strip()


def build_search_text(summary: Dict[str, Any]) -> str:
    """Compact fulltext payload used for the Neo4j fulltext index.

    Concatenates `title + phrases + keywords`. By design this is **not** the
    human prose — fulltext relevance comes from the search terms the LLM was
    asked to produce, not from `core_idea` repeating.
    """
    human = _human(summary)
    search = _search(summary)

    parts: List[str] = []
    title = _clean(human.get("title"))
    if title:
        parts.append(title)

    for value in search.get("phrases", []) or []:
        s = _clean(value)
        if s:
            parts.append(s)

    for value in search.get("keywords", []) or []:
        s = _clean(value)
        if s:
            parts.append(s)

    return "\n".join(parts).strip()
