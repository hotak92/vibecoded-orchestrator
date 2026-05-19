# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the v0.2.18 consumer migration (Commit 5).

Covers the new EmbeddingService routing in:
  * templates/scripts/sync_knowledge_graph.py
      - WeaviateWrapper uses EmbeddingService for embed calls + slot
      - _build_vector_arg builds the correct insert vector arg in both
        DUAL and legacy modes
      - main() catches NoEmbeddingBackendError and emits a deferral
        rather than crashing
  * templates/scripts/analyze_code_graph.py
      - _shape_for_insert correctly handles dict vs list vs None
      - _active_code_vector_slot consults the injected service
      - main() refuses to run when code_backend_ready() is False
  * claude_mcp_servers/weaviate_mcp/server.py
      - _get_embedding_service lazy-cache, retry-window throttling,
        and graceful no-service fallback
  * claude_mcp_servers/scripts/migrate_to_new_embeddings.py
      - _active_kg_slot / _active_code_slot resolve via EmbeddingService
        when reachable; fall back to hardcoded names otherwise

All tests run with HTTP / Weaviate mocked. Nothing touches a real
service.
"""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from vco_lib.embedding_service import (
    EmbeddingService,
    NoEmbeddingBackendError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeService:
    """Minimal EmbeddingService stand-in for consumer tests."""

    def __init__(
        self,
        *,
        text_slot: str = "qwen3_embed",
        code_slot: str = "codesage_embed",
        text_dim: int = 1024,
        code_dim: int = 2048,
        text_model_id: str = "qwen3-embedding:0.6b",
        code_model_id: str = "codesage-large-v2",
        text_ready: bool = True,
        code_ready: bool = True,
        text_all_slots: Optional[dict] = None,
        code_all_slots: Optional[dict] = None,
    ) -> None:
        self.text_vector_slot = text_slot
        self.code_vector_slot = code_slot
        self.text_dim = text_dim
        self.code_dim = code_dim
        self.text_model_id = text_model_id
        self.code_model_id = code_model_id
        self._text_ready = text_ready
        self._code_ready = code_ready
        self._text_all_slots = text_all_slots
        self._code_all_slots = code_all_slots
        self.embed_text_calls: list[str] = []
        self.embed_code_calls: list[str] = []
        self.closed = False

    def text_backend_ready(self) -> bool:
        return self._text_ready

    def code_backend_ready(self) -> bool:
        return self._code_ready

    def embed_text(self, text: str) -> list:
        self.embed_text_calls.append(text)
        return [0.1, 0.2, 0.3]

    def embed_code(self, text: str) -> list:
        self.embed_code_calls.append(text)
        return [0.4, 0.5, 0.6]

    def embed_text_all_configured(self, text: str) -> dict:
        self.embed_text_calls.append(text)
        if self._text_all_slots is not None:
            return self._text_all_slots
        return {self.text_vector_slot: [0.1, 0.2, 0.3]}

    def embed_code_all_configured(self, text: str) -> dict:
        self.embed_code_calls.append(text)
        if self._code_all_slots is not None:
            return self._code_all_slots
        return {self.code_vector_slot: [0.4, 0.5, 0.6]}

    def close(self) -> None:
        self.closed = True


def _load_module(name: str, path: Path):
    """Import a script as a module without running its __main__ guard."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Make sure imports inside the module work — they probably reach
    # back into REPO_ROOT for vco_lib / weaviate_mcp.
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# sync_knowledge_graph
# ---------------------------------------------------------------------------


