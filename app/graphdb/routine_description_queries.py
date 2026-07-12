"""
Cypher query templates for routine description search.

This module contains the canonical Cypher queries for searching routines by doc_description.
All fulltext and vector search implementations should use these templates to ensure consistency.
"""
from typing import Optional

# Fulltext search query for routine descriptions
# IMPORTANT: This is the SINGLE SOURCE OF TRUTH for routine description fulltext search Cypher
# Used by: template_ops.py (find_routines_by_description), routine_search_service.py (hybrid search)
# Uses ftx_routine_doc_description index with Russian analyzer
# IMPORTANT: CALL ... YIELD followed by MATCH ensures guard injection works
ROUTINE_DESCRIPTION_FULLTEXT_CYPHER = """
CALL db.index.fulltext.queryNodes('ftx_routine_doc_description', $text) YIELD node, score
MATCH (node:Routine)
WITH node AS r, score
WHERE score >= $min_score
  AND (
    ($owner_qn IS NOT NULL AND toLower(r.owner_qn) = toLower($owner_qn))
    OR ($owner_qn_prefix IS NOT NULL AND toLower(r.owner_qn) STARTS WITH toLower($owner_qn_prefix))
    OR ($owner_qn IS NULL AND $owner_qn_prefix IS NULL)
  )
  AND ($routine_type IS NULL OR toLower(r.routine_type) = toLower($routine_type))
  AND ($export IS NULL OR coalesce(r.export,false) = $export)
  AND ($is_ssl_api IS NULL OR coalesce(r.is_ssl_api,false) = $is_ssl_api)
  AND ($name IS NULL OR toLower(coalesce(r.name,'')) CONTAINS toLower($name))
  AND ($directive IS NULL OR ANY(d IN r.directives WHERE toLower(d) CONTAINS toLower($directive)))
  AND (
        ($project_name IS NULL AND $project_prefix IS NULL)
        OR (
            (r.project_name IS NOT NULL AND toLower(r.project_name) = toLower($project_name))
            OR (r.qualified_name IS NOT NULL AND r.qualified_name STARTS WITH $project_prefix)
        )
      )
  AND ($config_name IS NULL OR r.config_name = $config_name)
  AND ($module_type IS NULL OR r.module_type = $module_type)
  AND (size(coalesce($owner_categories, [])) = 0 OR r.owner_category IN $owner_categories)
OPTIONAL MATCH (mod:Module)-[:DECLARES]->(r)
WITH r, score, coalesce(mod.module_type, 'CommonModule') AS module_type
WITH r, score, module_type, split(r.owner_qn, '/') AS p
WITH r, score, module_type,
     CASE
       WHEN size(p) >= 4 AND (p[size(p)-2] = 'Form' OR p[size(p)-2] = 'Command') THEN p[size(p)-4] + '.' + p[size(p)-3]
       WHEN size(p) >= 4 THEN p[size(p)-2] + '.' + p[size(p)-1]
       WHEN size(p) >= 2 THEN 'Конфигурация.' + p[size(p)-1]
       ELSE coalesce(r.owner_qn, '')
     END AS owner,
     CASE
       WHEN module_type = 'FormModule' AND size(p) >= 1 THEN p[size(p)-1]
       ELSE ''
     END AS form_name
RETURN coalesce(r.id,'') AS id,
       coalesce(r.name,'') AS name,
       coalesce(r.config_name,'') AS config_name,
       owner,
       module_type,
       coalesce(r.owner_category,'') AS owner_category,
       CASE WHEN form_name <> '' THEN form_name ELSE NULL END AS form_name,
       coalesce(r.owner_qn,'') AS owner_qn,
       coalesce(r.signature,'') AS signature,
       coalesce(r.directives, []) AS directives,
       coalesce(r.doc_description,'') AS doc_description,
       coalesce(r.doc_params_text,'') AS doc_params_text,
       coalesce(r.doc_return_text,'') AS doc_return_text,
       score
ORDER BY score DESC, name ASC
SKIP $offset
LIMIT $limit
"""

