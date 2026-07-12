"""
Cypher query templates for metadata description search.

This module contains the canonical Cypher queries for searching metadata objects by description.
All fulltext and vector search implementations should use these templates to ensure consistency.
"""
from typing import Optional

# Fulltext search query for metadata descriptions
# IMPORTANT: This is the SINGLE SOURCE OF TRUTH for metadata description fulltext search Cypher
# Used by: template_ops.py (search_metadata_by_description), metadata_search_service.py (hybrid search)
# Uses ftx_metadataobject_description index with Russian analyzer
# Indexes multiple fields: name, Синоним, Комментарий, Справка, Пояснение
METADATA_DESCRIPTION_FULLTEXT_CYPHER = """
CALL db.index.fulltext.queryNodes('ftx_metadataobject_description', $text) YIELD node, score
MATCH (node:MetadataObject)
WITH node AS m, score
WHERE score >= $min_score
  AND (size($categories) = 0 OR toLower(m.category_name) IN [c IN $categories | toLower(c)])
  AND (
        ($project_name IS NULL AND $project_prefix IS NULL)
        OR (
            (m.project_name IS NOT NULL AND toLower(m.project_name) = toLower($project_name))
            OR (m.qualified_name IS NOT NULL AND m.qualified_name STARTS WITH $project_prefix)
        )
      )
RETURN m.config_name AS config_name,
       m.category_name AS category,
       m.name AS name,
       m.qualified_name AS qualified_name,
       coalesce(m.`Синоним`, '') AS synonym,
       coalesce(m.`Комментарий`, '') AS comment,
       coalesce(m.`Справка`, '') AS help_text,
       coalesce(m.`Пояснение`, '') AS explanation,
       score
ORDER BY score DESC, category, name
SKIP $offset
LIMIT $limit
"""

def build_metadata_description_fulltext_cypher(config_name: Optional[str] = None) -> str:
    """Return METADATA_DESCRIPTION_FULLTEXT_CYPHER with optional config_name filter injected."""
    base = METADATA_DESCRIPTION_FULLTEXT_CYPHER
    if config_name:
        base = base.replace(
            "\nRETURN",
            "\n  AND m.config_name = $config_name\nRETURN",
            1,
        )
    return base


# Vector search query for metadata descriptions
# Uses vec_metadataobject_description index with cosine similarity
# Filters by category_name, project_name and config_name using WHERE clause (post-filter with oversampling)
# IMPORTANT: Project guard in WHERE clause prevents cross-project data leakage
# Returns same fields as fulltext search for consistency (except similarity instead of score)
METADATA_DESCRIPTION_VECTOR_CYPHER = """
CALL db.index.vector.queryNodes('vec_metadataobject_description', $limit, $embedding)
YIELD node, score AS similarity
MATCH (node:MetadataObject)
WITH node AS m, similarity
WHERE (size($categories) = 0 OR toLower(m.category_name) IN [c IN $categories | toLower(c)])
  AND (
        ($project_name IS NULL AND $project_prefix IS NULL)
        OR (
            (m.project_name IS NOT NULL AND toLower(m.project_name) = toLower($project_name))
            OR (m.qualified_name IS NOT NULL AND m.qualified_name STARTS WITH $project_prefix)
        )
      )
  AND ($config_name IS NULL OR m.config_name = $config_name)
RETURN m.config_name AS config_name,
       m.category_name AS category,
       m.name AS name,
       m.qualified_name AS qualified_name,
       coalesce(m.`Синоним`, '') AS synonym,
       coalesce(m.`Комментарий`, '') AS comment,
       coalesce(m.`Справка`, '') AS help_text,
       coalesce(m.`Пояснение`, '') AS explanation,
       similarity
ORDER BY similarity DESC
"""


def build_metadata_description_search_cypher(
    category_name: Optional[str] = None,
    config_name: Optional[str] = None,
) -> str:
    """
    Build Cypher with Neo4j SEARCH clause and index-level prefilter for vec_metadataobject_description.

    All scalar filters (project_name, config_name, category_name) go inside SEARCH WHERE so the
    vector index returns only matching nodes (no post-filter oversampling needed).

    A duplicate project_name + config_name guard is applied in the outer WHERE as a defensive
    check against index metadata drift.

    `category_name` is per-leg: callers wanting fan-out by multiple categories must invoke
    this builder once per category and merge results in Python (SEARCH does not allow IN).
    """
    search_filters = ["m.project_name = $project_name"]
    if config_name:
        search_filters.append("m.config_name = $config_name")
    if category_name:
        search_filters.append("m.category_name = $category_name")
    search_where = "\n      AND ".join(search_filters)

    outer_filters = ["m.project_name = $project_name"]
    if config_name:
        outer_filters.append("m.config_name = $config_name")
    outer_where = "\n  AND ".join(outer_filters)

    return f"""
MATCH (m:MetadataObject)
  SEARCH m IN (
    VECTOR INDEX vec_metadataobject_description
    FOR $embedding
    WHERE {search_where}
    LIMIT $per_leg_k
  ) SCORE AS similarity
WHERE {outer_where}
RETURN m.config_name AS config_name,
       m.category_name AS category,
       m.name AS name,
       m.qualified_name AS qualified_name,
       coalesce(m.`Синоним`, '') AS synonym,
       coalesce(m.`Комментарий`, '') AS comment,
       coalesce(m.`Справка`, '') AS help_text,
       coalesce(m.`Пояснение`, '') AS explanation,
       similarity
ORDER BY similarity DESC
"""