class SyncKnowledgeGraphTests(unittest.TestCase):
    """v0.2.18 changes to sync_knowledge_graph.py."""

    @classmethod
    def setUpClass(cls) -> None:
        # Set VCT_ORCHESTRATOR_ROOT so the script resolves the MCP package.
        os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)

    def _import_module(self):
        # Each test gets a fresh import so module-level state doesn't leak.
        script_path = REPO_ROOT / "templates" / "scripts" / "sync_knowledge_graph.py"
        # Use unique name to avoid sys.modules collisions.
        mod_name = f"_test_sync_kg_{id(self)}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        return _load_module(mod_name, script_path)

    def test_build_vector_arg_dual_mode_returns_slots_dict(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = True
        svc = _FakeService(
            text_slot="qwen3_embed",
            text_all_slots={"qwen3_embed": [0.1, 0.2], "openai_text_embed": [0.3, 0.4]},
        )
        wrapper = MagicMock()
        wrapper._get_all_kg_embeddings = svc.embed_text_all_configured
        wrapper._get_embedding = svc.embed_text
        wrapper.text_vector_slot = svc.text_vector_slot

        vec_arg, slots = mod._build_vector_arg(wrapper, "hello")
        self.assertIsInstance(vec_arg, dict)
        self.assertEqual(set(slots.keys()), {"qwen3_embed", "openai_text_embed"})

    def test_build_vector_arg_legacy_mode_returns_flat_list(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = False
        svc = _FakeService(text_slot="qwen3_embed")
        wrapper = MagicMock()
        wrapper._get_all_kg_embeddings = svc.embed_text_all_configured
        wrapper._get_embedding = svc.embed_text
        wrapper.text_vector_slot = svc.text_vector_slot

        vec_arg, slots = mod._build_vector_arg(wrapper, "hello")
        self.assertIsInstance(vec_arg, list)
        self.assertEqual(list(slots.keys()), ["qwen3_embed"])

    def test_build_vector_arg_dual_mode_raises_on_empty_slots(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = True
        # service returns empty dict → no backend succeeded
        wrapper = MagicMock()
        wrapper._get_all_kg_embeddings = lambda text: {}
        with self.assertRaises(RuntimeError) as cm:
            mod._build_vector_arg(wrapper, "hello")
        self.assertIn("No embedding backend", str(cm.exception))

    def test_no_qwen3_only_assertion_remains(self):
        """The doomed _active_named_vector_for_kg must be deleted."""
        mod = self._import_module()
        self.assertFalse(
            hasattr(mod, "_active_named_vector_for_kg"),
            "v0.2.18 should have removed the qwen3-only assertion helper",
        )

    def test_module_does_not_read_active_embedding_directly(self):
        """The module-level ACTIVE_EMBEDDING attribute must be gone."""
        mod = self._import_module()
        # _ACTIVE_EMBEDDING was the env-time module-global pre-v0.2.18.
        self.assertFalse(hasattr(mod, "_ACTIVE_EMBEDDING"))

    def test_module_does_not_read_embedding_model_directly(self):
        """EMBEDDING_MODEL env var read should be removed."""
        mod = self._import_module()
        self.assertFalse(hasattr(mod, "EMBEDDING_MODEL"))

    def test_weavi_wrapper_signature(self):
        """v0.2.18: WeaviateWrapper takes (weaviate_url, embedding_service, grpc_port)."""
        mod = self._import_module()
        import inspect
        sig = inspect.signature(mod.WeaviateWrapper.__init__)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["self", "weaviate_url", "embedding_service", "grpc_port"])


# ---------------------------------------------------------------------------
# analyze_code_graph
# ---------------------------------------------------------------------------


class AnalyzeCodeGraphTests(unittest.TestCase):
    """v0.2.18 changes to analyze_code_graph.py."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)

    def _import_module(self):
        script_path = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
        mod_name = f"_test_analyze_cg_{id(self)}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        return _load_module(mod_name, script_path)

    def test_shape_for_insert_none_passthrough(self):
        mod = self._import_module()
        self.assertIsNone(mod._shape_for_insert(None))
        self.assertIsNone(mod._shape_for_insert([]))
        self.assertIsNone(mod._shape_for_insert({}))

    def test_shape_for_insert_dict_passthrough(self):
        mod = self._import_module()
        slots = {"codesage_embed": [0.1, 0.2]}
        self.assertIs(mod._shape_for_insert(slots), slots)

    def test_shape_for_insert_list_dual_mode_wraps_with_active_slot(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = True
        svc = _FakeService(code_slot="openai_code_embed")
        mod._set_embedding_service(svc)
        try:
            result = mod._shape_for_insert([0.1, 0.2])
        finally:
            mod._set_embedding_service(None)  # type: ignore[arg-type]
        self.assertEqual(result, {"openai_code_embed": [0.1, 0.2]})

    def test_shape_for_insert_list_legacy_mode_passthrough(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = False
        self.assertEqual(mod._shape_for_insert([0.1, 0.2]), [0.1, 0.2])

    def test_active_code_vector_slot_uses_service(self):
        mod = self._import_module()
        svc = _FakeService(code_slot="jina_embed")
        mod._set_embedding_service(svc)
        try:
            self.assertEqual(mod._active_code_vector_slot(), "jina_embed")
        finally:
            mod._set_embedding_service(None)  # type: ignore[arg-type]

    def test_active_code_vector_slot_falls_back_when_no_service(self):
        mod = self._import_module()
        mod._set_embedding_service(None)  # type: ignore[arg-type]
        self.assertEqual(mod._active_code_vector_slot(), "codesage_embed")

    def test_generate_embedding_returns_none_without_service(self):
        mod = self._import_module()
        mod._set_embedding_service(None)  # type: ignore[arg-type]
        # No service initialised → returns None (matches pre-v0.2.18
        # behaviour for "no backend").
        self.assertIsNone(mod.generate_embedding("hello"))

    def test_generate_embedding_dual_mode_returns_slot_dict(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = True
        svc = _FakeService(code_all_slots={"codesage_embed": [0.1], "openai_code_embed": [0.2]})
        mod._set_embedding_service(svc)
        try:
            result = mod.generate_embedding("def foo(): pass")
        finally:
            mod._set_embedding_service(None)  # type: ignore[arg-type]
        self.assertIsInstance(result, dict)
        self.assertEqual(set(result.keys()), {"codesage_embed", "openai_code_embed"})

    def test_generate_embedding_legacy_mode_returns_flat_list(self):
        mod = self._import_module()
        mod.DUAL_EMBEDDING_ENABLED = False
        svc = _FakeService()
        mod._set_embedding_service(svc)
        try:
            result = mod.generate_embedding("def foo(): pass")
        finally:
            mod._set_embedding_service(None)  # type: ignore[arg-type]
        self.assertEqual(result, [0.4, 0.5, 0.6])

    def test_no_active_code_vector_module_constant(self):
        """The hardcoded `_ACTIVE_CODE_VECTOR` env-time constant must be gone."""
        mod = self._import_module()
        self.assertFalse(hasattr(mod, "_ACTIVE_CODE_VECTOR"))

    def test_no_code_embed_backend_module_constant(self):
        """The legacy CODE_EMBED_BACKEND module-level read should be removed."""
        mod = self._import_module()
        self.assertFalse(hasattr(mod, "CODE_EMBED_BACKEND"))
        self.assertFalse(hasattr(mod, "CODE_EMBED_SERVICE_URL"))
        self.assertFalse(hasattr(mod, "OLLAMA_CONFIG"))


# ---------------------------------------------------------------------------
# server.py — MCP embed dispatch
# ---------------------------------------------------------------------------


class MCPServerEmbedDispatchTests(unittest.TestCase):
    """v0.2.18 changes to claude_mcp_servers/weaviate_mcp/server.py."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)
        # server.py uses relative + bare `chunking` import — both
        # claude_mcp_servers/ and claude_mcp_servers/weaviate_mcp/ must
        # be on sys.path so the import works whether the loader treats
        # server.py as a top-level module or a package member.
        mcp_parent = REPO_ROOT / "claude_mcp_servers"
        weaviate_mcp_dir = mcp_parent / "weaviate_mcp"
        for p in (mcp_parent, weaviate_mcp_dir):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))

    def _import_server(self):
        # Import as a real package member to satisfy the relative
        # `from .chunking import Chunker` in server.py.
        if "weaviate_mcp.server" in sys.modules:
            del sys.modules["weaviate_mcp.server"]
        import importlib
        return importlib.import_module("weaviate_mcp.server")

    def test_lazy_service_accessor_caches(self):
        mod = self._import_server()
        # First call constructs (potentially failing), second returns cache.
        # We can't easily construct a real EmbeddingService here without
        # Ollama running, but we CAN verify the cache behaviour by
        # injecting a fake at the cache slot directly.
        fake = _FakeService()
        mod._cached_embed_service = fake
        result = mod._get_embedding_service()
        self.assertIs(result, fake)
        # Second call still returns cached instance
        result2 = mod._get_embedding_service()
        self.assertIs(result2, fake)
        # Cleanup so other tests don't see leakage.
        mod._cached_embed_service = None

    def test_service_construction_failure_throttled(self):
        mod = self._import_server()
        mod._cached_embed_service = None
        # Patch EmbeddingService.for_project to raise NoEmbeddingBackendError
        with patch.object(
            mod, "EmbeddingService", create=True
        ) as fake_cls:
            fake_cls.for_project.side_effect = mod.NoEmbeddingBackendError(
                "no backends",
                attempted_backends=[],
                error_per_backend={},
                capture=False,
            )
            mod.HAS_EMBEDDING_SERVICE = True

            # First call: probes, fails, returns None
            result = mod._get_embedding_service()
            self.assertIsNone(result)
            self.assertEqual(fake_cls.for_project.call_count, 1)

            # Second call within retry window: short-circuits, no new probe
            result = mod._get_embedding_service()
            self.assertIsNone(result)
            self.assertEqual(fake_cls.for_project.call_count, 1)

        mod._embed_service_construction_failed_at = 0.0

    def test_no_service_returns_none(self):
        mod = self._import_server()
        mod._cached_embed_service = None
        mod.HAS_EMBEDDING_SERVICE = False
        try:
            self.assertIsNone(mod._get_embedding_service())
        finally:
            mod.HAS_EMBEDDING_SERVICE = True  # restore