# Vector search query for routine descriptions
# Uses vec_routine_doc_description index with cosine similarity
# Returns same fields as fulltext search for consistency (except similarity instead of score)
ROUTINE_DESCRIPTION_VECTOR_CYPHER = """
CALL db.index.vector.queryNodes('vec_routine_doc_description', $limit, $embedding)
YIELD node, score AS similarity
MATCH (node:Routine)
WITH node AS r, similarity
WHERE (
  ($owner_qn IS NOT NULL AND toLower(r.owner_qn) = toLower($owner_qn))
  OR ($owner_qn_prefix IS NOT NULL AND toLower(r.owner_qn) STARTS WITH toLower($owner_qn_prefix))
  OR ($owner_qn IS NULL AND $owner_qn_prefix IS NULL)
)
  AND ($routine_type IS NULL OR toLower(r.routine_type) = toLower($routine_type))
  AND ($export IS NULL OR coalesce(r.export,false) = $export)
  AND ($is_ssl_api IS NULL OR coalesce(r.is_ssl_api,false) = $is_ssl_api)
  AND ($name IS NULL OR toLower(coalesce(r.name,'')) CONTAINS toLower($name))
  AND ($directive IS NULL OR ANY(d IN r.directives WHERE toLower(d) CONTAINS toLower($directive)))
  AND (
        ($project_name IS NULL AND $project_prefix IS NULL)
        OR (
            (r.project_name IS NOT NULL AND toLower(r.project_name) = toLower($project_name))
            OR (r.qualified_name IS NOT NULL AND r.qualified_name STARTS WITH $project_prefix)
        )
      )
  AND ($config_name IS NULL OR r.config_name = $config_name)
  AND ($module_type IS NULL OR r.module_type = $module_type)
  AND (size(coalesce($owner_categories, [])) = 0 OR r.owner_category IN $owner_categories)
OPTIONAL MATCH (mod:Module)-[:DECLARES]->(r)
WITH r, similarity, coalesce(mod.module_type, 'CommonModule') AS module_type
WITH r, similarity, module_type, split(r.owner_qn, '/') AS p
WITH r, similarity, module_type,
     CASE
       WHEN size(p) >= 4 AND (p[size(p)-2] = 'Form' OR p[size(p)-2] = 'Command') THEN p[size(p)-4] + '.' + p[size(p)-3]
       WHEN size(p) >= 4 THEN p[size(p)-2] + '.' + p[size(p)-1]
       WHEN size(p) >= 2 THEN 'Конфигурация.' + p[size(p)-1]
       ELSE coalesce(r.owner_qn, '')
     END AS owner,
     CASE
       WHEN module_type = 'FormModule' AND size(p) >= 1 THEN p[size(p)-1]
       ELSE ''
     END AS form_name
RETURN coalesce(r.id,'') AS id,
       coalesce(r.name,'') AS name,
       coalesce(r.config_name,'') AS config_name,
       owner,
       module_type,
       coalesce(r.owner_category,'') AS owner_category,
       CASE WHEN form_name <> '' THEN form_name ELSE NULL END AS form_name,
       coalesce(r.owner_qn,'') AS owner_qn,
       coalesce(r.signature,'') AS signature,
       coalesce(r.directives, []) AS directives,
       coalesce(r.doc_description,'') AS doc_description,
       coalesce(r.doc_params_text,'') AS doc_params_text,
       coalesce(r.doc_return_text,'') AS doc_return_text,
       similarity
ORDER BY similarity DESC
"""


