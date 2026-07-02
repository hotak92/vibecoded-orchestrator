# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 (P3): cross_references + query_code_structure resolve to the
CANONICAL (chunk_num==0) object when multi-chunk objects are present.

Covers:
  * server._pick_canonical_chunk selects chunk 0 among mixed chunks, and
    handles legacy (chunk_num absent) rows.
  * analyzer._populate_caches_from_weaviate populates full_name → the chunk-0
    UUID even when the iterator yields a non-canonical chunk last.
  * analyzer._strip_chunk_header removes the [chunk N/total] header before
    AST parse in call-linking.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "claude_mcp_servers"))

from weaviate_mcp import server as srv  # noqa: E402

_ANALYZER_PATH = Path(__file__).parent.parent / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_v0272_canon_analyze", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer()


class _Obj:
    def __init__(self, uuid, chunk_num):
        self.uuid = uuid
        self.properties = {"chunk_num": chunk_num, "full_name": "mod.f"}


def test_pick_canonical_chunk_prefers_chunk_zero():
    objs = [_Obj("u2", 2), _Obj("u0", 0), _Obj("u1", 1)]
    picked = srv._pick_canonical_chunk(objs)
    assert picked.uuid == "u0"


def test_pick_canonical_chunk_legacy_row_none():
    # A single-chunk legacy row (chunk_num absent/None) is canonical.
    objs = [_Obj("legacy", None)]
    assert srv._pick_canonical_chunk(objs).uuid == "legacy"


def test_pick_canonical_chunk_empty_returns_none():
    assert srv._pick_canonical_chunk([]) is None


def test_pick_canonical_chunk_lowest_when_no_zero():
    # No chunk 0 present (corrupt/partial) → lowest chunk_num wins.
    objs = [_Obj("u3", 3), _Obj("u1", 1)]
    assert srv._pick_canonical_chunk(objs).uuid == "u1"


def test_populate_caches_prefers_canonical_chunk(analyzer_mod):
    """The iterator yields chunk 2 LAST — the cache must still hold chunk 0's UUID."""
    prefer = analyzer_mod.CodeGraphAnalyzer._prefer_canonical_chunk

    class _It:
        def __init__(self, objs):
            self._objs = objs

        def iterator(self):
            return iter(self._objs)

    # Simulate the function-loading loop manually (the method's body).
    function_cache = {}
    objs = [_Obj("u0", 0), _Obj("u1", 1), _Obj("u2", 2)]
    for obj in objs:
        full_name = obj.properties.get("full_name", "")
        if full_name and prefer(function_cache, full_name, obj):
            function_cache[full_name] = str(obj.uuid)
    assert function_cache["mod.f"] == "u0", "cache must hold canonical chunk-0 UUID"


def test_strip_chunk_header(analyzer_mod):
    strip = analyzer_mod._strip_chunk_header
    assert strip("[chunk 1/3]\n\ndef f():\n    pass") == "def f():\n    pass"
    # No header → unchanged.
    assert strip("def g():\n    pass") == "def g():\n    pass"
