# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.27 KG env-propagation bug fix — server-side empty-string safety.

Background: the 2026-05-22 bug report showed that an MCP subprocess
without ``KG_COLLECTION`` set (or with it set to empty by a stale
``.vscode/settings.json claude-code.env`` block, which doesn't propagate
to MCP subprocesses on Linux per PR-27) fell back to the bundled default
``ClaudeKnowledgeGraph`` and surfaced a ``WeaviateSchemaError`` listing
the default names with no indication of where they came from. Users had
no signal that the env wasn't propagating.

These tests pin three contracts:

1. ``_config_field(empty_means_unset=True)`` coerces an explicit empty
   env value to the bundled default — used for ``KG_COLLECTION``, where
   empty would propagate to Weaviate and cause schema-fail.
2. ``_config_field(empty_means_unset=False)`` (default) preserves the
   empty value — used for ``SHARED_KG_COLLECTION`` and
   ``DEVELOPMENT_COLLECTION``, where empty carries the semantic "no
   shared / dev collection bound for this project".
3. ``_kg_collections_to_search`` defensively filters empty / whitespace
   entries before fan-out.
4. ``_format_failed_collections_hint`` annotates each failing collection
   with its resolution source (self/shared/peer/dev + hub/env/default).
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reload_server(env: dict):
    """Reload weaviate_mcp.server with the given env, return the module.

    Block the hub lookup with VCT_HUB_PORT=0 so we exercise the env-fallback
    path deterministically (hub-resolved tests live elsewhere).

    Also clear ``VCT_KG_ACCESS_LIST`` / ``VCT_CODE_GRAPH_ACCESS_LIST`` so
    earlier tests in the same session that set these don't leak into the
    fan-out list and inflate the search-collection output.
    """
    full_env = dict(env)
    full_env.setdefault("VCT_HUB_PORT", "0")
    # Sibling test files (e.g. test_search_knowledge_access_list.py)
    # mutate os.environ directly without rollback for ``VCT_KG_ACCESS_LIST``
    # / ``VCT_CODE_GRAPH_ACCESS_LIST``, so by the time our tests run those
    # may still be set to "MissingPeer" or similar from earlier runs. We
    # have to explicitly drop them from os.environ before mock.patch.dict
    # snapshots the current state — passing them in `full_env` would only
    # ADD them to the patch, leaving any pre-existing literal value intact.
    for leak_key in ("VCT_KG_ACCESS_LIST", "VCT_CODE_GRAPH_ACCESS_LIST"):
        os.environ.pop(leak_key, None)
    with mock.patch.dict(os.environ, full_env, clear=False):
        import claude_mcp_servers.weaviate_mcp.server as server_mod  # type: ignore
        # Force a fresh hub-resolve attempt — previous test may have
        # populated the cache with a different project_config snapshot.
        try:
            server_mod._resolved_project_config = None  # type: ignore[attr-defined]
        except Exception:
            pass
        importlib.reload(server_mod)
        return server_mod


