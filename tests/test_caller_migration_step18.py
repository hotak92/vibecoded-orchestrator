# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for v0.2.21 Step 18 — caller migration to project_config resolver.

Verifies that the migrated callers in templates/scripts/*.py and
claude_mcp_servers/weaviate_mcp/server.py prefer the launcher's vct-hub
(via vco_lib.project_config.resolve) over direct os.getenv reads, and
fall back to env when the hub is unreachable.

Strategy: monkeypatch vco_lib.project_config.resolve to (a) return a
known ProjectConfig (hub-up scenario), (b) raise HubUnreachable
(hub-down scenario). Reimport each caller module and assert the
module-level constants reflect the right source.

We do NOT stand up a real hub — the resolver-client tests in
test_project_config.py cover that wire.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "claude_mcp_servers"))


def _fake_project_config(
    *,
    kg_collection: str = "Hub_KnowledgeGraph",
    shared_kg_collection: str = "Hub_Shared_KnowledgeGraph",
    development_collection: str = "Hub_Development",
    code_graph_project: str = "hubproject",
    active_embedding: str = "qwen3",
    project_display_name: str = "HubProject",
    project_slug: str = "hubproject",
    kg_access_list: tuple[str, ...] = (),
):
    """Build a fake ProjectConfig dataclass for resolver-stub return.

    ``retrieval_tuning`` and ``schema_version`` are intentionally OMITTED:
    they have calibrated defaults on ``ProjectConfig`` (see the field-
    declaration docstrings in ``vco_lib/project_config.py``), so direct
    callers don't need to know about them. Tests that DO need to override
    them should pass an explicit ``retrieval_tuning=RetrievalTuning(...)``.
    """
    from vco_lib.project_config import EmbeddingModels, ProjectConfig
    return ProjectConfig(
        project_id="11111111-2222-3333-4444-555555555555",
        project_path="/fake/project",
        project_slug=project_slug,
        project_display_name=project_display_name,
        code_graph_project=code_graph_project,
        code_graph_collection_prefix="Hubproject",
        kg_collection=kg_collection,
        shared_kg_collection=shared_kg_collection,
        development_collection=development_collection,
        active_embedding=active_embedding,
        embedding_models=EmbeddingModels(
            text="qwen3-embedding:0.6b", code="CodeSage-Large-v2"
        ),
        kg_access_list=tuple(kg_access_list),
        codegraph_access_list=(),
        weaviate_url="http://localhost:8081",
        ollama_url="http://localhost:11435",
        grpc_port=50052,
        shared_kg_write_disabled=False,
    )


def _purge_modules(prefixes: tuple[str, ...]) -> None:
    """Drop cached imports so we can reimport with fresh module-level state."""
    for mod in list(sys.modules):
        if any(mod == p or mod.startswith(p + ".") for p in prefixes):
            del sys.modules[mod]


def _clear_relevant_env() -> None:
    """Strip env vars the migrated callers read so each test starts clean."""
    for k in (
        "KG_COLLECTION",
        "SHARED_KG_COLLECTION",
        "DEVELOPMENT_COLLECTION",
        "ACTIVE_EMBEDDING",
        "CODE_GRAPH_PROJECT",
        "PROJECT_NAME",
        "VCT_KG_ACCESS_LIST",
        "KG_BASE_DIR",
        # v0.2.89 BUG 3: the new non-leaking root channel outranks
        # KG_BASE_DIR in sync_knowledge_graph.py — strip it too so a
        # host shell can never steer the module-load-time resolution.
        "KG_SYNC_PROJECT_ROOT",
    ):
        os.environ.pop(k, None)


class WeaviateMcpServerResolverTests(unittest.TestCase):
    """The MCP server is the highest-value migration. Pin both paths."""

    def setUp(self) -> None:
        _clear_relevant_env()

    def tearDown(self) -> None:
        _clear_relevant_env()
        _purge_modules(("weaviate_mcp", "vco_lib.project_config"))

    def _reimport_server(self):
        _purge_modules(("weaviate_mcp",))
        return importlib.import_module("weaviate_mcp.server")

    def test_hub_up_resolver_values_win(self):
        """Hub reachable → KG_COLLECTION/etc reflect resolver, not env."""
        # Env says one thing; hub will say another.
        os.environ["KG_COLLECTION"] = "EnvKG"
        os.environ["SHARED_KG_COLLECTION"] = "EnvShared"
        os.environ["DEVELOPMENT_COLLECTION"] = "EnvDev"
        os.environ["ACTIVE_EMBEDDING"] = "openai"
        os.environ["CODE_GRAPH_PROJECT"] = "envproj"

        fake_cfg = _fake_project_config(
            kg_collection="HubKG",
            shared_kg_collection="HubShared",
            development_collection="HubDev",
            active_embedding="qwen3",
            code_graph_project="hubproj",  # slug alias (kept for cfg shape)
        )

        # Stub resolve() BEFORE reimporting the server.
        with mock.patch(
            "vco_lib.project_config.resolve",
            return_value=fake_cfg,
        ):
            server = self._reimport_server()

        self.assertEqual(server.KG_COLLECTION, "HubKG")
        self.assertEqual(server.SHARED_KG_COLLECTION, "HubShared")
        self.assertEqual(server.DEVELOPMENT_COLLECTION, "HubDev")
        self.assertEqual(server.ACTIVE_EMBEDDING, "qwen3")
        # v0.2.23 W3 (2026-05-21): server.CODE_GRAPH_PROJECT now sources
        # from cfg.code_graph_collection_prefix (the binding-row truth),
        # NOT cfg.code_graph_project (the slug alias). The fixture's
        # `code_graph_collection_prefix="Hubproject"` is what propagates.
        self.assertEqual(server.CODE_GRAPH_PROJECT, "Hubproject")

    def test_hub_unreachable_falls_back_to_env(self):
        """Hub raises → KG_COLLECTION/etc reflect os.getenv."""
        os.environ["KG_COLLECTION"] = "EnvKG"
        os.environ["SHARED_KG_COLLECTION"] = "EnvShared"
        os.environ["DEVELOPMENT_COLLECTION"] = "EnvDev"
        os.environ["ACTIVE_EMBEDDING"] = "openai"
        os.environ["CODE_GRAPH_PROJECT"] = "envproj"

        from vco_lib.project_config import HubUnreachable
        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=HubUnreachable("launcher not running"),
        ):
            server = self._reimport_server()

        self.assertEqual(server.KG_COLLECTION, "EnvKG")
        self.assertEqual(server.SHARED_KG_COLLECTION, "EnvShared")
        self.assertEqual(server.DEVELOPMENT_COLLECTION, "EnvDev")
        self.assertEqual(server.ACTIVE_EMBEDDING, "openai")
        self.assertEqual(server.CODE_GRAPH_PROJECT, "envproj")

    def test_hub_unreachable_uses_pre_v0221_defaults_on_empty_env(self):
        """Hub down + env unset → pre-v0.2.21 hardcoded defaults preserved."""
        from vco_lib.project_config import HubUnreachable
        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=HubUnreachable("no token"),
        ):
            server = self._reimport_server()

        self.assertEqual(server.KG_COLLECTION, "ClaudeKnowledgeGraph")
        # SHARED_KG_COLLECTION default in v0.2.12+ is the orchestrator
        # collection — preserved across the migration. v0.2.23 B1 flipped
        # the canonical casing to capital-C "VibeCoded" to match the
        # brand spelling; case-insensitive adoption in install.py keeps
        # existing on-disk lowercase-c classes untouched.
        self.assertEqual(
            server.SHARED_KG_COLLECTION,
            "VibeCodedOrchestrator_KnowledgeGraph",
        )
        self.assertEqual(server.DEVELOPMENT_COLLECTION, "")
        self.assertEqual(server.ACTIVE_EMBEDDING, "qwen3")
        self.assertEqual(server.CODE_GRAPH_PROJECT, "")

    def test_hub_kg_access_list_replaces_env_csv(self):
        """When the hub returns kg_access_list, peer collections derive
        from it (canonical names); VCT_KG_ACCESS_LIST is ignored."""
        os.environ["VCT_KG_ACCESS_LIST"] = "LegacyPeerA,LegacyPeerB"

        fake_cfg = _fake_project_config(
            kg_collection="HubKG",
            shared_kg_collection="HubShared",
            kg_access_list=("HubKG", "HubShared", "Peer1_KnowledgeGraph"),
        )

        with mock.patch(
            "vco_lib.project_config.resolve",
            return_value=fake_cfg,
        ):
            server = self._reimport_server()

        peers = server._kg_peer_collections()
        # Self + shared filtered out → only the actual peer remains.
        self.assertEqual(peers, ["Peer1_KnowledgeGraph"])
        # And it does NOT contain the legacy CSV entries.
        self.assertNotIn("Legacypeera_KnowledgeGraph", peers)
        self.assertNotIn("Legacypeerb_KnowledgeGraph", peers)

    def test_hub_down_kg_access_list_falls_back_to_env_csv(self):
        """When the hub is unreachable, peer collections derive from the
        legacy VCT_KG_ACCESS_LIST CSV (env-fallback path)."""
        os.environ["VCT_KG_ACCESS_LIST"] = "LegacyPeer"

        from vco_lib.project_config import HubUnreachable
        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=HubUnreachable("launcher offline"),
        ):
            server = self._reimport_server()

        peers = server._kg_peer_collections()
        # Sanitized + suffixed → first letter capitalized (rest of casing
        # preserved by _sanitize_collection_prefix).
        self.assertEqual(peers, ["LegacyPeer_KnowledgeGraph"])


class TemplateScriptCallerTests(unittest.TestCase):
    """Pin the resolver-prefer-env-fallback contract for one
    representative Group A script (sync_knowledge_graph.py) — it
    exercises the most fields (KG + Development) and the same idiom
    runs in every sibling.
    """

    def setUp(self) -> None:
        _clear_relevant_env()

    def tearDown(self) -> None:
        _clear_relevant_env()

    def test_resolve_collections_prefers_hub(self):
        """The migrated _resolve_collections() returns the hub values
        when the hub is reachable."""
        # Load the script module directly via importlib so the test
        # doesn't depend on its CLI side-effects.
        import importlib.util

        script_path = PROJECT_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        spec = importlib.util.spec_from_file_location(
            "_step18_sync_knowledge_graph", script_path
        )
        assert spec is not None and spec.loader is not None

        fake_cfg = _fake_project_config(
            kg_collection="HubProjectKG",
            development_collection="HubProjectDev",
        )

        # Patch resolve BEFORE exec to ensure the module-load-time
        # call sees the stub. The module reads env at import; we want
        # the resolver path to win.
        os.environ["KG_COLLECTION"] = "EnvKG"
        os.environ["DEVELOPMENT_COLLECTION"] = "EnvDev"

        with mock.patch(
            "vco_lib.project_config.resolve",
            return_value=fake_cfg,
        ):
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except SystemExit:
                # Some CLI scripts exit at end of module load if invoked
                # without argv; tolerate it.
                pass

        self.assertEqual(module.COLLECTION_NAME, "HubProjectKG")
        self.assertEqual(module.DEV_COLLECTION_NAME, "HubProjectDev")

    def test_resolve_collections_falls_back_to_env(self):
        """When resolve() raises, _resolve_collections returns env."""
        import importlib.util

        script_path = PROJECT_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        spec = importlib.util.spec_from_file_location(
            "_step18_sync_knowledge_graph_envfb", script_path
        )
        assert spec is not None and spec.loader is not None

        os.environ["KG_COLLECTION"] = "EnvKG"
        os.environ["DEVELOPMENT_COLLECTION"] = "EnvDev"

        from vco_lib.project_config import HubUnreachable
        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=HubUnreachable("hub down"),
        ):
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except SystemExit:
                pass

        self.assertEqual(module.COLLECTION_NAME, "EnvKG")
        self.assertEqual(module.DEV_COLLECTION_NAME, "EnvDev")

    def test_resolve_collections_keys_hub_off_kg_base_dir_for_cross_project_seed(self):
        """A manual cross-project seed (KG_BASE_DIR set to a DIFFERENT project
        root) must resolve the hub against THAT root, not the script's own
        parent tree — so the collection name matches the project whose
        knowledge/ is being walked, fixing the file-root vs collection-name
        asymmetry. (Hub precedence is preserved; only the resolution TARGET
        changes.)"""
        import importlib.util

        script_path = PROJECT_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        spec = importlib.util.spec_from_file_location(
            "_step18_sync_knowledge_graph_crossseed", script_path
        )
        assert spec is not None and spec.loader is not None

        target_root = "/some/other/project/root"
        os.environ["KG_BASE_DIR"] = target_root
        # An ambient KG_COLLECTION must NOT win over the hub (v0.2.21 contract);
        # the hub — queried against the TARGET root — is authoritative.
        os.environ["KG_COLLECTION"] = "StaleAmbientKG"

        seen_roots: list[Path] = []

        def _capture_resolve(root):
            seen_roots.append(Path(root))
            return _fake_project_config(
                kg_collection="TargetProjectKG",
                development_collection="TargetProjectDev",
            )

        with mock.patch(
            "vco_lib.project_config.resolve",
            side_effect=_capture_resolve,
        ):
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)  # type: ignore[union-attr]
            except SystemExit:
                pass

        # The hub was queried against KG_BASE_DIR, not the script's own tree.
        self.assertIn(Path(target_root), seen_roots)
        self.assertEqual(module.COLLECTION_NAME, "TargetProjectKG")
        self.assertEqual(module.DEV_COLLECTION_NAME, "TargetProjectDev")


if __name__ == "__main__":
    unittest.main()
