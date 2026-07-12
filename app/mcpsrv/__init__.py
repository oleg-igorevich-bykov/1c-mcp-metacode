"""
MCP server package (modularized).

Submodules:
- audit: audit logging helpers
- encoding: Neo4j JSON encoders and result serialization
- summarization: result filtering utilities and simple formatting
- resolvers: shared ref-resolution helpers for typed tools
- queries: parameterized Cypher utilities for typed tools
- typed_tools: 16 typed MCP tools with JSON Schema parameters
- neo4j_init: Neo4j initialization helpers
- server: FastMCP wiring and run_server entry
"""
__all__ = [
    "audit",
    "encoding",
    "summarization",
    "resolvers",
    "queries",
    "typed_tools",
    "neo4j_init",
    "server",
]