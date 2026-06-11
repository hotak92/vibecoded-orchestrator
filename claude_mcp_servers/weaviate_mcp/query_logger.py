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
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Optional

# Log files.
#
# v0.2.54 (Track A1 install hygiene): logs used to land in the installed
# package directory (`Path(__file__).parent`), polluting the repo/venv
# tree and breaking on read-only installs. They now go to the VCT state
# dir (`$VCT_STATE_DIR` or `~/.vct`) under `logs/weaviate_mcp/`, with
# `$VCT_QUERY_LOG_DIR` as an explicit override for tests.


def _resolve_log_dir() -> Path:
    override = os.environ.get("VCT_QUERY_LOG_DIR", "").strip()
    if override:
        return Path(override)
    # State-root resolution goes through the MCP-isolation mirror
    # `_lib.update_gate._vct_root_dir` (the documented in-package mirror
    # of vco_lib.paths.vct_root_dir — MCP servers run from
    # claude_mcp_servers/.venv which doesn't carry vco_lib). The
    # consolidation gate in tests/test_vct_root_dir_consolidation.py
    # forbids inline ~/.vct reconstruction outside that mirror.
    try:
        import sys

        _pkg_parent = Path(__file__).resolve().parent.parent
        if str(_pkg_parent) not in sys.path:
            sys.path.insert(0, str(_pkg_parent))
        from _lib.update_gate import _vct_root_dir  # type: ignore

        root = _vct_root_dir()
    except Exception:  # noqa: BLE001 — logging must never break the MCP
        state_dir = os.environ.get("VCT_STATE_DIR", "").strip()
        root = Path(state_dir) if state_dir else Path(os.path.expanduser("~/.vct"))
    return root / "logs" / "weaviate_mcp"


LOG_DIR = _resolve_log_dir()
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            log_file.parent.mkdir(parents=True, exist_ok=True)
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
            log_file.parent.mkdir(parents=True, exist_ok=True)
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
