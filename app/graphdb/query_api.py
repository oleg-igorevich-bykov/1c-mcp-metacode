"""
QueryApiMixin: ad-hoc query helpers (read/write) and project-scoped statistics.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
import logging

from neo4j.exceptions import Neo4jError
from config import settings

logger = logging.getLogger(__name__)


class QueryApiMixin:
    def execute_query(self, cypher_query: str, parameters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Execute a Cypher query and return results (no guard, use carefully)"""
        try:
            with self.driver.session(database=settings.neo4j_database) as session:
                result = session.run(cypher_query, parameters or {})
                records = [dict(record) for record in result]
                return records
        except Neo4jError as e:
            logger.error("Query execution failed: %s", str(e))
            raise

    def execute_query_readonly(self, cypher_query: str, parameters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """Execute a Cypher query in a read transaction and return results"""
        try:
            driver = self._get_read_driver()
            with driver.session(database=settings.neo4j_database, fetch_size=settings.neo4j_fetch_size) as session:
                def _work(tx):
                    res = tx.run(cypher_query, parameters or {})
                    return [dict(r) for r in res]
                try:
                    return session.execute_read(lambda tx: _work(tx))  # neo4j 5+
                except AttributeError:
                    return session.read_transaction(lambda tx: _work(tx))  # neo4j 4.x
        except Neo4jError as e:
            logger.error("Read-only query execution failed: %s", str(e))
            raise

    def get_statistics(self, project_name: Optional[str] = None) -> Dict[str, int]:
        """Get database statistics scoped to the specified project (defaults to settings.project_name)."""
        with self.driver.session(database=settings.neo4j_database, fetch_size=settings.neo4j_fetch_size) as session:
            stats: Dict[str, int] = {}

            prj = project_name or settings.project_name
            prefix = prj + '/'

            # Keep a stable order for logging
            node_types = [
                'Project', 'Configuration', 'MetadataCategory', 'MetadataObject',
                'JournalGraph', 'Form', 'FormControl', 'FormEvent', 'FormEventAction', 'FormAttribute',
                'Command', 'Layout', 'Attribute', 'TabularPart', 'Resource',
                'Dimension', 'EnumValue', 'Characteristic', 'UrlTemplate',
                'UrlMethod', 'AccountingFlag', 'DimensionAccountingFlag'
            ]

            # Project (exact by name)
            res = session.run("MATCH (p:Project {name: $name}) RETURN count(p) AS count", name=prj)
            stats['Project'] = res.single()['count']

            # All other labels by qualified_name prefix "<project>/..."
            for node_type in node_types:
                if node_type == 'Project':
                    continue
                cypher = f"""
                MATCH (n:{node_type})
                WHERE n.qualified_name IS NOT NULL AND n.qualified_name STARTS WITH $prefix
                RETURN count(n) AS count
                """
                result = session.run(cypher, prefix=prefix)
                stats[node_type] = result.single()['count']

            # EventSubscription nodes are stored as MetadataObject with category_name = 'ПодпискиНаСобытия'
            res = session.run(
                "MATCH (es:MetadataObject) WHERE es.qualified_name STARTS WITH $prefix AND es.category_name = 'ПодпискиНаСобытия' RETURN count(es) AS count",
                prefix=prefix,
            )
            stats['EventSubscription'] = res.single()['count']

            # BSL-specific counts (Module/Routine do not use qualified_name; use project_name)
            res = session.run("MATCH (m:Module) WHERE m.project_name = $prj RETURN count(m) AS count", prj=prj)
            stats['Module'] = res.single()['count']
            res = session.run("MATCH (r:Routine) WHERE r.project_name = $prj RETURN count(r) AS count", prj=prj)
            stats['Routine'] = res.single()['count']

            # Relationships: between nodes of the project (by prefix) plus edges touching the Project node itself
            rel_cypher = """
            OPTIONAL MATCH (p:Project {name: $name})
            WITH p
            OPTIONAL MATCH (a)-[r]->(b)
            WHERE
              (
                (
                  (a.qualified_name IS NOT NULL AND a.qualified_name STARTS WITH $prefix)
                  OR (a.project_name IS NOT NULL AND toLower(a.project_name) = toLower($name))
                )
                AND
                (
                  (b.qualified_name IS NOT NULL AND b.qualified_name STARTS WITH $prefix)
                  OR (b.project_name IS NOT NULL AND toLower(b.project_name) = toLower($name))
                )
              )
              OR (p IS NOT NULL AND (a = p OR b = p))
            RETURN count(DISTINCT r) AS count
            """
            result = session.run(rel_cypher, name=prj, prefix=prefix)
            rec = result.single()
            stats['Relationships'] = (rec or {}).get('count', 0)

            # Total nodes = project node (if exists) + nodes with project-qualified prefix
            total_nodes_cypher = """
            OPTIONAL MATCH (p:Project {name: $name})
            WITH CASE WHEN p IS NULL THEN 0 ELSE 1 END AS projectCount
            OPTIONAL MATCH (n)
            WHERE
              (n.qualified_name IS NOT NULL AND n.qualified_name STARTS WITH $prefix)
              OR (n.project_name IS NOT NULL AND toLower(n.project_name) = toLower($name))
            WITH projectCount, count(DISTINCT n) AS nodeCount
            RETURN projectCount + nodeCount AS count
            """
            result = session.run(total_nodes_cypher, name=prj, prefix=prefix)
            rec = result.single()
            stats['TotalNodes'] = (rec or {}).get('count', 0)

            return stats