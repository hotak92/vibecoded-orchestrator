# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Shared helpers for the orchestrator's MCP servers.

This package holds small DRY utilities that more than one MCP server
needs. Kept deliberately tiny: each MCP otherwise lives in its own
top-level package under ``claude_mcp_servers/`` and the MCPs are run as
``python <install>/claude_mcp_servers/<name>/server.py`` from
``~/.claude.json``.
"""
