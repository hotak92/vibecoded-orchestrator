# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.72 (P3+P4): store fan-out + generalized collapse/tier helpers.

P3 store:
  * a multi-chunk function emits N Weaviate objects with distinct UUIDs
    (chunk_num mixed into the key), each carrying chunk_num/total_chunks.
  * `_maybe_chunk_and_write` returns the CANONICAL (chunk 0) UUID.
  * `_prefer_canonical_chunk` selects chunk 0 for the caches.

P4 collapse (BOTH branches):
  * KG default keyed (file_path,title) — UNCHANGED (parity).
  * code keyed (file_path,full_name) — N chunks → 1 entry, chunks_matched==N.

P4 tier (BOTH branches):
  * KG default gate min=0.42 — UNCHANGED (parity).
  * code gate uses code thresholds (min=0.22).
  * single-chunk "full" costs 1 not 7.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "claude_mcp_servers"))

from weaviate_mcp import server as srv  # noqa: E402

_THIS_DIR = Path(__file__).parent
_ANALYZER_PATH = _THIS_DIR.parent / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_v0272_analyze_code_graph", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer()


# ────────────────────────── P3: store fan-out ──────────────────────────


class _FakeData:
    def __init__(self):
        self.replaced = []
        self.inserted = []

    def replace(self, uuid, **kwargs):
        # Simulate cold-start: no object exists → raise the "not found" signal
        # so `_write_one_object` falls through to insert().
        raise RuntimeError(f"no object with id '{uuid}'")

    def insert(self, uuid, **kwargs):
        self.inserted.append({"uuid": uuid, **kwargs})


class _FakeQuery:
    def fetch_object_by_id(self, uuid, return_properties=None):
        return None  # absent → tombstone-skip falls through to write


class _FakeColl:
    def __init__(self, name):
        self.name = name
        self.data = _FakeData()
        self.query = _FakeQuery()


class _StubAnalyzer:
    """Minimal analyzer carrying the attrs the store path reads, plus the real
    chunk/store methods borrowed from CodeGraphAnalyzer."""

    def __init__(self, analyzer_mod):
        self.project_name = "P"
        self._track_visited = True
        self._current_language = "python"
        self._current_source = ""
        self.visited_uuids = set()
        cls = analyzer_mod.CodeGraphAnalyzer
        for meth in (
            "_dedup_insert", "_maybe_chunk_and_write",
            "_stamp_single_chunk_props", "_write_one_object",
            "_prefer_canonical_chunk",
        ):
            setattr(self, meth, getattr(cls, meth).__get__(self, _StubAnalyzer))


def _big_body(n=600):
    return "def big():\n" + "\n".join(
        f"    r_{i} = compute(input_{i}) + offset_{i} * scale_{i}" for i in range(n)
    ) + "\n    return r_0\n"


