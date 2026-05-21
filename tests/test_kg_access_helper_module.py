# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the centralized KG / code-graph access helper module.

Tests ``claude_mcp_servers/scripts/kg_access.py`` — the standalone
helper that standalone CLI scripts (``search_knowledge.py``,
``query_code_graph.py``, ``get_node_info.py``) import to honour the
runtime access matrix without pulling in the full MCP server.

This module is intentionally a parallel implementation to
``weaviate_mcp.server._kg_collections_to_search`` etc. — the MCP path
keeps its in-module helpers (existing tests in
``test_kg_access_list.py`` pin those), and the CLI path uses this
module. Both paths must produce the same lists given the same env.

Tests in this file pin the helper's contract in isolation (no real
Weaviate, no MCP imports). The CLI integration tests
(``test_search_knowledge_access_list.py``, etc.) verify the wire-up.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HELPER_DIR = PROJECT_ROOT / "claude_mcp_servers" / "scripts"
sys.path.insert(0, str(HELPER_DIR))


def _fresh_helper(env_overrides: dict[str, str] | None = None):
    """Reload kg_access with a fresh env snapshot.

    The helper reads env vars at function-call time (not import time),
    so a reload isn't strictly needed — but we do it anyway to keep the
    pattern consistent with ``test_kg_access_list.py`` and to guarantee
    no module-level state leaks between tests.
    """
    if env_overrides:
        for k, v in env_overrides.items():
            os.environ[k] = v
    if "kg_access" in sys.modules:
        del sys.modules["kg_access"]
    return importlib.import_module("kg_access")


def _clear_access_env() -> None:
    """Strip access-matrix env vars so each test starts from baseline."""
    for k in (
        "VCT_KG_ACCESS_LIST",
        "VCT_CODE_GRAPH_ACCESS_LIST",
    ):
        os.environ.pop(k, None)


