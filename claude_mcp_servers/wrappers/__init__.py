# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Wrapper MCPs — a shared layer that proxies upstream MCPs and filters
their tool surface per-project.

This package is the Phase 1.2 (diagrams-integration plan, 2026-05-24)
landing of the wrapper-MCP architecture that the plan promotes to all
MCPs in Phase 4. The base class lives at :mod:`.._base`; per-MCP
proxies (mermaid_proxy, future excalidraw_proxy, future
weaviate_kg_wrapper, …) subclass it.

See ``.claude/context/plans/diagrams-integration-excalidraw-mermaid-2026-05-24.md``
§3 Phase 1 item 5 + §3 Phase 4.
"""