class ConfigFieldEmptyEnvTests(unittest.TestCase):
    """Pin the empty_means_unset semantic for ``_config_field``."""

    def test_kg_collection_empty_env_coerces_to_default(self):
        env = {
            "KG_COLLECTION": "",
            "SHARED_KG_COLLECTION": "should-not-collide",
            "DEVELOPMENT_COLLECTION": "DevCol",
        }
        s = _reload_server(env)
        self.assertEqual(s.KG_COLLECTION, "ClaudeKnowledgeGraph")
        self.assertEqual(s._KG_COLLECTION_SOURCE, "default(empty-env-coerced)")

    def test_kg_collection_whitespace_only_env_coerces_to_default(self):
        env = {
            "KG_COLLECTION": "   ",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        self.assertEqual(s.KG_COLLECTION, "ClaudeKnowledgeGraph")
        self.assertEqual(s._KG_COLLECTION_SOURCE, "default(empty-env-coerced)")

    def test_kg_collection_explicit_value_propagates(self):
        env = {
            "KG_COLLECTION": "VCODev_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        self.assertEqual(s.KG_COLLECTION, "VCODev_KnowledgeGraph")
        self.assertEqual(s._KG_COLLECTION_SOURCE, "env")

    def test_shared_kg_empty_env_preserves_empty_literal(self):
        """SHARED_KG_COLLECTION='' must NOT be coerced — empty = unbound."""
        env = {
            "KG_COLLECTION": "X",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        self.assertEqual(s.SHARED_KG_COLLECTION, "")

    def test_development_collection_empty_env_preserves_empty_literal(self):
        env = {
            "KG_COLLECTION": "X",
            "SHARED_KG_COLLECTION": "Y",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        self.assertEqual(s.DEVELOPMENT_COLLECTION, "")


class KgCollectionsToSearchFilterTests(unittest.TestCase):
    """Pin the defensive empty-name filter in ``_kg_collections_to_search``."""

    def test_search_list_excludes_empty_shared_kg(self):
        env = {
            "KG_COLLECTION": "ProjA_KG",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "ProjA_dev",
        }
        s = _reload_server(env)
        # KG only, no shared (empty), with dev when include_dev=True
        self.assertEqual(s._kg_collections_to_search(include_dev=False), ["ProjA_KG"])
        self.assertEqual(
            s._kg_collections_to_search(include_dev=True),
            ["ProjA_KG", "ProjA_dev"],
        )

    def test_search_list_excludes_whitespace_shared_kg(self):
        env = {
            "KG_COLLECTION": "ProjA_KG",
            "SHARED_KG_COLLECTION": "   ",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        self.assertEqual(s._kg_collections_to_search(include_dev=False), ["ProjA_KG"])

    def test_search_list_dedupes_when_kg_equals_shared(self):
        env = {
            "KG_COLLECTION": "Shared_KG",
            "SHARED_KG_COLLECTION": "Shared_KG",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        # KG_COLLECTION present, SHARED skipped (same name)
        self.assertEqual(s._kg_collections_to_search(include_dev=False), ["Shared_KG"])

    def test_search_list_normal_fanout(self):
        env = {
            "KG_COLLECTION": "VCODev_KnowledgeGraph",
            "SHARED_KG_COLLECTION": "VibeCodedOrchestrator_KnowledgeGraph",
            "DEVELOPMENT_COLLECTION": "VCODev_Development",
        }
        s = _reload_server(env)
        self.assertEqual(
            s._kg_collections_to_search(include_dev=True),
            [
                "VCODev_KnowledgeGraph",
                "VibeCodedOrchestrator_KnowledgeGraph",
                "VCODev_Development",
            ],
        )


class FormatFailedCollectionsHintTests(unittest.TestCase):
    """Pin annotated hint formatting used in WeaviateSchemaError messages."""

    def test_hint_annotates_self_kg(self):
        env = {
            "KG_COLLECTION": "MyKG",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        hint = s._format_failed_collections_hint(["MyKG"])
        self.assertIn("MyKG", hint)
        self.assertIn("self/KG_COLLECTION", hint)
        self.assertIn("src=env", hint)

    def test_hint_annotates_default_when_env_empty(self):
        env = {
            "KG_COLLECTION": "",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        hint = s._format_failed_collections_hint(["ClaudeKnowledgeGraph"])
        self.assertIn("ClaudeKnowledgeGraph", hint)
        self.assertIn("default(empty-env-coerced)", hint)

    def test_hint_annotates_peer_for_unknown_collection(self):
        env = {
            "KG_COLLECTION": "Self_KG",
            "SHARED_KG_COLLECTION": "Shared_KG",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        hint = s._format_failed_collections_hint(["Peer_KG"])
        self.assertIn("Peer_KG", hint)
        self.assertIn("peer/VCT_KG_ACCESS_LIST", hint)

    def test_hint_truncates_above_six(self):
        env = {
            "KG_COLLECTION": "Self_KG",
            "SHARED_KG_COLLECTION": "",
            "DEVELOPMENT_COLLECTION": "",
        }
        s = _reload_server(env)
        failed = [f"Coll_{i}" for i in range(10)]
        hint = s._format_failed_collections_hint(failed)
        # Should include first 6 + a trailing ellipsis marker
        self.assertIn("Coll_0", hint)
        self.assertIn("Coll_5", hint)
        self.assertNotIn("Coll_6", hint)
        self.assertIn("…", hint)


if __name__ == "__main__":
    unittest.main()
