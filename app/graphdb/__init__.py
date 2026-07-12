"""
GraphDB package: modular mixins and helpers for Neo4j loader.
This package factors the monolithic Neo4jLoader into small, cohesive modules.

Modules:
- core: Neo4jClient base (connection/session/tx helpers)
- indexes: IndexManagementMixin (constraints/indexes/fulltext)
- types: TypeRefMixin (1C 'Тип' normalization and reference extraction)
- guid: GuidEnrichmentMixin (XCF name -> GUID enrichment)
- rows_builder: RowsBuilderMixin (prepare UNWIND rows per configuration)
- config_loader: ConfigLoaderMixin (chunked configuration loading)
- predefined_loader: PredefinedLoaderMixin (Predefined.xml)
- help_loader: HelpLoaderMixin (Ext/Help/ru.html)
- role_rights_loader: RoleRightsLoaderMixin (Roles rights)
- event_loader: EventSubscriptionsLoaderMixin (EventSubscriptions)
- forms_loader: FormsLoaderMixin (Ext/Form.xml)
- bsl_loader: BSLLoaderMixin (modules/routines/signatures)
- query_api: QueryApiMixin (ad-hoc queries and statistics)
- cypher_templates: reusable Cypher constants/factories
"""
from __future__ import annotations

# Re-export commonly used mixins in a single place for convenience.
# This lets app.neo4j_loader import from app.graphdb without deep module paths.
try:
    from .core import Neo4jClient
except Exception:
    Neo4jClient = None  # populated after core.py creation

try:
    from .indexes import IndexManagementMixin
except Exception:
    IndexManagementMixin = None

try:
    from .types import TypeRefMixin
except Exception:
    TypeRefMixin = None

try:
    from .guid import GuidEnrichmentMixin
except Exception:
    GuidEnrichmentMixin = None

try:
    from .rows_builder import RowsBuilderMixin
except Exception:
    RowsBuilderMixin = None

try:
    from .config_loader import ConfigLoaderMixin
except Exception:
    ConfigLoaderMixin = None

try:
    from .predefined_loader import PredefinedLoaderMixin
except Exception:
    PredefinedLoaderMixin = None

try:
    from .help_loader import HelpLoaderMixin
except Exception:
    HelpLoaderMixin = None

try:
    from .role_rights_loader import RoleRightsLoaderMixin
except Exception:
    RoleRightsLoaderMixin = None

try:
    from .event_loader import EventSubscriptionsLoaderMixin
except Exception:
    EventSubscriptionsLoaderMixin = None

try:
    from .forms_loader import FormsLoaderMixin
except Exception:
    FormsLoaderMixin = None

try:
    from .bsl_loader import BSLLoaderMixin
except Exception:
    BSLLoaderMixin = None

try:
    from .query_api import QueryApiMixin
except Exception:
    QueryApiMixin = None

try:
    from .incremental_loader import IncrementalLoaderMixin
except Exception:
    IncrementalLoaderMixin = None

try:
    from .ssl_api_marker import SslApiMarkerMixin
except Exception:
    SslApiMarkerMixin = None