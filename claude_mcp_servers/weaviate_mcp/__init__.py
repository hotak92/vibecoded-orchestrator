# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
weaviate_mcp — pip-installable package for the VibeCoded Orchestrator Weaviate MCP server.

Installing this package (pip install -e claude_mcp_servers/) makes the
weaviate_mcp namespace importable without sys.path hacks in consumer scripts.

Key sub-modules:
- weaviate_mcp.server      — FastMCP server (hybrid_search, store_knowledge_node, …)
- weaviate_mcp.chunking    — Chunker / TokenCounter for large-node splitting
- weaviate_mcp.code_truncation — code truncation helpers for embeddings
- weaviate_mcp.query_logger    — per-query telemetry logger

Public symbols re-exported here are the ones most commonly imported by
consumer scripts; the full API lives in the individual sub-modules.
"""

from .chunking import Chunker, TokenCounter  # noqa: F401

__all__ = [
    "Chunker",
    "TokenCounter",
]
