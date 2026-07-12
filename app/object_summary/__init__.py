"""Object summary feature package.

Builds compact LLM-friendly profiles of 1C metadata objects, sends them to a
dedicated LLM channel and stores the resulting structured summary on a bind
mount; Neo4j keeps only the path, the embedding and the search text.
"""