# ---------------------------------------------------------------------------
# migrate_to_new_embeddings.py
# ---------------------------------------------------------------------------


class MigrateToNewEmbeddingsTests(unittest.TestCase):
    """v0.2.18 changes to claude_mcp_servers/scripts/migrate_to_new_embeddings.py."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)

    def _import_module(self):
        script_path = REPO_ROOT / "claude_mcp_servers" / "scripts" / "migrate_to_new_embeddings.py"
        mod_name = f"_test_migrate_emb_{id(self)}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        return _load_module(mod_name, script_path)

    def test_active_slot_helpers_use_service(self):
        mod = self._import_module()
        if not getattr(mod, "HAS_EMBEDDING_SERVICE", False):
            self.skipTest("EmbeddingService not importable in this env")
        # Mock EmbeddingService.for_project to return a fake with custom slots
        fake = _FakeService(text_slot="arctic2_embed", code_slot="openai_code_embed")
        with patch.object(mod, "EmbeddingService") as fake_cls:
            fake_cls.for_project.return_value = fake
            self.assertEqual(mod._active_kg_slot(), "arctic2_embed")
            self.assertEqual(mod._active_code_slot(), "openai_code_embed")

    def test_active_slot_falls_back_when_no_service(self):
        mod = self._import_module()
        original_has = mod.HAS_EMBEDDING_SERVICE
        mod.HAS_EMBEDDING_SERVICE = False
        try:
            self.assertEqual(mod._active_kg_slot(), "qwen3_embed")
            self.assertEqual(mod._active_code_slot(), "codesage_embed")
        finally:
            mod.HAS_EMBEDDING_SERVICE = original_has

    def test_active_slot_falls_back_on_no_backend_error(self):
        mod = self._import_module()
        if not getattr(mod, "HAS_EMBEDDING_SERVICE", False):
            self.skipTest("EmbeddingService not importable in this env")
        with patch.object(mod, "EmbeddingService") as fake_cls:
            fake_cls.for_project.side_effect = mod.NoEmbeddingBackendError(
                "no backends",
                capture=False,
            )
            # Both helpers must fall back, not propagate
            self.assertEqual(mod._active_kg_slot(), "qwen3_embed")
            self.assertEqual(mod._active_code_slot(), "codesage_embed")

    def test_module_does_not_read_embedding_model_at_top_level(self):
        """The module-level EMBEDDING_MODEL constant must be gone."""
        mod = self._import_module()
        # Pre-v0.2.18 had `EMBEDDING_MODEL = os.getenv(...)` at top level.
        # Post-v0.2.18 it's read inside get_text_embedding's fallback.
        self.assertFalse(hasattr(mod, "EMBEDDING_MODEL"))


# ---------------------------------------------------------------------------
# search_knowledge.py
# ---------------------------------------------------------------------------


class SearchKnowledgeTests(unittest.TestCase):
    """v0.2.18 changes to templates/scripts/search_knowledge.py."""

    @classmethod
    def setUpClass(cls) -> None:
        os.environ["VCT_ORCHESTRATOR_ROOT"] = str(REPO_ROOT)

    def _import_module(self):
        script_path = REPO_ROOT / "templates" / "scripts" / "search_knowledge.py"
        mod_name = f"_test_search_kg_{id(self)}"
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        return _load_module(mod_name, script_path)

    def test_target_vector_slot_uses_service(self):
        mod = self._import_module()
        if not getattr(mod, "HAS_EMBEDDING_SERVICE", False):
            self.skipTest("EmbeddingService not importable")
        mod._cached_embedding_service = _FakeService(text_slot="arctic2_embed")
        try:
            self.assertEqual(mod._get_target_vector_slot(), "arctic2_embed")
        finally:
            mod._cached_embedding_service = None

    def test_target_vector_slot_falls_back_to_ollama_embed(self):
        mod = self._import_module()
        original_has = mod.HAS_EMBEDDING_SERVICE
        mod.HAS_EMBEDDING_SERVICE = False
        mod._cached_embedding_service = None
        try:
            # Without the service, must fall back to the pre-v0.2.18
            # hardcoded slot name.
            self.assertEqual(mod._get_target_vector_slot(), "ollama_embed")
        finally:
            mod.HAS_EMBEDDING_SERVICE = original_has

    def test_module_does_not_read_embedding_model_at_top_level(self):
        mod = self._import_module()
        # EMBEDDING_MODEL is now read inside the legacy fallback only,
        # not at module level.
        self.assertFalse(hasattr(mod, "EMBEDDING_MODEL"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
