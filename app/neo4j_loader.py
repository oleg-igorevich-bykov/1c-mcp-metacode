"""
Neo4j database loader facade for 1C metadata.

This module keeps the public class Neo4jLoader available at the same import path,
but delegates the implementation to modular mixins under app/graphdb.

Backwards compatibility:
- mcpsrv/neo4j_init.py uses `from neo4j_loader import Neo4jLoader` — this continues to work.
- Public methods and behavior are preserved via mixin composition.
"""

from __future__ import annotations

import logging

# Import mixins as top-level modules relative to current package layout.
# NOTE: `neo4j_loader` is imported as a top-level module elsewhere (no package prefix),
# so relative imports like ".graphdb" are avoided here to keep compatibility.
from graphdb import (
    Neo4jClient,
    IndexManagementMixin,
    TypeRefMixin,
    GuidEnrichmentMixin,
    RowsBuilderMixin,
    ConfigLoaderMixin,
    PredefinedLoaderMixin,
    HelpLoaderMixin,
    RoleRightsLoaderMixin,
    EventSubscriptionsLoaderMixin,
    FormsLoaderMixin,
    BSLLoaderMixin,
    QueryApiMixin,
    IncrementalLoaderMixin,
    SslApiMarkerMixin,
)

logger = logging.getLogger(__name__)


class Neo4jLoader(
    Neo4jClient,
    IndexManagementMixin,
    GuidEnrichmentMixin,
    TypeRefMixin,
    RowsBuilderMixin,
    ConfigLoaderMixin,
    PredefinedLoaderMixin,
    HelpLoaderMixin,
    RoleRightsLoaderMixin,
    EventSubscriptionsLoaderMixin,
    FormsLoaderMixin,
    BSLLoaderMixin,
    SslApiMarkerMixin,
    QueryApiMixin,
    IncrementalLoaderMixin,
):
    """
    Facade class that composes all loader capabilities.

    MRO intentionally places Neo4jClient first so its __init__ establishes connection.
    Type and GUID utilities appear before RowsBuilder so helpers are available.
    """
    pass
