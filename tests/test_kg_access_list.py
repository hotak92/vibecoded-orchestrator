# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the multi-source KG / code-graph access matrix env vars.

Covers the Python side of P1-D (2026-05-08): the launcher GUI's access
matrix lands as `VCT_KG_ACCESS_LIST=Foo,Bar` /
`VCT_CODE_GRAPH_ACCESS_LIST=Foo,Bar` env vars, which the MCP server +
`rl_kg_search.py` consume to fan-out searches across peer KGs and
codegraphs. Pre-fix the matrix had no runtime effect — pure UI feature.

These tests pin the consumption side (the env-vars → collections-list
mapping) without spinning up a real Weaviate.

The Rust side (write_project_env_files emits these vars to the 3
install surfaces) is covered in `launcher/src-tauri` cargo tests:
- test_write_project_env_files_includes_access_list_when_peers_granted
- test_write_project_env_files_omits_access_list_when_no_peers
- populate_resolves_kg_access_peers_from_matrix
- populate_resolves_code_graph_access_peers_from_matrix
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
sys.path.insert(0, str(MCP_DIR))


def _fresh_server(env_overrides: dict[str, str]):
    """Reload weaviate_mcp.server with a fresh env. Mirrors the helper in
    test_shared_kg.py — the server reads env vars at import time and we
    need to reimport for each permutation.
    """
    for mod in list(sys.modules):
        if mod.startswith("weaviate_mcp"):
            del sys.modules[mod]
    for k, v in env_overrides.items():
        os.environ[k] = v
    return importlib.import_module("weaviate_mcp.server")


def _clear_access_env() -> None:
    """Strip access-matrix env vars + the KG/SHARED config that influences
    the helper output, so each test starts from a known baseline."""
    for k in (
        "VCT_KG_ACCESS_LIST",
        "VCT_CODE_GRAPH_ACCESS_LIST",
        "KG_COLLECTION",
        "SHARED_KG_COLLECTION",
        "DEVELOPMENT_COLLECTION",
    ):
        os.environ.pop(k, None)


class KgAccessListTests(unittest.TestCase):
    """`_kg_collections_to_search` honours VCT_KG_ACCESS_LIST."""

    def setUp(self) -> None:
        _clear_access_env()

    def test_kg_collections_to_search_includes_peers_from_access_list_env(self):
        """When VCT_KG_ACCESS_LIST is set, the helper returns
        self + shared + peers (all 3 categories) in that order, deduped,
        with each peer name resolved to `<Sanitized>_KnowledgeGraph`.
        """
        srv = _fresh_server({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "VCT_KG_ACCESS_LIST": "Beta,Gamma",
        })
        result = srv._kg_collections_to_search(include_dev=False)
        # Order: self → shared → peers (in insertion order from the env).
        self.assertEqual(
            result,
            [
                "Alpha_KnowledgeGraph",
                "VibecodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
                "Gamma_KnowledgeGraph",
            ],
            f"unexpected fan-out list: {result}",
        )

    def test_kg_collections_to_search_handles_empty_or_missing_env(self):
        """Empty / unset VCT_KG_ACCESS_LIST falls back to the pre-P1-D
        behaviour: just self + shared (when shared is configured and
        distinct from self)."""
        # Fully unset: no peers.
        srv = _fresh_server({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
        })
        self.assertEqual(
            srv._kg_collections_to_search(include_dev=False),
            ["Alpha_KnowledgeGraph", "VibecodedOrchestrator_KnowledgeGraph"],
        )

        # Empty string (the pre-fix shape: launcher wrote
        # `VCT_KG_ACCESS_LIST=""` literal): also no peers.
        _clear_access_env()
        srv2 = _fresh_server({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "VCT_KG_ACCESS_LIST": "",
        })
        self.assertEqual(
            srv2._kg_collections_to_search(include_dev=False),
            ["Alpha_KnowledgeGraph", "VibecodedOrchestrator_KnowledgeGraph"],
        )

        # Whitespace-only entries are filtered: `,, ,` → no peers.
        _clear_access_env()
        srv3 = _fresh_server({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "VCT_KG_ACCESS_LIST": ",, ,",
        })
        self.assertEqual(
            srv3._kg_collections_to_search(include_dev=False),
            ["Alpha_KnowledgeGraph", "VibecodedOrchestrator_KnowledgeGraph"],
        )

    def test_kg_collections_to_search_dedupes_self_and_shared(self):
        """If a peer name happens to match self / shared, the helper
        does not double-list it. Defensive: should not happen in
        practice (the launcher's resolver excludes those) but the
        helper must be robust to malformed input."""
        srv = _fresh_server({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            # `Alpha` matches self, `VibeCodedTools` matches shared
            # (the sanitization is idempotent).
            "VCT_KG_ACCESS_LIST": "Alpha,VibeCodedTools,Beta",
        })
        result = srv._kg_collections_to_search(include_dev=False)
        # Only Beta is added beyond self + shared.
        self.assertEqual(
            result,
            [
                "Alpha_KnowledgeGraph",
                "VibecodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
            ],
        )

    def test_kg_collections_to_search_includes_dev_when_requested(self):
        """`hybrid_search` calls with `include_dev=True`. The helper must
        append DEVELOPMENT_COLLECTION at the end (after peers)."""
        srv = _fresh_server({
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "Alpha_Development",
            "VCT_KG_ACCESS_LIST": "Beta",
        })
        self.assertEqual(
            srv._kg_collections_to_search(include_dev=True),
            [
                "Alpha_KnowledgeGraph",
                "VibecodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
                "Alpha_Development",
            ],
        )

    def test_parse_csv_env_strips_whitespace(self):
        """`_parse_csv_env` (the helper that powers VCT_*_ACCESS_LIST
        parsing) tolerates whitespace-padded comma-separated input —
        common when users hand-edit `.env`."""
        srv = _fresh_server({
            "VCT_KG_ACCESS_LIST": " Foo , Bar ,Baz ",
        })
        self.assertEqual(srv._parse_csv_env("VCT_KG_ACCESS_LIST"), ["Foo", "Bar", "Baz"])

    def test_kg_peer_collections_dedupes_repeated_entries(self):
        """`_kg_peer_collections` dedupes by collection name. A user
        listing the same peer twice should not yield two collection
        queries."""
        srv = _fresh_server({
            "VCT_KG_ACCESS_LIST": "Foo,Foo,Bar,Foo",
        })
        self.assertEqual(
            srv._kg_peer_collections(),
            ["Foo_KnowledgeGraph", "Bar_KnowledgeGraph"],
        )


if __name__ == "__main__":
    unittest.main()
