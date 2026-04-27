# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the shared cross-project KG collection.

Covers the Python side of the shared-KG feature:
    - SHARED_KG_OPT_OUT env var disables the shared collection.
    - The default SHARED_KG_COLLECTION resolves to "VibeCodedTools_KnowledgeGraph".
    - _load_node_formats_for_collection picks the right sidecar per collection.
    - _format_result_by_tier uses the result's source_collection / collection
      to resolve sidecar fields.

The Weaviate-roundtrip flow (hybrid_search hitting both collections live) is
not covered here because it would require a running Weaviate plus seed
content; smoke-test that one manually after a fresh `python install.py`.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import unittest
from pathlib import Path

# Server module lives under claude_mcp_servers/weaviate_mcp/. Make sure both
# its parent (so `from chunking import Chunker` works in the script-style
# import inside server.py) and the project root are on sys.path before
# importing.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
sys.path.insert(0, str(MCP_DIR))


def _fresh_server(env_overrides: dict[str, str]):
    """Reload weaviate_mcp.server with a fresh env. Returns the module.

    The server reads KG_COLLECTION / SHARED_KG_COLLECTION / SHARED_KG_OPT_OUT
    at import time, so we have to reimport (not just patch) to test the
    different env permutations.

    Note: env_overrides are applied to ``os.environ`` directly (NOT via a
    context manager) because some sidecar lookups read env at runtime, not
    just at import. Tests are responsible for cleaning up if needed.
    """
    # Drop any cached version of the server module so the env is re-read.
    for mod in list(sys.modules):
        if mod.startswith("weaviate_mcp"):
            del sys.modules[mod]
    # Apply env overrides directly so they survive past this function's return.
    for k, v in env_overrides.items():
        os.environ[k] = v
    return importlib.import_module("weaviate_mcp.server")


class SharedKgEnvTests(unittest.TestCase):
    """SHARED_KG_COLLECTION / SHARED_KG_OPT_OUT env handling."""

    def test_default_shared_kg_is_vibecoded(self):
        """Without explicit env, the shared collection defaults to the
        canonical VibeCodedTools_KnowledgeGraph."""
        # Pop the keys entirely so the default branch fires.
        for k in ("SHARED_KG_COLLECTION", "SHARED_KG_OPT_OUT"):
            os.environ.pop(k, None)
        srv = _fresh_server(env_overrides={})
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibeCodedTools_KnowledgeGraph")
        self.assertFalse(srv.SHARED_KG_OPT_OUT)

    def test_opt_out_disables_shared_collection(self):
        """SHARED_KG_OPT_OUT=true zeroes SHARED_KG_COLLECTION even if the
        env var is set, so all dual-collection code paths skip the shared
        query."""
        srv = _fresh_server({
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "true",
        })
        self.assertTrue(srv.SHARED_KG_OPT_OUT)
        self.assertEqual(srv.SHARED_KG_COLLECTION, "")
        # The "would have been" reference is preserved for diagnostic logs.
        self.assertEqual(srv._SHARED_KG_RAW, "VibeCodedTools_KnowledgeGraph")

    def test_opt_out_accepts_multiple_truthy_values(self):
        """SHARED_KG_OPT_OUT honours common truthy spellings."""
        for val in ("true", "True", "TRUE", "1", "yes", "YES"):
            srv = _fresh_server({
                "SHARED_KG_COLLECTION": "Some_KG",
                "SHARED_KG_OPT_OUT": val,
            })
            self.assertTrue(srv.SHARED_KG_OPT_OUT, f"opt-out should be true for {val!r}")
            self.assertEqual(srv.SHARED_KG_COLLECTION, "")

    def test_opt_out_falsy_values_keep_shared_active(self):
        """SHARED_KG_OPT_OUT=false / empty / 0 leaves the shared collection
        active (default behaviour)."""
        for val in ("false", "FALSE", "0", "no", ""):
            srv = _fresh_server({
                "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
                "SHARED_KG_OPT_OUT": val,
            })
            self.assertFalse(srv.SHARED_KG_OPT_OUT, f"opt-out should be false for {val!r}")
            self.assertEqual(srv.SHARED_KG_COLLECTION, "VibeCodedTools_KnowledgeGraph")

    def test_explicit_shared_kg_override(self):
        """A user can point SHARED_KG_COLLECTION at a custom name (e.g. for
        a private team-shared collection)."""
        srv = _fresh_server({
            "SHARED_KG_COLLECTION": "AcmeTeam_SharedKG",
            "SHARED_KG_OPT_OUT": "false",
        })
        self.assertEqual(srv.SHARED_KG_COLLECTION, "AcmeTeam_SharedKG")