def test_multichunk_function_emits_n_objects_distinct_uuids(analyzer_mod, monkeypatch):
    # Deterministic embedding so we don't need a live backend.
    monkeypatch.setattr(analyzer_mod, "generate_embedding", lambda text: [0.1, 0.2, 0.3])
    monkeypatch.setattr(analyzer_mod, "_resolve_code_model_id", lambda: "codesage/codesage-large-v2")

    stub = _StubAnalyzer(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    params = {
        "properties": {
            "name": "big", "full_name": "mod.big",
            "function_body": _big_body(), "signature": "def big()",
            "type_uses": [], "cfg_summary": "", "data_flow_vars": [],
            "language": "python",
        },
        "vector": [0.9, 0.9, 0.9],
    }
    canonical = stub._dedup_insert(coll, params, "mod.big", file_path_rel="src/mod.py")

    inserted = coll.data.inserted
    assert len(inserted) >= 2, "over-budget function must fan out into N objects"
    uuids = [o["uuid"] for o in inserted]
    assert len(set(uuids)) == len(uuids), "each chunk must have a distinct UUID"

    # chunk_num / total_chunks stamped; total consistent across chunks.
    chunk_nums = sorted(o["properties"]["chunk_num"] for o in inserted)
    assert chunk_nums == list(range(len(inserted))), "0-indexed contiguous chunk_num"
    totals = {o["properties"]["total_chunks"] for o in inserted}
    assert totals == {len(inserted)}

    # canonical returned is the chunk-0 UUID.
    chunk0_uuid = next(o["uuid"] for o in inserted if o["properties"]["chunk_num"] == 0)
    assert canonical == chunk0_uuid


def test_in_budget_function_single_object_chunk_zero(analyzer_mod, monkeypatch):
    monkeypatch.setattr(analyzer_mod, "generate_embedding", lambda text: [0.1, 0.2, 0.3])
    monkeypatch.setattr(analyzer_mod, "_resolve_code_model_id", lambda: "codesage/codesage-large-v2")

    stub = _StubAnalyzer(analyzer_mod)
    coll = _FakeColl("P_CodeFunction")
    params = {
        "properties": {
            "name": "small", "full_name": "mod.small",
            "function_body": "def small():\n    return 1\n", "signature": "def small()",
            "type_uses": [], "cfg_summary": "", "data_flow_vars": [],
            "language": "python",
        },
        "vector": [0.9, 0.9, 0.9],
    }
    stub._dedup_insert(coll, params, "mod.small", file_path_rel="src/mod.py")
    assert len(coll.data.inserted) == 1, "in-budget entity → single object"
    props = coll.data.inserted[0]["properties"]
    assert props["chunk_num"] == 0 and props["total_chunks"] == 1
    # FULL body preserved (no chunk header, no truncation loss).
    assert props["function_body"] == "def small():\n    return 1\n"


def test_prefer_canonical_chunk(analyzer_mod):
    prefer = analyzer_mod.CodeGraphAnalyzer._prefer_canonical_chunk

    class _Obj:
        def __init__(self, cn):
            self.properties = {"chunk_num": cn}

    cache = {}
    # First object (a non-canonical chunk) takes empty slot.
    assert prefer(cache, "mod.f", _Obj(3)) is True
    cache["mod.f"] = "u3"
    # chunk 0 overrides.
    assert prefer(cache, "mod.f", _Obj(0)) is True
    # a non-zero chunk does NOT override an incumbent.
    cache["mod.f"] = "u0"
    assert prefer(cache, "mod.f", _Obj(5)) is False
    # legacy row (chunk_num None) counts as canonical.
    assert prefer({}, "mod.g", _Obj(None)) is True


# ────────────────── P4: collapse — BOTH branches ────────────────────────


def test_collapse_kg_default_unchanged_parity():
    """KG default keys (file_path, title); two chunks of a node collapse to 1."""
    inp = [
        {"title": "A", "file_path": "k/a.md", "combined_score": 0.9, "chunk_number": 3, "content": "c3"},
        {"title": "A", "file_path": "k/a.md", "combined_score": 0.8, "chunk_number": 7, "content": "c7"},
        {"title": "B", "file_path": "k/b.md", "combined_score": 0.6, "chunk_number": None},
    ]
    out = srv._collapse_to_one_per_node(inp)  # NO new kwargs → KG behaviour
    assert len(out) == 2
    a = next(r for r in out if r["title"] == "A")
    assert a["chunks_matched"] == 2
    assert a["best_chunk_number"] == 3
    assert a["content"] == "c3"


def test_collapse_code_branch_keyed_full_name():
    """Code branch keys (file_path, full_name); N chunks → 1 entry."""
    inp = [
        {"full_name": "mod.f", "file_path": "src/mod.py", "score": 0.5, "chunk_num": 0, "function_body": "b0"},
        {"full_name": "mod.f", "file_path": "src/mod.py", "score": 0.8, "chunk_num": 2, "function_body": "b2"},
        {"full_name": "mod.f", "file_path": "src/mod.py", "score": 0.6, "chunk_num": 1, "function_body": "b1"},
        {"full_name": "mod.g", "file_path": "src/mod.py", "score": 0.4, "chunk_num": 0, "function_body": "g0"},
    ]
    out = srv._collapse_to_one_per_node(
        inp, score_field="score",
        key_fields=("file_path", "full_name"),
        chunk_field="chunk_num",
        dedup_kind="code",
    )
    assert len(out) == 2
    f = next(r for r in out if r["full_name"] == "mod.f")
    assert f["chunks_matched"] == 3
    assert f["best_chunk_number"] == 2, "highest-scoring chunk (0.8) wins"
    assert f["function_body"] == "b2"


# ────────────────── P4: tier — BOTH branches ────────────────────────────


def test_tier_kg_default_gate_042_unchanged():
    """KG gate min=0.42 (parity)."""
    assert srv._get_result_verbosity_by_score(0.41) == "discard"
    assert srv._get_result_verbosity_by_score(0.42) == "summary"
    assert srv._get_result_verbosity_by_score(0.55) == "single_chunk"
    assert srv._get_result_verbosity_by_score(0.65) == "three_chunks"
    assert srv._get_result_verbosity_by_score(0.75) == "full"


def test_tier_code_gate_uses_code_thresholds():
    """Code gate min=0.22 — a 0.30 code match is NOT discarded (v0.2.70 Bug B)."""
    t = srv._CODE_TIER_THRESHOLDS
    assert srv._get_result_verbosity_by_score(0.21, t) == "discard"
    assert srv._get_result_verbosity_by_score(0.30, t) == "summary"
    assert srv._get_result_verbosity_by_score(0.45, t) == "single_chunk"
    assert srv._get_result_verbosity_by_score(0.60, t) == "three_chunks"
    assert srv._get_result_verbosity_by_score(0.75, t) == "full"
    # And the KG gate WOULD have discarded that 0.30 code match:
    assert srv._get_result_verbosity_by_score(0.30) == "discard"


def test_single_chunk_full_costs_one_not_seven():
    """A 'full' tier on a 1-chunk node costs 1 chunk, not 7 — for both gates."""
    tier, cost = srv._allocate_tier_within_budget(0.95, total_chunks=1, remaining_budget=20)
    assert tier == "full" and cost == 1
    # code gate: same budgeting math, code thresholds.
    tier_c, cost_c = srv._allocate_tier_within_budget(
        0.95, total_chunks=1, remaining_budget=20, thresholds=srv._CODE_TIER_THRESHOLDS,
    )
    assert tier_c == "full" and cost_c == 1


def test_code_tier_render_summary_uses_signature_plus_first_chunk():
    """Code has no sidecar → summary = signature + first chunk body (guarded)."""
    props = {
        "full_name": "mod.f", "signature": "def f(x)",
        "function_body": "[chunk 1/3]\n\ndef f(x):\n    return x", "file_path": "src/mod.py",
        "chunk_num": 0, "total_chunks": 3,
    }
    out = srv._format_code_result_by_tier(props, "CodeFunction", "summary", score=0.3)
    assert out["tier"] == "summary"
    assert out["signature"] == "def f(x)"
    # header stripped in the summary body
    assert "[chunk" not in out["summary"]
    assert "def f(x)" in out["summary"]


def test_code_tier_render_full_assembles_via_fetcher():
    props = {
        "full_name": "mod.f", "signature": "def f()",
        "function_body": "[chunk 2/3]\n\nmiddle", "file_path": "src/mod.py",
        "chunk_num": 1, "total_chunks": 3,
    }

    def fetcher(full_name, hit, total, max_chunks):
        assert full_name == "mod.f"
        assert max_chunks == 7  # full-tier window
        return [
            {"function_body": "[chunk 1/3]\n\nfirst"},
            {"function_body": "[chunk 2/3]\n\nmiddle"},
            {"function_body": "[chunk 3/3]\n\nlast"},
        ]

    out = srv._format_code_result_by_tier(props, "CodeFunction", "full", score=0.9, chunk_fetcher=fetcher)
    assert out["chunks_shown"] == 3
    assert "first" in out["function_body"] and "last" in out["function_body"]
    assert "[chunk" not in out["function_body"], "headers stripped in assembled body"
