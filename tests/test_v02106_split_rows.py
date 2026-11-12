# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.106 split ROWS (owner ruling 2026-11-11, "extra rows in both places,
so we know which chunk actually matched in longer documents, and only
retrieve it and the nearby ones").

A chunk ANY model of a row's vector set refuses is stored as several
consecutive rows — on the KG / document side AND in the code graph — each
embedded whole by every model; no stored vector is a length-weighted average
of pieces any more. This file pins:

* the code-graph row model (``vco_lib.codegraph_guards``): ordered piece rows
  with deterministic ids (``chunk_key``) and per-row content hashes, in-band
  plan boundaries (a continuation piece carries no ``[chunk i/n]`` header),
  the exact re-join, the unchanged-split-entity recognition, the revision-2
  heal veto;
* the analyzer wiring (the REAL ``_dedup_insert`` against a stateful fake
  collection): a refused entity becomes rows, a second walk embeds and writes
  nothing, a changed entity re-embeds only its rows, the one-time revision-2
  heal stamps rows that provably fit and re-embeds the others;
* retrieval: one result per entity naming the matched row, neighbours glued
  back exactly, the KG window centred on the true neighbours;
* the other row writers (``process_documents``, the RL citation answer
  vectors).
"""
from __future__ import annotations

import importlib.util
import math
import sys
import types
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(REPO_ROOT), str(REPO_ROOT / "claude_mcp_servers")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from vco_lib import codegraph_guards as G  # noqa: E402
from vco_lib import embed_split as ES  # noqa: E402

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"


def _vec(text: str) -> List[float]:
    """A deterministic per-text unit vector (distinct texts → distinct)."""
    h = sum((i + 1) * ord(c) for i, c in enumerate(text[:400])) + len(text)
    raw = [float((h >> s) & 0xFF) + 1.0 for s in (0, 8, 16, 24)]
    n = math.sqrt(sum(x * x for x in raw))
    return [x / n for x in raw]


# ═══════════════════════════════════════════════════════════════════════════
# Pure helpers
# ═══════════════════════════════════════════════════════════════════════════


def test_chunk_key_is_the_one_key_space():
    assert G.chunk_key("mod.f", 0) == "mod.f"
    assert G.chunk_key("mod.f", 3) == "mod.f::3"
    uuids, _h = G.chunk_identities(
        ["a", "b", "c"], {}, True, "mod.f", 3, uuid_fn=lambda k: k, hash_fn=lambda p: "h",
    )
    assert uuids == ["mod.f", "mod.f::1", "mod.f::2"]


def test_join_glues_continuation_pieces_exactly():
    # plan chunk 1 whole, plan chunk 2 split into two pieces (the second has
    # no header), plan chunk 3 whole.
    rows = [
        (0, "[chunk 1/3]\n\ndef f():\n    a = 1"),
        (1, "[chunk 2/3]\n\n    b = 'abcdefgh"),
        (2, "ijkl'\n    c = 3"),
        (3, "[chunk 3/3]\n\n    return a"),
    ]
    out = G.join_chunk_bodies(rows, "\n")
    assert out == "def f():\n    a = 1\n    b = 'abcdefghijkl'\n    c = 3\n    return a"


def test_join_single_plan_split_and_gaps():
    # A single-chunk plan split into rows: no header anywhere → every row
    # after the first is a continuation.
    assert G.join_chunk_bodies([(0, "abc"), (1, "def"), (2, "ghi")]) == "abcdefghi"
    # A gap in chunk_num (a partial window) is never glued.
    assert G.join_chunk_bodies([(0, "abc"), (2, "ghi")], "|") == "abc|ghi"
    # Legacy rows (every chunk headed) keep the separator.
    legacy = [(0, "[chunk 1/2]\n\nx"), (1, "[chunk 2/2]\n\ny")]
    assert G.join_chunk_bodies(legacy, "\n\n") == "x\n\ny"


class _FpColl:
    """Point-read fake: ``rows`` maps uuid → properties."""

    def __init__(self, rows: Dict[str, dict]):
        self.rows = rows
        self.body_reads = 0
        outer = self

        class _Q:
            def fetch_object_by_id(self, uuid, return_properties=None, include_vector=None):
                props = outer.rows.get(uuid)
                if props is None:
                    return None
                if return_properties == ["function_body"]:
                    outer.body_reads += 1
                return types.SimpleNamespace(
                    properties={k: props.get(k) for k in (return_properties or props)},
                    vector={},
                )

        self.query = _Q()


def _fp(coll):
    def read(uuid):
        props = coll.rows.get(uuid)
        return None if props is None else {"total_chunks": props.get("total_chunks")}
    return read


def test_stored_split_rows_recognises_an_unchanged_split_entity():
    plan = ["[chunk 1/2]\n\nabcdef", "[chunk 2/2]\n\nghij"]
    stored = ["[chunk 1/2]\n\nabcdef", "[chunk 2/2]\n\ngh", "ij"]
    coll = _FpColl({f"u{k}": {"function_body": b, "total_chunks": 3} for k, b in enumerate(stored)})
    rows = G.stored_split_rows(coll, plan, "function_body", uuid_fn=lambda k: f"u{k}", read_fp=_fp(coll))
    assert rows == stored


def test_stored_split_rows_refuses_a_changed_or_unsplit_entity():
    plan = ["[chunk 1/2]\n\nabcdef", "[chunk 2/2]\n\nghij"]
    # Not split (stored count == plan count): no body is even read.
    coll = _FpColl({f"u{k}": {"function_body": b, "total_chunks": 2} for k, b in enumerate(plan)})
    assert G.stored_split_rows(coll, plan, "function_body", uuid_fn=lambda k: f"u{k}", read_fp=_fp(coll)) is None
    assert coll.body_reads == 0
    # Split, but the plan changed underneath → None (the walk re-embeds).
    stored = ["[chunk 1/2]\n\nabcdeX", "[chunk 2/2]\n\ngh", "ij"]
    coll = _FpColl({f"u{k}": {"function_body": b, "total_chunks": 3} for k, b in enumerate(stored)})
    assert G.stored_split_rows(coll, plan, "function_body", uuid_fn=lambda k: f"u{k}", read_fp=_fp(coll)) is None
    # A missing row → None.
    del coll.rows["u2"]
    assert G.stored_split_rows(coll, plan, "function_body", uuid_fn=lambda k: f"u{k}", read_fp=_fp(coll)) is None


def test_may_have_been_refused_is_a_provable_byte_bound():
    codesage = "codesage/codesage-large-v2"  # 1 024-token window
    assert G.may_have_been_refused("x" * 900, [codesage]) is False
    assert G.may_have_been_refused("x" * 1_100, [codesage]) is True
    # A multi-byte text counts bytes, not characters.
    assert G.may_have_been_refused("é" * 600, [codesage]) is True
    # An unregistered model cannot be proven to accept anything.
    assert G.may_have_been_refused("x", ["some-unknown-model"]) is True
    # Every model of the vector set must accept it.
    assert G.may_have_been_refused("x" * 3_000, ["qwen3-embedding:0.6b"]) is False
    assert G.may_have_been_refused("x" * 3_000, ["qwen3-embedding:0.6b", codesage]) is True
    assert G.may_have_been_refused(None, [codesage]) is True


# ═══════════════════════════════════════════════════════════════════════════
# The fan-out and the dispatcher
# ═══════════════════════════════════════════════════════════════════════════


def _refusing(limit: int, log: Optional[list] = None):
    def embed(text: str):
        assert ES.row_split_active(), "code row embeds run inside the row scope"
        if len(text) > limit:
            raise ES.ChunkOverflowError("m", len(text), "exceeds the context length")
        if log is not None:
            log.append(text)
        return {"slot": _vec(text)}
    return embed


def test_fan_out_writes_ordered_piece_rows_with_whole_vectors():
    plan = ["[chunk 1/2]\n\n" + "a " * 40, "[chunk 2/2]\n\n" + "b " * 1_200]
    written: List[Tuple[str, dict]] = []
    canonical = G.fan_out_chunk_writes(
        {"properties": {}}, {"full_name": "m.f"}, plan, True,
        embed_fn=_refusing(800),
        write_fn=lambda u, p: written.append((u, p)) or u,
        uuid_fn=lambda k: f"uuid-{k}",
    )
    assert canonical == "uuid-0"
    bodies = [p["properties"]["function_body"] for _u, p in written]
    assert len(bodies) > 2 and bodies[0] == plan[0]
    assert "".join(bodies[1:]) == plan[1], "the refused chunk's pieces concatenate exactly"
    assert [u for u, _p in written] == [f"uuid-{k}" for k in range(len(bodies))]
    for k, (_u, p) in enumerate(written):
        assert p["properties"]["chunk_num"] == k
        assert p["properties"]["total_chunks"] == len(bodies)
        assert p["vector"] == {"slot": _vec(bodies[k])}, "every row's vector is whole"


def test_fan_out_embeds_everything_before_writing():
    written: list = []
    with pytest.raises(ES.UnsplittableTextError):
        G.fan_out_chunk_writes(
            {"properties": {}}, {}, ["fits", "x" * 3_000], True,
            embed_fn=_refusing(100),  # the floor (512) is above the limit
            write_fn=lambda u, p: written.append(u) or u,
            uuid_fn=str,
        )
    assert written == [], "a failed embed leaves the stored rows untouched"


def test_dispatch_reports_a_refused_single_text():
    params = {"_deferred_embed": lambda: _refusing(10)("x" * 50)}
    refused = G.dispatch_deferred_embed(
        params, is_multi_chunk=False, resolver=None,
        eager_shape_fn=lambda v: v, split_scope=True,
    )
    assert refused is True and "vector" not in params and "_deferred_embed" not in params
    params = {"_deferred_embed": lambda: _refusing(10_000)("x" * 50)}
    assert G.dispatch_deferred_embed(
        params, is_multi_chunk=False, resolver=None,
        eager_shape_fn=lambda v: v, split_scope=True,
    ) is False
    assert params["vector"] == {"slot": _vec("x" * 50)}


# ═══════════════════════════════════════════════════════════════════════════
# The analyzer, through the REAL _dedup_insert
# ═══════════════════════════════════════════════════════════════════════════


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_v02106_splitrows_analyzer", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def am() -> types.ModuleType:
    return _load_analyzer()


class _Obj:
    def __init__(self, uuid, properties, vector=None):
        self.uuid = uuid
        self.properties = properties
        self.vector = vector


class _MemColl:
    """A stateful in-memory class: writes are visible to the next walk."""

    def __init__(self, name: str = "P_CodeFunction"):
        self.name = name
        self.rows: Dict[str, Tuple[dict, object]] = {}
        self.writes: List[str] = []
        self.updates: List[str] = []
        self.deletes: List[str] = []
        outer = self

        class _Data:
            def replace(self, uuid, properties=None, vector=None, references=None, **kw):
                if uuid not in outer.rows:
                    raise RuntimeError(f"no object with id '{uuid}'")
                outer.rows[uuid] = (dict(properties or {}), vector)
                outer.writes.append(uuid)

            def insert(self, uuid=None, properties=None, vector=None, references=None, **kw):
                outer.rows[uuid] = (dict(properties or {}), vector)
                outer.writes.append(uuid)

            def update(self, uuid=None, properties=None, vector=None, **kw):
                outer.rows[uuid][0].update(properties or {})
                outer.updates.append(uuid)

            def delete_by_id(self, uuid):
                outer.rows.pop(str(uuid), None)
                outer.deletes.append(str(uuid))

        class _Query:
            def fetch_object_by_id(self, uuid, return_properties=None, include_vector=None):
                row = outer.rows.get(uuid)
                if row is None:
                    return None
                props, vec = row
                if return_properties:
                    props = {k: props.get(k) for k in return_properties}
                return _Obj(uuid, dict(props), vec)

            def fetch_objects(self, *a, **k):
                return types.SimpleNamespace(
                    objects=[_Obj(u, dict(p), v) for u, (p, v) in outer.rows.items()])

        self.data = _Data()
        self.query = _Query()
        _names = ["full_name", "file_path", "project", "project_source", "chunk_num",
                  "total_chunks", "function_body", "content_hash", "embed_revision"]
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            properties=[types.SimpleNamespace(name=n) for n in _names], vector_config={}))

    def iterator(self, *a, **k):
        return iter([_Obj(u, dict(p), v) for u, (p, v) in list(self.rows.items())])

    def body_rows(self) -> List[Tuple[int, str]]:
        return sorted((p["chunk_num"], p["function_body"]) for p, _v in self.rows.values())


def _analyzer(am):
    inst = am.CodeGraphAnalyzer.__new__(am.CodeGraphAnalyzer)
    inst.project_name = "P"
    inst._track_visited = True
    inst._current_language = "python"
    inst._current_source = ""
    inst.visited_uuids = set()
    inst.module_cache, inst.class_cache, inst.function_cache = {}, {}, {}
    inst.module_imports = {}
    inst._embed_skip_fingerprint = None
    return inst


LIMIT = 1_000  # the fake code model's window, in characters


class _Walk:
    """Drive one entity through ``_dedup_insert`` the way a walker does."""

    def __init__(self, am, monkeypatch, plan: List[str]):
        self.am = am
        self.embedded: List[str] = []
        self.plan = plan
        monkeypatch.setattr(am, "chunk_or_truncate_for_embedding",
                            lambda s, b, **kw: list(self.plan))

        def _gen(text):
            if not ES.row_split_active():
                raise AssertionError("a Function embed ran outside the row scope")
            if len(text) > LIMIT:
                raise ES.ChunkOverflowError("codesage", len(text), "exceeds the context length")
            self.embedded.append(text)
            return {"codesage_embed": _vec(text)}

        monkeypatch.setattr(am, "generate_embedding", _gen)

    def run(self, coll, body: str = "def big(): ..."):
        inst = _analyzer(self.am)
        props = {"name": "big", "full_name": "mod.big", "function_body": body,
                 "signature": "def big()", "type_uses": [], "cfg_summary": "",
                 "data_flow_vars": [], "project": "P"}
        params = {"properties": props,
                  "_deferred_embed": lambda: self.am.generate_embedding(self.plan[0])}
        return inst._dedup_insert(coll, params, "mod.big", file_path_rel="src/mod.py"), inst


def _single_text(n: int = 3_000) -> str:
    return "def big():\n" + "\n".join(
        f"    value_{i} = compute(item_{i})  # step {i}" for i in range(n // 40))


def test_a_refused_single_entity_becomes_ordered_rows(am, monkeypatch):
    text = _single_text()
    walk = _Walk(am, monkeypatch, [text])
    coll = _MemColl()
    canonical, _inst = walk.run(coll)
    rows = coll.body_rows()
    assert len(rows) > 1, "a refused entity is stored as several rows"
    assert [n for n, _b in rows] == list(range(len(rows)))
    assert "".join(b for _n, b in rows) == text, "every character, in text order"
    assert all(len(b) <= LIMIT for _n, b in rows)
    det0 = am._deterministic_uuid("P", "src/mod.py", "mod.big", project_source="")
    assert canonical == det0, "callers capture the canonical row (the bare key)"
    for uid, (props, vec) in coll.rows.items():
        k = props["chunk_num"]
        assert uid == am._deterministic_uuid("P", "src/mod.py", G.chunk_key("mod.big", k), project_source="")
        assert props["total_chunks"] == len(rows)
        assert props["full_name"] == "mod.big" and props["signature"] == "def big()"
        assert vec == {"codesage_embed": _vec(props["function_body"])}, "no averaged vector"
        assert props["embed_revision"] == am.CODEGRAPH_EMBED_REVISION
        assert props["content_hash"]


def test_a_second_walk_embeds_and_writes_nothing(am, monkeypatch):
    walk = _Walk(am, monkeypatch, [_single_text()])
    coll = _MemColl()
    walk.run(coll)
    walk.embedded.clear()
    writes = len(coll.writes)
    _canonical, inst = walk.run(coll)
    assert walk.embedded == [], "an unchanged split entity re-embeds nothing"
    assert len(coll.writes) == writes and coll.updates == [] and coll.deletes == []
    visited = {u for _c, u in inst.visited_uuids}
    assert visited == set(coll.rows), "every row is marked visited (prune/reconcile keep them)"


def test_a_multi_chunk_plan_splits_only_the_refused_chunk(am, monkeypatch):
    plan = ["[chunk 1/3]\n\ndef big():\n    a = 1",
            "[chunk 2/3]\n\n" + _single_text(2_400),
            "[chunk 3/3]\n\n    return a"]
    walk = _Walk(am, monkeypatch, plan)
    coll = _MemColl()
    walk.run(coll)
    rows = coll.body_rows()
    assert rows[0][1] == plan[0] and rows[-1][1] == plan[2]
    middle = [b for _n, b in rows[1:-1]]
    assert len(middle) > 1 and "".join(middle) == plan[1]
    assert G.join_chunk_bodies(rows, "\n").startswith("def big():\n    a = 1\n")
    walk.embedded.clear()
    walk.run(coll)
    assert walk.embedded == [], "second walk: nothing"


def test_a_changed_entity_re_embeds_its_rows_and_drops_the_tail(am, monkeypatch):
    walk = _Walk(am, monkeypatch, [_single_text(3_000)])
    coll = _MemColl()
    walk.run(coll)
    before = len(coll.rows)
    walk.plan = ["def big():\n    return 1"]  # now fits: one row
    walk.embedded.clear()
    walk.run(coll, body="def big():\n    return 1")
    assert walk.embedded == ["def big():\n    return 1"]
    assert len(coll.rows) == 1 and before > 1, "the stale piece rows are deleted"


def _seed_rev1(am, coll, text: str):
    """A row the pre-split-rows analyzer wrote: content-current, revision 1."""
    props = {"name": "big", "full_name": "mod.big", "function_body": "def big(): ...",
             "signature": "def big()", "type_uses": [], "cfg_summary": "",
             "data_flow_vars": [], "chunk_num": 0, "total_chunks": 1,
             "language": "python", "file_path": "src/mod.py", "is_test": False}
    props["content_hash"] = am._content_hash_for_object("P_CodeFunction", props)
    props["embed_revision"] = 1
    det = am._deterministic_uuid("P", "src/mod.py", "mod.big", project_source="")
    coll.rows[det] = (props, {"codesage_embed": [0.5, 0.5, 0.5, 0.5]})
    return det


def test_the_resync_revision_fallback_matches_the_analyzer(am):
    """``codegraph_resync._FALLBACK_EMBED_REVISION`` "MUST MATCH" the analyzer
    constant, and the regex the resync parses it with must still read it — a
    mismatch makes every converged row count as owed (or the reverse)."""
    from vco_lib import codegraph_resync as cr

    assert cr._resolve_embed_revision(_ANALYZER_PATH) == am.CODEGRAPH_EMBED_REVISION == 2
    assert cr._FALLBACK_EMBED_REVISION == am.CODEGRAPH_EMBED_REVISION


def test_rev2_heal_stamps_a_row_that_provably_fit(am, monkeypatch):
    assert am.CODEGRAPH_EMBED_REVISION == 2
    small = "def big():\n    return 1"
    walk = _Walk(am, monkeypatch, [small])
    coll = _MemColl()
    det = _seed_rev1(am, coll, small)
    walk.run(coll)
    assert walk.embedded == [], "a row that fit every model keeps its vector"
    assert coll.updates == [det] and coll.rows[det][0]["embed_revision"] == 2


def test_rev2_heal_re_embeds_a_row_that_may_have_been_refused(am, monkeypatch):
    big = _single_text(3_000)  # over CodeSage's 1 024-token window in bytes
    walk = _Walk(am, monkeypatch, [big])
    coll = _MemColl()
    _seed_rev1(am, coll, big)
    walk.run(coll)
    assert walk.embedded, "a possibly cut/averaged row is re-embedded once"
    assert len(coll.rows) > 1, "…and the refused text is now stored as rows"
    walk.embedded.clear()
    walk.run(coll)
    assert walk.embedded == [], "once"


# ═══════════════════════════════════════════════════════════════════════════
# Retrieval
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def srv():
    import weaviate_mcp.server as srv  # noqa: PLC0415

    return srv


def _rows_props(bodies: List[str]) -> List[dict]:
    return [{"full_name": "mod.big", "file_path": "src/mod.py", "signature": "def big()",
             "function_body": b, "chunk_num": k, "total_chunks": len(bodies)}
            for k, b in enumerate(bodies)]


def test_tier_render_names_the_matched_row_and_glues_neighbours(srv):
    text = "def big():\n    x = 'abcdefghij" + "klmnop'\n    return x\n"
    bodies = [text[:20], text[20:35], text[35:]]  # one entity, a single plan split in 3
    rows = _rows_props(bodies)

    def _fetch(full_name, hit, total, window, file_path):
        assert (full_name, file_path) == ("mod.big", "src/mod.py")
        picked = sorted(rows, key=lambda p: abs(p["chunk_num"] - hit))[:window]
        return sorted(picked, key=lambda p: p["chunk_num"])

    out = srv._format_code_result_by_tier(rows[1], "CodeFunction", "three_chunks",
                                          score=0.55, chunk_fetcher=_fetch)
    assert out["matched_chunk"] == 2 and out["chunks_total"] == 3
    assert out["function_body"] == text, "split pieces glue back exactly"
    single = srv._format_code_result_by_tier(rows[1], "CodeFunction", "single_chunk",
                                             score=0.4, chunk_fetcher=_fetch)
    assert single["function_body"] == bodies[1] and single["matched_chunk"] == 2


def test_tier_thresholds_are_the_calibrated_code_gate(srv):
    t = srv._CODE_TIER_THRESHOLDS
    assert srv._get_result_verbosity_by_score(0.40, t) == "single_chunk"
    assert srv._get_result_verbosity_by_score(0.55, t) == "three_chunks"
    assert srv._get_result_verbosity_by_score(0.70, t) == "full"


def test_collapse_reports_the_entity_once_with_the_matched_row(srv):
    rows = _rows_props(["a" * 30, "b" * 30, "c" * 30])
    candidates = [
        {"_c": "CodeFunction", "_s": s, "_d": 1 - s, "_p": p, "_rerank": s}
        for s, p in zip((0.31, 0.58, 0.29), rows)
    ]
    out = srv.make_code_collapse_fn()(candidates)
    assert len(out) == 1, "one result per entity"
    assert out[0]["_p"]["chunk_num"] == 1, "the row that actually matched"


def test_kg_window_is_centred_on_the_true_neighbours():
    from weaviate_mcp import kg_query_core as core

    class _Coll:
        pass

    stored = [(k, f"piece-{k}") for k in range(1, 10)]

    def _fake_rows(coll, flt, pred, max_matches=None):
        return [types.SimpleNamespace(properties={"chunk_num": k, "content": c})
                for k, c in stored]

    import vco_lib.weaviate_exact_match as wem
    orig = wem.fetch_matching_rows
    wem.fetch_matching_rows = _fake_rows
    try:
        window = core._fetch_node_chunks(_Coll(), "T", 5, 9, 3, file_path="k/t.md")
    finally:
        wem.fetch_matching_rows = orig
    assert [k for k, _c in window] == [4, 5, 6]


def test_kg_tier_render_names_the_matched_row(monkeypatch):
    """The KG tier renderer reports which row matched (1-based, text order)
    beside the window it shows — the split pieces of a refused chunk are
    numbered in place, so the window is the hit's true neighbours."""
    from weaviate_mcp import kg_query_core as core

    rows = {k: f"piece-{k}" for k in range(1, 10)}
    monkeypatch.setattr(core, "_fetch_node_chunks", lambda coll, title, hit, total, window, file_path="": [
        (k, rows[k]) for k in range(max(1, hit - window // 2), max(1, hit - window // 2) + window)
    ])
    result = {"title": "T", "file_path": "knowledge/t.md", "node_type": "concept",
              "content": "piece-5", "chunk_number": 5, "total_chunks": 9}
    out = core._format_result_by_tier(result, "three_chunks", sidecar_db={}, coll=object())
    assert out["matched_chunk"] == 5 and out["chunks_shown"] == 3 and out["chunks_total"] == 9
    assert out["content"].endswith("piece-4\npiece-5\npiece-6")


# ═══════════════════════════════════════════════════════════════════════════
# Other row writers
# ═══════════════════════════════════════════════════════════════════════════


def test_mcp_backfill_fills_whole_rows_only(srv, monkeypatch):
    """The MCP ``backfill_embeddings`` tool fills one slot of EXISTING rows:
    it hands each row's whole content in a row-split scope (it used to cut it
    to 8 000 characters) and a row the model refuses gets NO vector — an
    error for that object, never a cut or averaged vector."""
    import asyncio
    import json as _json

    rows = {"a": "short row", "b": "z" * 9_000, "c": "another short row"}
    seen: list = []

    async def _embed(text):
        seen.append(text)
        if len(text) > 5_000:
            assert ES.row_split_active()
            raise ES.ChunkOverflowError("qwen3", len(text), "exceeds the context length")
        return _vec(text)

    updates: dict = {}

    class _Coll:
        def iterator(self, include_vector=False):
            return iter([types.SimpleNamespace(uuid=u, vector={}, properties={"content": c})
                         for u, c in rows.items()])

        data = types.SimpleNamespace(update=lambda uuid, vector: updates.__setitem__(uuid, vector))

    client = types.SimpleNamespace(collections=types.SimpleNamespace(
        exists=lambda name: True, get=lambda name: _Coll()))
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", True)
    monkeypatch.setattr(srv, "get_ollama_embedding", _embed)
    monkeypatch.setattr(srv, "get_weaviate_client", lambda: client)
    tool = getattr(srv.backfill_embeddings, "fn", srv.backfill_embeddings)
    out = _json.loads(asyncio.run(tool("Acme_KnowledgeGraph", provider="qwen3")))
    assert seen == list(rows.values()), "whole content, never the first 8 000 chars"
    assert set(updates) == {"a", "c"} and out["errors"] == 1
    assert updates["a"] == {"qwen3_embed": _vec("short row")}


def test_citation_answer_chunk_refused_yields_one_vector_per_piece():
    # The CANONICAL module path (every other test and production import it so;
    # `rl_client.…` would load a second copy of the module).
    from claude_mcp_servers.rl_client.citation_compute import _embed_answer_pieces

    def embed(text):
        if len(text) > 600:
            raise RuntimeError("Ollama HTTP 400: the input length exceeds the context length")
        return _vec(text)

    text = "word " * 400
    pieces = _embed_answer_pieces(text, embed, "m")
    assert len(pieces) > 1 and "".join(p for p, _v in pieces) == text
    assert all(v == _vec(p) for p, v in pieces), "no averaged answer vector"
    assert _embed_answer_pieces("short", embed, "m") == [("short", _vec("short"))]


def test_process_documents_stores_a_refused_chunk_as_renumbered_rows(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_v02106_process_documents", str(REPO_ROOT / "templates" / "scripts" / "process_documents.py"))
    assert spec is not None and spec.loader is not None
    pd = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(REPO_ROOT / "templates" / "scripts"))
    try:
        spec.loader.exec_module(pd)  # type: ignore[union-attr]
    finally:
        sys.path.remove(str(REPO_ROOT / "templates" / "scripts"))

    events: List[str] = []
    stored: List[Tuple[dict, list]] = []

    class _Data:
        def insert(self, properties=None, vector=None):
            events.append("insert")
            stored.append((properties, vector))

        def delete_by_id(self, uuid):
            events.append("delete")

    class _Q:
        def fetch_objects(self, filters=None, limit=None, after=None):
            return types.SimpleNamespace(objects=[types.SimpleNamespace(uuid="old")] if "delete" not in events else [])

    class _C:
        data = _Data()
        query = _Q()

    class _Srv:
        client = types.SimpleNamespace(collections=types.SimpleNamespace(get=lambda name: _C()))

        def _get_embedding(self, text):
            events.append("embed")
            if len(text) > 500:
                raise ES.ChunkOverflowError("m", len(text), "exceeds the context length")
            return _vec(text)

    chunks = [
        {"content": "first chunk", "chunk_number": 1, "total_chunks": 2, "token_count": 3, "source_id": "s"},
        {"content": "lorem ipsum " * 100, "chunk_number": 2, "total_chunks": 2, "token_count": 300, "source_id": "s"},
    ]
    assert pd.store_document_chunks(_Srv(), chunks, "s") is True
    assert events.index("delete") > max(i for i, e in enumerate(events) if e == "embed"), (
        "every row is embedded BEFORE the old rows are deleted")
    assert [p["chunk_number"] for p, _v in stored] == list(range(1, len(stored) + 1))
    assert {p["total_chunks"] for p, _v in stored} == {len(stored)} and len(stored) > 2
    assert "".join(p["content"] for p, _v in stored[1:]) == chunks[1]["content"]
    assert all(v == _vec(p["content"]) for p, v in stored)
