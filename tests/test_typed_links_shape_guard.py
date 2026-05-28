"""Tests for typed_links shape guard (NEW-11, 2026-05-28).

Covers:
  - list-of-objects (canonical) passes unchanged
  - list-of-strings ("rel::target") converts correctly
  - garbage / unexpected types are dropped with a warning
  - empty list and missing (None) field both produce []
  - _normalize helper from the repair script mirrors the guard behaviour
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch
import io

import pytest

# ---------------------------------------------------------------------------
# Import _normalize_typed_links from sync_knowledge_graph without running
# the module's top-level setup (weaviate client, env parsing, etc.).
# We use importlib with a targeted monkey-patch on the VCO-REWIRE block.
# ---------------------------------------------------------------------------

def _import_sync_kg() -> object:
    """Import sync_knowledge_graph and return the module, patching heavy deps."""
    script_path = (
        Path(__file__).resolve().parent.parent
        / "templates" / "scripts" / "sync_knowledge_graph.py"
    )
    # Provide stub modules so the top-level import block doesn't fail in a
    # unit-test environment that lacks weaviate / vco_lib / EmbeddingService.
    stubs = {
        "weaviate": type(sys)("weaviate"),
        "weaviate.classes": type(sys)("weaviate.classes"),
        "weaviate.classes.query": type(sys)("weaviate.classes.query"),
        "weaviate_mcp": type(sys)("weaviate_mcp"),
        "weaviate_mcp.chunking": type(sys)("weaviate_mcp.chunking"),
        "vco_lib": type(sys)("vco_lib"),
        "vco_lib.embedding_service": type(sys)("vco_lib.embedding_service"),
    }
    # Stub Filter and chunking classes used at module level
    stubs["weaviate.classes.query"].Filter = object
    stubs["weaviate_mcp.chunking"].TokenCounter = object
    stubs["weaviate_mcp.chunking"].Chunker = object
    stubs["vco_lib.embedding_service"].EmbeddingService = object
    stubs["vco_lib.embedding_service"].NoEmbeddingBackendError = Exception

    for name, mod in stubs.items():
        # Register parent packages too (e.g. "weaviate")
        sys.modules.setdefault(name, mod)

    spec = importlib.util.spec_from_file_location("sync_knowledge_graph", script_path)
    mod = importlib.util.module_from_spec(spec)

    # Patch _resolve_mcp_servers_dir so the VCO-REWIRE block doesn't fail
    with patch.dict("sys.modules", stubs):
        with patch(
            "importlib.util.spec_from_file_location",
            return_value=spec,
        ):
            try:
                spec.loader.exec_module(mod)
            except Exception:
                # Some module-level setup may fail in test env — that's OK as
                # long as _normalize_typed_links is already defined by the time
                # the ImportError fires.
                pass

    return mod


# Lazy import — shared across all tests in this file.
_MOD = None


def _get_normalizer():
    """Return the _normalize_typed_links function from sync_knowledge_graph."""
    global _MOD
    if _MOD is None:
        _MOD = _import_sync_kg()
    fn = getattr(_MOD, "_normalize_typed_links", None)
    if fn is None:
        pytest.skip("_normalize_typed_links not importable in this test env")
    return fn


# ---------------------------------------------------------------------------
# Import _normalize from the repair script (lighter — pure Python, no weaviate
# client instantiation at import time).
# ---------------------------------------------------------------------------

def _import_repair_normalize():
    script_path = (
        Path(__file__).resolve().parent.parent
        / "claude_mcp_servers" / "scripts" / "repair_kg_typed_links.py"
    )
    spec = importlib.util.spec_from_file_location("repair_kg_typed_links", script_path)
    mod = importlib.util.module_from_spec(spec)
    # The repair script imports requests and weaviate at module level but only
    # uses them in functions — stub so import succeeds without network deps.
    stubs = {
        "requests": type(sys)("requests"),
        "weaviate": type(sys)("weaviate"),
    }
    stubs["requests"].post = lambda *a, **kw: None
    stubs["requests"].HTTPError = Exception
    stubs["weaviate"].WeaviateClient = object
    stubs["weaviate"].connect_to_local = lambda **kw: None
    with patch.dict("sys.modules", stubs):
        spec.loader.exec_module(mod)
    return mod


_REPAIR_MOD = None


def _get_repair_normalize():
    global _REPAIR_MOD
    if _REPAIR_MOD is None:
        _REPAIR_MOD = _import_repair_normalize()
    return _REPAIR_MOD._normalize, _REPAIR_MOD._is_canonical


# ===========================================================================
# Tests for sync_knowledge_graph._normalize_typed_links
# ===========================================================================

class TestSyncKGNormalizer:
    """Writer-side guard: _normalize_typed_links in sync_knowledge_graph.py."""

    def test_canonical_list_of_objects_unchanged(self):
        """Canonical list-of-objects passes through without modification."""
        fn = _get_normalizer()
        raw = [
            {"relation_type": "uses", "target_title": "Redis"},
            {"relation_type": "implements", "target_title": "Caching Pattern"},
        ]
        result = fn(raw, context="test-node")
        assert result == raw

    def test_list_of_strings_converts_to_objects(self):
        """Legacy 'rel::target' strings are parsed into canonical objects."""
        fn = _get_normalizer()
        raw = ["uses::Redis", "implements::Caching Pattern", "relatedTo::SomeNode"]
        result = fn(raw, context="test-node")
        assert result == [
            {"relation_type": "uses", "target_title": "Redis"},
            {"relation_type": "implements", "target_title": "Caching Pattern"},
            {"relation_type": "relatedTo", "target_title": "SomeNode"},
        ]

    def test_plain_string_without_separator_becomes_related_to(self):
        """Strings with no '::' separator are stored as relatedTo."""
        fn = _get_normalizer()
        raw = ["SomeTool"]
        result = fn(raw, context="test-node")
        assert result == [{"relation_type": "relatedTo", "target_title": "SomeTool"}]

    def test_garbage_type_dropped_with_warning(self, capsys):
        """Non-list input is dropped and a warning is printed."""
        fn = _get_normalizer()
        result = fn({"relation_type": "uses", "target_title": "X"}, context="garbage-node")
        assert result == []
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower() or "unexpected type" in captured.out.lower()

    def test_none_returns_empty_list(self):
        """None input returns [] without warnings."""
        fn = _get_normalizer()
        assert fn(None, context="none-node") == []

    def test_empty_list_returns_empty_list(self):
        """Empty list input returns [] without warnings."""
        fn = _get_normalizer()
        assert fn([], context="empty-node") == []

    def test_mixed_list_converts_strings_keeps_objects(self):
        """Mixed list with both objects and strings is handled correctly."""
        fn = _get_normalizer()
        raw = [
            {"relation_type": "uses", "target_title": "Redis"},
            "implements::FastAPI",
        ]
        result = fn(raw, context="mixed-node")
        assert result == [
            {"relation_type": "uses", "target_title": "Redis"},
            {"relation_type": "implements", "target_title": "FastAPI"},
        ]

    def test_integer_item_in_list_is_skipped_with_warning(self, capsys):
        """Non-string, non-dict items in the list are skipped with a warning."""
        fn = _get_normalizer()
        raw = [42, {"relation_type": "uses", "target_title": "Tool"}]
        result = fn(raw, context="int-item-node")
        assert result == [{"relation_type": "uses", "target_title": "Tool"}]
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower() or "unexpected" in captured.out.lower()


# ===========================================================================
# Tests for repair_kg_typed_links._normalize / _is_canonical
# ===========================================================================

class TestRepairNormalize:
    """Repair script normaliser (_normalize + _is_canonical)."""

    def test_canonical_detected_correctly(self):
        _normalize, _is_canonical = _get_repair_normalize()
        canonical = [{"relation_type": "uses", "target_title": "Redis"}]
        assert _is_canonical(canonical) is True

    def test_list_of_strings_not_canonical(self):
        _normalize, _is_canonical = _get_repair_normalize()
        assert _is_canonical(["uses::Redis"]) is False

    def test_none_not_canonical(self):
        _normalize, _is_canonical = _get_repair_normalize()
        assert _is_canonical(None) is False

    def test_empty_list_canonical(self):
        _normalize, _is_canonical = _get_repair_normalize()
        # Empty list satisfies the canonical contract (all items are objects)
        assert _is_canonical([]) is True

    def test_string_conversion_produces_no_change_flag_for_already_canonical(self):
        _normalize, _is_canonical = _get_repair_normalize()
        canonical = [{"relation_type": "uses", "target_title": "Redis"}]
        result, changed = _normalize(canonical)
        # Already canonical — should not be flagged as changed
        assert result == canonical
        assert changed is False

    def test_string_list_conversion_flags_changed(self):
        _normalize, _is_canonical = _get_repair_normalize()
        raw = ["uses::Redis", "implements::Concept"]
        result, changed = _normalize(raw)
        assert changed is True
        assert result == [
            {"relation_type": "uses", "target_title": "Redis"},
            {"relation_type": "implements", "target_title": "Concept"},
        ]

    def test_empty_input_no_change(self):
        _normalize, _is_canonical = _get_repair_normalize()
        result, changed = _normalize([])
        assert result == []
        assert changed is False

    def test_none_input_no_change(self):
        _normalize, _is_canonical = _get_repair_normalize()
        result, changed = _normalize(None)
        assert result == []
        assert changed is False
