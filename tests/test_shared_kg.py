# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the shared cross-project KG collection.

Covers the Python side of the shared-KG feature:
    - SHARED_KG_COLLECTION read paths are ALWAYS active when configured
      (no per-project read opt-out — asymmetric model since 2026-05-01).
    - SHARED_KG_WRITE_DISABLED gates writes only.
    - SHARED_KG_OPT_OUT (legacy) is honoured as a write-only fallback.
    - The default SHARED_KG_COLLECTION resolves to "VibecodedOrchestrator_KnowledgeGraph".
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

    The server reads KG_COLLECTION / SHARED_KG_COLLECTION /
    SHARED_KG_WRITE_DISABLED / SHARED_KG_OPT_OUT at import time, so we
    have to reimport (not just patch) to test the different env
    permutations.

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


def _clear_shared_env() -> None:
    """Clean both new and legacy shared-KG env vars between tests so leftover
    values from a prior test don't bleed across cases."""
    for k in (
        "SHARED_KG_COLLECTION",
        "SHARED_KG_WRITE_DISABLED",
        "SHARED_KG_OPT_OUT",
    ):
        os.environ.pop(k, None)


class SharedKgEnvTests(unittest.TestCase):
    """SHARED_KG_COLLECTION / SHARED_KG_WRITE_DISABLED / legacy alias env handling.

    Asymmetric model since 2026-05-01: SHARED_KG_COLLECTION is ALWAYS exposed
    to read paths when set; only writes are gated.
    """

    def setUp(self) -> None:
        _clear_shared_env()

    def test_default_shared_kg_is_vibecoded(self):
        """Without explicit env, the shared collection defaults to the
        canonical VibecodedOrchestrator_KnowledgeGraph and writes are allowed."""
        srv = _fresh_server(env_overrides={})
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")
        self.assertFalse(srv.SHARED_KG_WRITE_DISABLED)
        # Back-compat alias — points at the resolved write-disabled value.
        self.assertFalse(srv.SHARED_KG_OPT_OUT)

    def test_read_path_is_unconditional(self):
        """Even with SHARED_KG_WRITE_DISABLED=true, SHARED_KG_COLLECTION
        stays populated for read paths. This is the headline asymmetry of
        the 2026-05-01 refactor."""
        srv = _fresh_server({
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "true",
        })
        # Read surface untouched.
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")
        # Write gate is on.
        self.assertTrue(srv.SHARED_KG_WRITE_DISABLED)

    def test_read_path_is_unconditional_under_legacy_alias(self):
        """SHARED_KG_OPT_OUT=true must NOT zero the read collection any
        more. It only forwards to the write gate. This breaks the previous
        contract on purpose: keeping legacy alias semantically symmetric
        with the new key prevents two different "true" meanings."""
        srv = _fresh_server({
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "true",
        })
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")
        self.assertTrue(srv.SHARED_KG_WRITE_DISABLED)
        self.assertTrue(srv.SHARED_KG_OPT_OUT)

    def test_canonical_key_wins_over_legacy_alias(self):
        """When both are set, SHARED_KG_WRITE_DISABLED wins. This lets
        users explicitly RE-ENABLE writes on a project whose .env still
        carries the legacy SHARED_KG_OPT_OUT=true."""
        srv = _fresh_server({
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "false",
            "SHARED_KG_OPT_OUT": "true",
        })
        self.assertFalse(srv.SHARED_KG_WRITE_DISABLED,
                         "canonical key 'false' must win over legacy 'true'")
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")

    def test_write_disabled_accepts_truthy_spellings(self):
        """SHARED_KG_WRITE_DISABLED honours common truthy spellings."""
        for val in ("true", "True", "TRUE", "1", "yes", "YES"):
            _clear_shared_env()
            srv = _fresh_server({
                "SHARED_KG_COLLECTION": "Some_KG",
                "SHARED_KG_WRITE_DISABLED": val,
            })
            self.assertTrue(srv.SHARED_KG_WRITE_DISABLED,
                            f"write-disabled should be true for {val!r}")
            # Read path stays open.
            self.assertEqual(srv.SHARED_KG_COLLECTION, "Some_KG")

    def test_write_disabled_falsy_keeps_writes_enabled(self):
        """SHARED_KG_WRITE_DISABLED=false / empty / 0 leaves writes enabled."""
        for val in ("false", "FALSE", "0", "no", ""):
            _clear_shared_env()
            srv = _fresh_server({
                "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
                "SHARED_KG_WRITE_DISABLED": val,
            })
            self.assertFalse(srv.SHARED_KG_WRITE_DISABLED,
                             f"write-disabled should be false for {val!r}")
            self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")

    def test_legacy_alias_truthy_disables_writes(self):
        """When only SHARED_KG_OPT_OUT is set, it gates writes."""
        for val in ("true", "True", "1", "yes"):
            _clear_shared_env()
            srv = _fresh_server({
                "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
                "SHARED_KG_OPT_OUT": val,
            })
            self.assertTrue(srv.SHARED_KG_WRITE_DISABLED,
                            f"legacy alias should disable writes for {val!r}")
            # Read still open.
            self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")

    def test_explicit_shared_kg_override(self):
        """A user can point SHARED_KG_COLLECTION at a custom name (e.g. for
        a private team-shared collection)."""
        srv = _fresh_server({
            "SHARED_KG_COLLECTION": "AcmeTeam_SharedKG",
            "SHARED_KG_WRITE_DISABLED": "false",
        })
        self.assertEqual(srv.SHARED_KG_COLLECTION, "AcmeTeam_SharedKG")


