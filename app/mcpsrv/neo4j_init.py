"""
Neo4j initialization helpers.
"""

from __future__ import annotations

import logging
from typing import Optional

from neo4j_loader import Neo4jLoader
from config import settings

neo4j_loader: Optional[Neo4jLoader] = None


def initialize_neo4j() -> bool:
    """Initialize Neo4j connection (idempotent)."""
    global neo4j_loader
    if neo4j_loader is not None:
        return True
    try:
        neo4j_loader = Neo4jLoader()
        logging.debug("Neo4j connection established")
        return True
    except Exception as e:
        logging.error(f"Failed to connect to Neo4j: {str(e)}")
        neo4j_loader = None
        return False


def get_loader() -> Optional[Neo4jLoader]:
    """Get current Neo4jLoader instance (may be None)."""
    return neo4j_loader


def check_neo4j_connection() -> bool:
    """Check if Neo4j is connected."""
    global neo4j_loader
    if neo4j_loader:
        try:
            neo4j_loader.execute_query_readonly("RETURN 1")
            return True
        except Exception:
            return False
    return False