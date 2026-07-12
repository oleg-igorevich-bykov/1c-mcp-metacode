"""
JSON encoding utilities for Neo4j result rows.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from neo4j import graph

from config import settings

try:
    from toon_format import encode as _toon_encode
    _TOON_AVAILABLE = True
except ImportError:
    _TOON_AVAILABLE = False


class Neo4jNodeEncoder(json.JSONEncoder):
    """Custom JSON encoder for Neo4j Node and Relationship objects (verbose)."""

    def default(self, obj):  # noqa: D401
        """Convert Neo4j objects to JSON-serializable format."""
        if isinstance(obj, graph.Node):
            # Convert Node to dictionary with its properties
            return {
                "element_id": obj.element_id if hasattr(obj, "element_id") else str(obj.id),
                "labels": list(obj.labels),
                "properties": dict(obj.items()),
            }
        elif isinstance(obj, graph.Relationship):
            # Convert Relationship to dictionary
            return {
                "element_id": obj.element_id if hasattr(obj, "element_id") else str(obj.id),
                "type": obj.type,
                "properties": dict(obj.items()),
                "start_node": obj.start_node.element_id
                if hasattr(obj.start_node, "element_id")
                else str(obj.start_node.id),
                "end_node": obj.end_node.element_id if hasattr(obj.end_node, "element_id") else str(obj.end_node.id),
            }
        elif isinstance(obj, frozenset):
            # Convert frozenset to list
            return list(obj)
        elif isinstance(obj, set):
            # Convert set to list
            return list(obj)
        # Let the base class handle other types or raise TypeError
        return super().default(obj)


class CompactNeo4jEncoder(json.JSONEncoder):
    """
    More compact encoder:
    - Node: only properties (no labels/ids)
    - Relationship: keep type and properties, drop ids and endpoints
    - Sets: convert to list
    """

    def default(self, obj):  # noqa: D401
        """Convert Neo4j objects to compact JSON-serializable format."""
        if isinstance(obj, graph.Node):
            # Only properties of node
            return dict(obj.items())
        elif isinstance(obj, graph.Relationship):
            data = dict(obj.items())
            rel_type = getattr(obj, "type", None)
            if rel_type:
                data = {"type": rel_type, **data}
            return data
        elif isinstance(obj, (frozenset, set)):
            return list(obj)
        return super().default(obj)


def results_to_json(results: List[Dict], compact: bool = True) -> str:
    """
    Serialize results to JSON with optional compact Neo4j encoding and optional pretty-print when debug.
    """
    encoder = CompactNeo4jEncoder if (compact and getattr(settings, "response_compact_nodes", True)) else Neo4jNodeEncoder
    separators = (",", ":") if getattr(settings, "response_json_compact", True) and not settings.enable_debug else (", ", ": ")
    try:
        return json.dumps(results, ensure_ascii=False, separators=separators, cls=encoder)
    except Exception:
        # Fallback without custom encoder if custom encoding fails
        return json.dumps(results, ensure_ascii=False, separators=separators)


def results_to_toon(results: List[Dict]) -> str:
    """Serialize results to TOON format. Neo4j objects converted via JSON round-trip."""
    if not _TOON_AVAILABLE:
        return results_to_json(results, compact=True)
    try:
        plain = json.loads(
            json.dumps(results, ensure_ascii=False, cls=CompactNeo4jEncoder)
        )
        return _toon_encode(plain)
    except Exception:
        return results_to_json(results, compact=True)


__all__ = ["Neo4jNodeEncoder", "CompactNeo4jEncoder", "results_to_json", "results_to_toon"]