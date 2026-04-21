# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Unified logging for Weaviate MCP and kg tool usage

Logs queries to:
- Weaviate MCP searches/operations
- kg-search, kg-info, kg-sync tool usage
- Per-project operation tracking
"""

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

# Log files
LOG_DIR = Path(__file__).parent
QUERY_LOG = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_queries.jsonl"
TOOL_USAGE_LOG = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}_tool_usage.jsonl"


class QueryLogger:
    """Log Weaviate MCP queries with metadata"""

    @staticmethod
    def log_search(
        query: str,
        collection: str,
        limit: int = 5,
        result_count: int = 0,
        duration_ms: float = 0,
        source: str = "weaviate-mcp",
        success: bool = True,
        error: Optional[str] = None,
        filters: Optional[dict] = None,
        project: Optional[str] = None
    ):
        """Log a search query"""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "search",
            "source": source,
            "project": project or "claude-orchestrator",
            "query": query,
            "collection": collection,
            "limit": limit,
            "filters": filters or {},
            "result_count": result_count,
            "duration_ms": duration_ms,
            "success": success,
            "error": error
        }
        QueryLogger._write_log(QUERY_LOG, entry)

    @staticmethod
    def log_store(
        title: str,
        collection: str,
        chunks: int = 1,
        duration_ms: float = 0,
        source: str = "weaviate-mcp",
        success: bool = True,
        error: Optional[str] = None,
        project: Optional[str] = None
    ):
        """Log a store operation"""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "store",
            "source": source,
            "project": project or "claude-orchestrator",
            "title": title,
            "collection": collection,
            "chunks": chunks,
            "duration_ms": duration_ms,
            "success": success,
            "error": error
        }
        QueryLogger._write_log(QUERY_LOG, entry)

    @staticmethod
    def log_delete(
        source_id: str,
        collection: str,
        deleted_count: int = 0,
        duration_ms: float = 0,
        success: bool = True,
        error: Optional[str] = None,
        project: Optional[str] = None
    ):
        """Log a delete operation"""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "type": "delete",
            "source": "weaviate-mcp",
            "project": project or "claude-orchestrator",
            "source_id": source_id,
            "collection": collection,
            "deleted_count": deleted_count,
            "duration_ms": duration_ms,
            "success": success,
            "error": error
        }
        QueryLogger._write_log(QUERY_LOG, entry)

    @staticmethod
    def _write_log(log_file: Path, entry: dict):
        """Write entry to JSONL log file"""
        try:
            with open(log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            logging.error(f"Failed to write query log: {e}")


class ToolUsageLogger:
    """Log kg tool usage (kg-search, kg-info, kg-sync)"""

    @staticmethod
    def log_kg_search(
        query: str,
        result_count: int = 0,
        duration_ms: float = 0,
        success: bool = True,
        error: Optional[str] = None,
        filters: Optional[dict] = None,
        project: Optional[str] = None
    ):
        """Log kg-search usage"""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "tool": "kg-search",
            "project": project or "claude-orchestrator",
            "query": query,
            "result_count": result_count,
            "filters": filters or {},
            "duration_ms": duration_ms,
            "success": success,
            "error": error
        }
        ToolUsageLogger._write_log(TOOL_USAGE_LOG, entry)

    @staticmethod
    def log_kg_info(
        node_title: str,
        duration_ms: float = 0,
        success: bool = True,
        error: Optional[str] = None,
        project: Optional[str] = None
    ):
        """Log kg-info usage"""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "tool": "kg-info",
            "project": project or "claude-orchestrator",
            "node_title": node_title,
            "duration_ms": duration_ms,
            "success": success,
            "error": error
        }
        ToolUsageLogger._write_log(TOOL_USAGE_LOG, entry)

    @staticmethod
    def log_kg_sync(
        file_path: str,
        chunks_created: int = 0,
        duration_ms: float = 0,
        success: bool = True,
        error: Optional[str] = None,
        project: Optional[str] = None
    ):
        """Log kg-sync usage"""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "tool": "kg-sync",
            "project": project or "claude-orchestrator",
            "file_path": file_path,
            "chunks_created": chunks_created,
            "duration_ms": duration_ms,
            "success": success,
            "error": error
        }
        ToolUsageLogger._write_log(TOOL_USAGE_LOG, entry)

    @staticmethod
    def _write_log(log_file: Path, entry: dict):
        """Write entry to JSONL log file"""
        try:
            with open(log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        except Exception as e:
            logging.error(f"Failed to write tool usage log: {e}")


# Convenience functions for direct import
def log_query(**kwargs):
    """Log Weaviate query"""
    QueryLogger.log_search(**kwargs)


def log_tool_usage(**kwargs):
    """Log kg tool usage"""
    ToolUsageLogger.log_kg_search(**kwargs)