def build_routine_description_search_cypher(
    owner_category: Optional[str] = None,
    module_type: Optional[str] = None,
    routine_type: Optional[str] = None,
    export: Optional[bool] = None,
    is_ssl_api: Optional[bool] = None,
    config_name: Optional[str] = None,
) -> str:
    """
    Build Cypher with Neo4j SEARCH clause and index-level prefilter for vec_routine_doc_description.

    All scalar filters that are filterable properties of the index go into SEARCH WHERE.
    `owner_qn`, `name`, `directive` remain post-filter (high cardinality / not filterable).

    Boolean filters (`export`, `is_ssl_api`) are detected via `is not None` so that an explicit
    False value still injects a filter (truthy-check would silently drop False).
    """
    # Neo4j SEARCH WHERE is restricted: only direct property predicates on the binding
    # variable (`r`), no function calls (toLower/coalesce). MCP tool restricts routine_type to
    # Literal["Procedure","Function"] and CYPHER_UPSERT_ROUTINE coalesces export/is_ssl_api to
    # bool — so direct equality is safe.
    search_filters = ["r.project_name = $project_name"]
    if config_name:
        search_filters.append("r.config_name = $config_name")
    if owner_category:
        search_filters.append("r.owner_category = $owner_category")
    if module_type:
        search_filters.append("r.module_type = $module_type")
    if routine_type:
        search_filters.append("r.routine_type = $routine_type")
    if export is not None:
        search_filters.append("r.export = $export")
    if is_ssl_api is not None:
        search_filters.append("r.is_ssl_api = $is_ssl_api")
    search_where = "\n      AND ".join(search_filters)

    outer_filters = ["r.project_name = $project_name"]
    if config_name:
        outer_filters.append("r.config_name = $config_name")
    # owner_qn (exact) OR owner_qn_prefix (STARTS WITH) — mirrors the fulltext template contract.
    outer_filters.append(
        "(($owner_qn IS NOT NULL AND toLower(r.owner_qn) = toLower($owner_qn))"
        " OR ($owner_qn_prefix IS NOT NULL AND toLower(r.owner_qn) STARTS WITH toLower($owner_qn_prefix))"
        " OR ($owner_qn IS NULL AND $owner_qn_prefix IS NULL))"
    )
    outer_filters.append(
        "($name IS NULL OR toLower(coalesce(r.name,'')) CONTAINS toLower($name))"
    )
    outer_filters.append(
        "($directive IS NULL OR ANY(d IN r.directives WHERE toLower(d) CONTAINS toLower($directive)))"
    )
    outer_where = "\n  AND ".join(outer_filters)

    return f"""
MATCH (r:Routine)
  SEARCH r IN (
    VECTOR INDEX vec_routine_doc_description
    FOR $embedding
    WHERE {search_where}
    LIMIT $per_leg_k
  ) SCORE AS similarity
WHERE {outer_where}
WITH r, similarity, coalesce(r.module_type, 'CommonModule') AS module_type
WITH r, similarity, module_type, split(r.owner_qn, '/') AS p
WITH r, similarity, module_type,
     CASE
       WHEN size(p) >= 4 AND (p[size(p)-2] = 'Form' OR p[size(p)-2] = 'Command') THEN p[size(p)-4] + '.' + p[size(p)-3]
       WHEN size(p) >= 4 THEN p[size(p)-2] + '.' + p[size(p)-1]
       WHEN size(p) >= 2 THEN 'Конфигурация.' + p[size(p)-1]
       ELSE coalesce(r.owner_qn, '')
     END AS owner,
     CASE
       WHEN module_type = 'FormModule' AND size(p) >= 1 THEN p[size(p)-1]
       ELSE ''
     END AS form_name
RETURN coalesce(r.id,'') AS id,
       coalesce(r.name,'') AS name,
       coalesce(r.config_name,'') AS config_name,
       owner,
       module_type,
       coalesce(r.owner_category,'') AS owner_category,
       CASE WHEN form_name <> '' THEN form_name ELSE NULL END AS form_name,
       coalesce(r.owner_qn,'') AS owner_qn,
       coalesce(r.signature,'') AS signature,
       coalesce(r.directives, []) AS directives,
       coalesce(r.doc_description,'') AS doc_description,
       coalesce(r.doc_params_text,'') AS doc_params_text,
       coalesce(r.doc_return_text,'') AS doc_return_text,
       similarity
ORDER BY similarity DESC
"""