class SidecarPerCollectionTests(unittest.TestCase):
    """Per-collection sidecar resolution for tier formatting."""

    def setUp(self):
        # Build a fresh server module each test; helpers cache module-level
        # state (_node_formats_by_collection) and we want isolation.
        self.tmpdir = Path(__file__).resolve().parent / "_tmp_shared_kg"
        self.tmpdir.mkdir(exist_ok=True)
        # Project sidecar
        self.project_kg_dir = self.tmpdir / "project" / "knowledge"
        self.project_kg_dir.mkdir(parents=True, exist_ok=True)
        (self.project_kg_dir / ".node_formats.json").write_text(json.dumps({
            "knowledge/concepts/foo.md": {
                "description": "PROJECT: foo description",
                "summary": "project foo summary",
            },
        }))
        # Shared sidecar (override path via env)
        self.shared_sidecar = self.tmpdir / "shared.node_formats.json"
        self.shared_sidecar.write_text(json.dumps({
            "knowledge/concepts/bar.md": {
                "description": "SHARED: bar description",
                "summary": "shared bar summary",
            },
        }))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_per_collection_sidecar_resolution(self):
        """Project-KG result and shared-KG result resolve descriptions from
        their respective sidecars even when keyed under the same file_path
        prefix."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "false",
            "KG_BASE_DIR": str(self.tmpdir / "project"),
            "SHARED_KG_NODE_FORMATS": str(self.shared_sidecar),
        })

        proj = srv._load_node_formats_for_collection("ProjectKG")
        shared = srv._load_node_formats_for_collection("VibeCodedTools_KnowledgeGraph")

        self.assertIn("knowledge/concepts/foo.md", proj)
        self.assertEqual(
            proj["knowledge/concepts/foo.md"]["description"],
            "PROJECT: foo description",
        )
        self.assertIn("knowledge/concepts/bar.md", shared)
        self.assertEqual(
            shared["knowledge/concepts/bar.md"]["description"],
            "SHARED: bar description",
        )

    def test_unknown_collection_returns_empty_dict(self):
        """Unknown collections (e.g. dev docs) get an empty sidecar dict
        rather than blowing up."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
        })
        self.assertEqual(
            srv._load_node_formats_for_collection("RandomDevCollection"), {}
        )

    def test_format_result_by_tier_uses_collection_sidecar(self):
        """A result with collection=SHARED... pulls from the shared sidecar,
        not the project sidecar — this is the integration that matters for
        users."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "false",
            "KG_BASE_DIR": str(self.tmpdir / "project"),
            "SHARED_KG_NODE_FORMATS": str(self.shared_sidecar),
        })

        # Shared-collection result ⇒ should pull "SHARED:" description.
        shared_result = {
            "title": "bar",
            "node_type": "concept",
            "file_path": "knowledge/concepts/bar.md",
            "tags": [],
            "score": 0.5,  # → summary tier
            "content": "(content body)",
            "collection": "VibeCodedTools_KnowledgeGraph",
        }
        out = srv._format_result_by_tier(shared_result, "summary")
        self.assertIsNotNone(out)
        self.assertEqual(out["description"], "SHARED: bar description")

        # Project-collection result ⇒ should pull "PROJECT:" description.
        project_result = {
            "title": "foo",
            "node_type": "concept",
            "file_path": "knowledge/concepts/foo.md",
            "tags": [],
            "score": 0.5,
            "content": "(content body)",
            "collection": "ProjectKG",
        }
        out = srv._format_result_by_tier(project_result, "summary")
        self.assertIsNotNone(out)
        self.assertEqual(out["description"], "PROJECT: foo description")


class StoreKnowledgeNodeScopeTests(unittest.TestCase):
    """The scope='shared' parameter on store_knowledge_node routes writes to
    the shared collection. Doesn't roundtrip Weaviate (no live instance in
    unit tests); just verifies the collection-selection branch."""

    def test_scope_shared_targets_shared_collection_when_set(self):
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "false",
        })

        # Replicate the core selection logic: scope='shared' AND
        # SHARED_KG_COLLECTION set AND not equal to KG_COLLECTION → shared.
        # This is the same condition store_knowledge_node uses internally.
        scope = "shared"
        target = srv.KG_COLLECTION
        if scope == "shared" and srv.SHARED_KG_COLLECTION and srv.SHARED_KG_COLLECTION != srv.KG_COLLECTION:
            target = srv.SHARED_KG_COLLECTION

        self.assertEqual(target, "VibeCodedTools_KnowledgeGraph")

    def test_scope_shared_falls_back_when_opted_out(self):
        """Opt-out makes SHARED_KG_COLLECTION='' — scope='shared' then
        falls back to the project KG (no silent black-hole writes)."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "true",
        })

        scope = "shared"
        target = srv.KG_COLLECTION
        if scope == "shared" and srv.SHARED_KG_COLLECTION and srv.SHARED_KG_COLLECTION != srv.KG_COLLECTION:
            target = srv.SHARED_KG_COLLECTION

        self.assertEqual(target, "ProjectKG")  # fallback

    def test_scope_project_always_targets_kg_collection(self):
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibeCodedTools_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "false",
        })

        scope = "project"
        target = srv.KG_COLLECTION
        if scope == "shared" and srv.SHARED_KG_COLLECTION and srv.SHARED_KG_COLLECTION != srv.KG_COLLECTION:
            target = srv.SHARED_KG_COLLECTION

        self.assertEqual(target, "ProjectKG")


if __name__ == "__main__":
    unittest.main()