class SidecarPerCollectionTests(unittest.TestCase):
    """Per-collection sidecar resolution for tier formatting."""

    def setUp(self):
        _clear_shared_env()
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
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "KG_BASE_DIR": str(self.tmpdir / "project"),
            "SHARED_KG_NODE_FORMATS": str(self.shared_sidecar),
        })

        proj = srv._load_node_formats_for_collection("ProjectKG")
        shared = srv._load_node_formats_for_collection("VibecodedOrchestrator_KnowledgeGraph")

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
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
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
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
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
            "collection": "VibecodedOrchestrator_KnowledgeGraph",
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
    unit tests); just verifies the collection-selection branch and the
    write-disabled gate."""

    def setUp(self) -> None:
        _clear_shared_env()

    def test_scope_shared_targets_shared_collection_when_set(self):
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "false",
        })

        # Replicate the core selection logic: scope='shared' AND
        # SHARED_KG_COLLECTION set AND not equal to KG_COLLECTION → shared.
        # This is the same condition store_knowledge_node uses internally.
        scope = "shared"
        target = srv.KG_COLLECTION
        if scope == "shared" and srv.SHARED_KG_COLLECTION and srv.SHARED_KG_COLLECTION != srv.KG_COLLECTION:
            target = srv.SHARED_KG_COLLECTION

        self.assertEqual(target, "VibecodedOrchestrator_KnowledgeGraph")

    def test_write_gate_blocks_shared_writes_with_canonical_key(self):
        """SHARED_KG_WRITE_DISABLED=true must make _resolve_shared_kg_write_disabled
        return True — the store_knowledge_node implementation refuses the
        write at that point with a clear error, NOT a silent project-KG
        reroute."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "true",
        })
        # The collection still resolves (read path) — but the write gate is on.
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")
        self.assertTrue(srv._resolve_shared_kg_write_disabled())

    def test_write_gate_uses_legacy_alias_as_fallback(self):
        """When SHARED_KG_WRITE_DISABLED is unset, the legacy
        SHARED_KG_OPT_OUT acts as a fallback — keeps existing per-project
        env files honouring the write gate after the rename."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "true",
        })
        self.assertEqual(srv.SHARED_KG_COLLECTION, "VibecodedOrchestrator_KnowledgeGraph")
        self.assertTrue(srv._resolve_shared_kg_write_disabled())

    def test_write_gate_canonical_overrides_legacy(self):
        """SHARED_KG_WRITE_DISABLED=false explicitly RE-ENABLES writes
        even when SHARED_KG_OPT_OUT=true is still set in the env (e.g.
        from a stale .env that survived the rename)."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "false",
            "SHARED_KG_OPT_OUT": "true",
        })
        self.assertFalse(srv._resolve_shared_kg_write_disabled())

    def test_scope_project_always_targets_kg_collection(self):
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "false",
        })

        scope = "project"
        target = srv.KG_COLLECTION
        if scope == "shared" and srv.SHARED_KG_COLLECTION and srv.SHARED_KG_COLLECTION != srv.KG_COLLECTION:
            target = srv.SHARED_KG_COLLECTION

        self.assertEqual(target, "ProjectKG")


class ReadPathAlwaysIncludesSharedTests(unittest.TestCase):
    """Verify the collections-to-search list assembled by hybrid_search /
    semantic_graph_search includes the shared KG regardless of whether
    SHARED_KG_WRITE_DISABLED or the legacy SHARED_KG_OPT_OUT is on. We
    mirror the helper logic both readers use rather than spinning up a
    Weaviate instance."""

    def setUp(self) -> None:
        _clear_shared_env()

    def _collections_to_search(self, srv) -> list[str]:
        """Replicate the assembly used by both hybrid_search and
        semantic_graph_search to determine which collections to query."""
        out: list[str] = [srv.KG_COLLECTION]
        if srv.SHARED_KG_COLLECTION and srv.SHARED_KG_COLLECTION != srv.KG_COLLECTION:
            out.append(srv.SHARED_KG_COLLECTION)
        return out

    def test_shared_in_search_with_default_env(self):
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
        })
        self.assertIn("VibecodedOrchestrator_KnowledgeGraph",
                      self._collections_to_search(srv))

    def test_shared_in_search_when_writes_disabled(self):
        """The headline regression test: a write-disabled project must
        STILL read the shared KG."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_WRITE_DISABLED": "true",
        })
        self.assertIn("VibecodedOrchestrator_KnowledgeGraph",
                      self._collections_to_search(srv))

    def test_shared_in_search_when_legacy_optout_set(self):
        """Legacy SHARED_KG_OPT_OUT=true used to ZERO the read collection;
        after the refactor it only gates writes, so reads must still see
        the shared collection."""
        srv = _fresh_server({
            "KG_COLLECTION": "ProjectKG",
            "SHARED_KG_COLLECTION": "VibecodedOrchestrator_KnowledgeGraph",
            "SHARED_KG_OPT_OUT": "true",
        })
        self.assertIn("VibecodedOrchestrator_KnowledgeGraph",
                      self._collections_to_search(srv))


if __name__ == "__main__":
    unittest.main()