class ParseCsvEnvTests(unittest.TestCase):
    """``parse_csv_env`` must mirror the MCP-side ``_parse_csv_env`` so
    callers get identical behaviour through both paths."""

    def setUp(self) -> None:
        _clear_access_env()

    def test_returns_empty_list_when_unset(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(helper.parse_csv_env("VCT_KG_ACCESS_LIST"), [])

    def test_returns_empty_list_when_empty_string(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": ""})
        self.assertEqual(helper.parse_csv_env("VCT_KG_ACCESS_LIST"), [])

    def test_returns_empty_list_when_whitespace_only(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": ",, , "})
        self.assertEqual(helper.parse_csv_env("VCT_KG_ACCESS_LIST"), [])

    def test_strips_whitespace_around_entries(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": " Foo , Bar ,Baz "})
        self.assertEqual(
            helper.parse_csv_env("VCT_KG_ACCESS_LIST"),
            ["Foo", "Bar", "Baz"],
        )


class SanitizeCollectionPrefixTests(unittest.TestCase):
    """Idempotent sanitization — already-sanitized prefixes pass through."""

    def setUp(self) -> None:
        _clear_access_env()

    def test_idempotent_for_clean_input(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(helper.sanitize_collection_prefix("Alpha"), "Alpha")
        self.assertEqual(helper.sanitize_collection_prefix("Vibe_Coded_Tools"), "Vibe_Coded_Tools")

    def test_replaces_unsupported_chars(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(helper.sanitize_collection_prefix("foo-bar.baz"), "Foo_bar_baz")

    def test_uppercases_first_char(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(helper.sanitize_collection_prefix("alpha"), "Alpha")


class KgPeerCollectionsTests(unittest.TestCase):
    """``kg_peer_collections`` parses VCT_KG_ACCESS_LIST → list of
    ``<Peer>_KnowledgeGraph`` collection names, deduped, order
    preserved."""

    def setUp(self) -> None:
        _clear_access_env()

    def test_empty_when_unset(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(helper.kg_peer_collections(), [])

    def test_returns_kg_suffixed_names(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": "Foo,Bar"})
        self.assertEqual(
            helper.kg_peer_collections(),
            ["Foo_KnowledgeGraph", "Bar_KnowledgeGraph"],
        )

    def test_dedupes_repeated_entries(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": "Foo,Foo,Bar,Foo"})
        self.assertEqual(
            helper.kg_peer_collections(),
            ["Foo_KnowledgeGraph", "Bar_KnowledgeGraph"],
        )


class KgCollectionsToSearchTests(unittest.TestCase):
    """``kg_collections_to_search`` returns self → shared → peers → dev
    in that order, deduped."""

    def setUp(self) -> None:
        _clear_access_env()

    def test_self_only_when_no_shared_no_peers(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(
            helper.kg_collections_to_search("Alpha_KnowledgeGraph"),
            ["Alpha_KnowledgeGraph"],
        )

    def test_self_then_shared_when_no_peers(self) -> None:
        helper = _fresh_helper()
        self.assertEqual(
            helper.kg_collections_to_search(
                "Alpha_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
            ),
            ["Alpha_KnowledgeGraph", "VibeCodedOrchestrator_KnowledgeGraph"],
        )

    def test_self_shared_peers_in_order(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": "Beta,Gamma"})
        self.assertEqual(
            helper.kg_collections_to_search(
                "Alpha_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
            ),
            [
                "Alpha_KnowledgeGraph",
                "VibeCodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
                "Gamma_KnowledgeGraph",
            ],
        )

    def test_dev_appended_when_include_dev_true(self) -> None:
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": "Beta"})
        self.assertEqual(
            helper.kg_collections_to_search(
                "Alpha_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
                development="Alpha_Development",
                include_dev=True,
            ),
            [
                "Alpha_KnowledgeGraph",
                "VibeCodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
                "Alpha_Development",
            ],
        )

    def test_dev_not_appended_when_include_dev_false(self) -> None:
        """Even when ``development`` is set, ``include_dev=False``
        suppresses it. Mirrors ``semantic_graph_search`` semantics
        (skips dev docs)."""
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": "Beta"})
        self.assertEqual(
            helper.kg_collections_to_search(
                "Alpha_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
                development="Alpha_Development",
                include_dev=False,
            ),
            [
                "Alpha_KnowledgeGraph",
                "VibeCodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
            ],
        )

    def test_self_equals_shared_does_not_double_list(self) -> None:
        """If the project's KG happens to BE the shared KG (single-tenant
        / orchestrator-self setup), don't double-list it."""
        helper = _fresh_helper()
        self.assertEqual(
            helper.kg_collections_to_search(
                "VibeCodedOrchestrator_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
            ),
            ["VibeCodedOrchestrator_KnowledgeGraph"],
        )

    def test_peer_matching_self_is_filtered(self) -> None:
        """Defensive: launcher excludes self from access list, but the
        helper must be robust if a malformed env var lists self."""
        helper = _fresh_helper({"VCT_KG_ACCESS_LIST": "Alpha,Beta"})
        self.assertEqual(
            helper.kg_collections_to_search(
                "Alpha_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
            ),
            [
                "Alpha_KnowledgeGraph",
                "VibeCodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
            ],
        )

    def test_peer_matching_shared_is_filtered(self) -> None:
        """Same defensive guard, applied to the shared collection.

        PR-34 (v0.2.12) renamed the canonical shared KG from
        VibeCodedTools_KnowledgeGraph to VibecodedOrchestrator_KnowledgeGraph.
        v0.2.23 B1 (2026-05-21) flipped the casing back to capital-C
        VibeCodedOrchestrator_KnowledgeGraph to match the brand spelling.
        The peer that matches "shared" is therefore "VibeCodedOrchestrator"
        (capital-C, matching the canonical) — pass a peer that maps to
        the same collection as `shared_kg` and verify the dedup guard
        kicks in.
        """
        helper = _fresh_helper(
            {"VCT_KG_ACCESS_LIST": "VibeCodedOrchestrator,Beta"}
        )
        self.assertEqual(
            helper.kg_collections_to_search(
                "Alpha_KnowledgeGraph",
                shared_kg="VibeCodedOrchestrator_KnowledgeGraph",
            ),
            [
                "Alpha_KnowledgeGraph",
                "VibeCodedOrchestrator_KnowledgeGraph",
                "Beta_KnowledgeGraph",
            ],
        )


class CodeGraphCollectionsToQueryTests(unittest.TestCase):
    """``code_graph_collections_to_query`` builds (collection_name,
    filter_value) pairs for code-graph fan-out."""

    def setUp(self) -> None:
        _clear_access_env()

    def test_returns_bare_collections_when_no_self_project(self) -> None:
        """Empty self_project → bare collection names, no filter
        (cross-tenant fallback path used when caller asks for
        ``project=""``)."""
        helper = _fresh_helper()
        result = helper.code_graph_collections_to_query("")
        self.assertEqual(
            result,
            [
                ("CodeFunction", ""),
                ("CodeClass", ""),
                ("CodeModule", ""),
                ("CodeAPI", ""),
                ("CodeInteraction", ""),
            ],
        )

    def test_self_project_only_when_no_peers(self) -> None:
        helper = _fresh_helper()
        result = helper.code_graph_collections_to_query("Alpha")
        self.assertEqual(
            result,
            [
                ("Alpha_CodeFunction", "Alpha"),
                ("Alpha_CodeClass", "Alpha"),
                ("Alpha_CodeModule", "Alpha"),
                ("Alpha_CodeAPI", "Alpha"),
                ("Alpha_CodeInteraction", "Alpha"),
            ],
        )

    def test_peers_included_when_access_list_set(self) -> None:
        """Both self and peer collections appear, each with their own
        project filter value."""
        helper = _fresh_helper(
            {"VCT_CODE_GRAPH_ACCESS_LIST": "Beta,Gamma"}
        )
        result = helper.code_graph_collections_to_query(
            "Alpha", bases=("CodeFunction", "CodeClass")
        )
        self.assertEqual(
            result,
            [
                ("Alpha_CodeFunction", "Alpha"),
                ("Alpha_CodeClass", "Alpha"),
                ("Beta_CodeFunction", "Beta"),
                ("Beta_CodeClass", "Beta"),
                ("Gamma_CodeFunction", "Gamma"),
                ("Gamma_CodeClass", "Gamma"),
            ],
        )

    def test_peer_matching_self_is_skipped(self) -> None:
        """Defensive: an access list listing self mustn't double-list
        self collections."""
        helper = _fresh_helper(
            {"VCT_CODE_GRAPH_ACCESS_LIST": "Alpha,Beta"}
        )
        result = helper.code_graph_collections_to_query(
            "Alpha", bases=("CodeFunction",)
        )
        self.assertEqual(
            result,
            [
                ("Alpha_CodeFunction", "Alpha"),
                ("Beta_CodeFunction", "Beta"),
            ],
        )

    def test_default_bases_covers_all_5_collections(self) -> None:
        """When ``bases`` is not passed, all 5 base collections appear
        per project. Pins the canonical ``CODE_GRAPH_BASES`` constant
        against drift."""
        helper = _fresh_helper()
        result = helper.code_graph_collections_to_query("Alpha")
        # 5 bases × 1 project = 5 pairs
        self.assertEqual(len(result), 5)
        names = [coll for coll, _ in result]
        self.assertEqual(
            names,
            [
                "Alpha_CodeFunction",
                "Alpha_CodeClass",
                "Alpha_CodeModule",
                "Alpha_CodeAPI",
                "Alpha_CodeInteraction",
            ],
        )

    def test_dedupes_peers_by_prefix(self) -> None:
        """Repeated peers in the access list yield the same prefix and
        must be deduped."""
        helper = _fresh_helper(
            {"VCT_CODE_GRAPH_ACCESS_LIST": "Beta,Beta,Gamma"}
        )
        result = helper.code_graph_collections_to_query(
            "Alpha", bases=("CodeFunction",)
        )
        self.assertEqual(
            result,
            [
                ("Alpha_CodeFunction", "Alpha"),
                ("Beta_CodeFunction", "Beta"),
                ("Gamma_CodeFunction", "Gamma"),
            ],
        )


class HelperParityWithMcpServerTests(unittest.TestCase):
    """The CLI helper and the MCP server's in-module helpers must
    produce IDENTICAL output given the same env. Pinning this guards
    against silent drift between the two."""

    def setUp(self) -> None:
        # Strip every env var that influences either helper.
        for k in (
            "VCT_KG_ACCESS_LIST",
            "VCT_CODE_GRAPH_ACCESS_LIST",
            "KG_COLLECTION",
            "SHARED_KG_COLLECTION",
            "DEVELOPMENT_COLLECTION",
            "PROJECT_NAME",
            "CODE_GRAPH_PROJECT",
        ):
            os.environ.pop(k, None)

    def _fresh_mcp_server(self, env_overrides: dict[str, str]):
        """Reload weaviate_mcp.server with a fresh env."""
        for mod in list(sys.modules):
            if mod.startswith("weaviate_mcp"):
                del sys.modules[mod]
        for k, v in env_overrides.items():
            os.environ[k] = v
        # Add MCP servers dir to path
        mcp_dir = PROJECT_ROOT / "claude_mcp_servers"
        if str(mcp_dir) not in sys.path:
            sys.path.insert(0, str(mcp_dir))
        return importlib.import_module("weaviate_mcp.server")

    def test_kg_helper_matches_mcp_server_output(self) -> None:
        """For the same env, ``kg_access.kg_collections_to_search`` and
        ``server._kg_collections_to_search`` produce the same list."""
        env = {
            "KG_COLLECTION": "Alpha_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "Alpha_Development",
            "VCT_KG_ACCESS_LIST": "Beta,Gamma",
        }
        mcp = self._fresh_mcp_server(env)
        helper = _fresh_helper()
        self.assertEqual(
            helper.kg_collections_to_search(
                self_kg=mcp.KG_COLLECTION,
                shared_kg=mcp.SHARED_KG_COLLECTION,
                development=mcp.DEVELOPMENT_COLLECTION,
                include_dev=False,
            ),
            mcp._kg_collections_to_search(include_dev=False),
        )
        self.assertEqual(
            helper.kg_collections_to_search(
                self_kg=mcp.KG_COLLECTION,
                shared_kg=mcp.SHARED_KG_COLLECTION,
                development=mcp.DEVELOPMENT_COLLECTION,
                include_dev=True,
            ),
            mcp._kg_collections_to_search(include_dev=True),
        )


if __name__ == "__main__":
    unittest.main()
