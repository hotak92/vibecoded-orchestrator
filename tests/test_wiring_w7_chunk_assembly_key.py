# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""W7 (v0.2.92 wiring audit) — chunk assembly must key on the NODE, not the title.

The defect
----------
``_fetch_node_chunks`` (the ``single_chunk`` / ``three_chunks`` / ``full``
tier assembler) filtered on ``title`` ALONE, and ``_fetch_adjacent_chunks``
filtered on ``source_node_id == title``. A title is not a node identity:
measured live on this machine, **two** titles in ``VCODev_KnowledgeGraph``
and **two** in ``VCODev_Development`` each map to more than one
``file_path`` — e.g. one 3-chunk node and one 5-chunk node sharing the
title ``"Due Prodotti AI per Sviluppatori — Presentazione per
Investitori"``, whose ``chunk_num`` values (1,1,2,2,3,3,4,5) overlap. A
title-keyed window therefore spliced the OTHER node's chunks into this
node's assembled content, silently, with no marker in the output.

The key, and why file_path
--------------------------
Measured 2026-09-05 across ALL 21 live KG / Development collections on
this machine (1 545 rows):

  * ``file_path`` is populated on **1545 / 1545** rows — not one row
    anywhere lacks it. Grouping by it, EVERY group's rows agree on
    ``source_node_id`` and the group size equals the stored
    ``total_chunks``. It is also the key every WRITE path already scopes
    by (kg-sync's delete, the embed-skip gate and
    ``_stored_plan_matches_current`` all filter on ``file_path``).
  * Colliding titles are NOT a VCODev curiosity: 3 of the 21 collections
    have them — ``ARTup_Development`` has **10** titles mapping to more
    than one file_path, ``VCODev_Development`` and
    ``VCODev_KnowledgeGraph`` two each.
  * ``source_node_id`` — also 100% populated, but it is a per-WRITE uuid4
    for kg-sync rows and was literally ``title`` for MCP-written rows
    before W8, so on exactly the colliding-title population it collides
    identically. It is also absent from the search result dict, which
    already carries ``file_path``.

So ``file_path`` is the key, and it is already in hand at every call site.

Honest degradation
------------------
A result whose ``file_path`` is empty (no such row measured live) falls
back to the pre-fix title-only filter — no worse than before, never an
empty result — and the caller still reports a PARTIAL view rather than
claiming ``coverage: complete`` over chunks it did not assemble.

These tests drive the PRODUCTION entry points (``_format_result_by_tier``
for the window assembler, ``_enrich_with_adjacent_chunks`` for the
neighbour fetch), not the helpers directly, so removing the ``file_path``
key from either call site turns them red.
"""

from __future__ import annotations

import sys
import types
import uuid as _uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MCP_DIR = PROJECT_ROOT / "claude_mcp_servers"
for _p in (str(PROJECT_ROOT), str(MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _srv():
    """This repo's server module — asserted, not assumed.

    A sibling orchestrator checkout on ``sys.path`` (every maintainer box has
    one) can bind ``claude_mcp_servers.weaviate_mcp`` to the OTHER tree, and
    these tests would then silently validate code this repo does not ship.
    Same guard, same reason, as ``tests/test_v0292_chunk_plan_shared.py``.
    """
    import claude_mcp_servers.weaviate_mcp.server as srv  # noqa: E402

    resolved = Path(srv.__file__).resolve()
    expected = (MCP_DIR / "weaviate_mcp" / "server.py").resolve()
    assert resolved == expected, (
        f"weaviate_mcp.server resolved to {resolved}, not this repo's "
        f"{expected} — another checkout shadowed it (imported earlier in this "
        "process). Fix the import order rather than relaxing this assertion."
    )
    return srv


# ── The live collision, reproduced ────────────────────────────────────────
COLLIDING_TITLE = "Due Prodotti AI per Sviluppatori — Presentazione per Investitori"
NODE_A_FP = "docs/investor/it/pitch_document_it.md"          # 3 chunks
NODE_B_FP = "docs/investor/it/presentation_outline_it.md"    # 5 chunks


class _FakeObj:
    def __init__(self, properties: dict):
        self.uuid = str(_uuid.uuid4())
        self.properties = properties
        self.vector = {}
        # `_format_obj` reads `obj.metadata.distance` — a fake that omits it
        # raises inside the neighbour fetch's broad `except`, silently
        # degrading to the legacy content-prefix strategy and making this
        # suite pass for the wrong reason. Mirror the real object's shape.
        self.metadata = types.SimpleNamespace(distance=None)


class _Pred:
    def __init__(self, fn):
        self._fn = fn

    def matches(self, props: dict) -> bool:
        return bool(self._fn(props))

    def __and__(self, other):
        return _Pred(lambda p: self.matches(p) and other.matches(p))


class _ByProperty:
    def __init__(self, name: str):
        self._name = name

    def equal(self, value):
        return _Pred(lambda p, n=self._name, v=value: p.get(n) == v)


class _FakeFilter:
    @staticmethod
    def by_property(name: str):
        return _ByProperty(name)


class _FakeQuery:
    def __init__(self, coll):
        self._coll = coll

    def fetch_objects(self, filters=None, limit=100, offset=0, **_kw):
        objs = [
            o for o in self._coll.objects
            if filters is None or filters.matches(o.properties)
        ]
        self._coll.fetch_calls += 1
        return types.SimpleNamespace(objects=objs[(offset or 0):(offset or 0) + limit])


class _FakeCollection:
    def __init__(self, rows):
        self.objects = [_FakeObj(r) for r in rows]
        self.query = _FakeQuery(self)
        self.fetch_calls = 0


def _rows():
    """The measured live shape: one 3-chunk node and one 5-chunk node that
    share a title and overlap on chunk_num."""
    rows = []
    for n in (1, 2, 3):
        rows.append({
            "title": COLLIDING_TITLE, "file_path": NODE_A_FP,
            "chunk_num": n, "total_chunks": 3,
            "content": f"A-CHUNK-{n}", "node_type": "doc",
            "source_node_id": "aaaa-node", "tags": [],
        })
    for n in (1, 2, 3, 4, 5):
        rows.append({
            "title": COLLIDING_TITLE, "file_path": NODE_B_FP,
            "chunk_num": n, "total_chunks": 5,
            "content": f"B-CHUNK-{n}", "node_type": "doc",
            "source_node_id": "bbbb-node", "tags": [],
        })
    return rows


def _result(file_path: str, chunk_number: int, total_chunks: int) -> dict:
    return {
        "title": COLLIDING_TITLE,
        "file_path": file_path,
        "node_type": "doc",
        "tags": [],
        "score": 0.9,
        "content": f"snippet for {file_path}",
        "chunk_number": chunk_number,
        "total_chunks": total_chunks,
    }


# ── Part A — the tier window assembler (_format_result_by_tier) ───────────


def test_full_tier_assembles_only_the_matched_nodes_chunks(monkeypatch):
    """The ``full`` window over a colliding title must contain ONLY the
    matched node's chunks. Pre-fix the title-only filter returned all 8
    rows, so node A's 3-chunk "full" view was 7 chunks of two nodes."""
    srv = _srv()
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    coll = _FakeCollection(_rows())

    out = srv._format_result_by_tier(
        _result(NODE_A_FP, 2, 3), "full", sidecar_db={}, coll=coll,
    )
    body = out["content"]

    assert "B-CHUNK-1" not in body and "B-CHUNK-5" not in body, (
        "the other node's chunks were spliced into this node's assembled "
        "content — the assembly key is not the node"
    )
    for n in (1, 2, 3):
        assert f"A-CHUNK-{n}" in body
    assert out["chunks_shown"] == 3
    # All 3 of A's chunks present → the caller may claim complete coverage.
    assert out.get("coverage") == "complete"


def test_three_chunks_tier_window_is_not_stolen_by_the_twin(monkeypatch):
    """A 3-wide window centred on B's chunk 4 must be B's 3,4,5 — with a
    title-only filter the 8-row list sorts A and B chunks together and the
    window lands on the wrong rows."""
    srv = _srv()
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    coll = _FakeCollection(_rows())

    out = srv._format_result_by_tier(
        _result(NODE_B_FP, 4, 5), "three_chunks", sidecar_db={}, coll=coll,
    )
    body = out["content"]

    assert body.count("A-CHUNK") == 0
    assert "B-CHUNK-3" in body and "B-CHUNK-4" in body and "B-CHUNK-5" in body
    assert out["chunks_shown"] == 3
    assert out["chunks_total"] == 5
    # Partial view (3 of 5) → NO complete-coverage claim.
    assert "coverage" not in out


def test_missing_file_path_degrades_honestly_not_silently(monkeypatch):
    """HONEST DEGRADATION: a row with no ``file_path`` (none measured live)
    still assembles by title — the pre-fix behaviour, never an empty result
    — but it must NOT claim ``coverage: complete``. The title-only fallback
    can return the right COUNT of the wrong rows, and vouching for that is
    exactly the silent mis-assembly W7 is about."""
    srv = _srv()
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    coll = _FakeCollection(_rows())

    out = srv._format_result_by_tier(
        _result("", 2, 3), "three_chunks", sidecar_db={}, coll=coll,
    )

    assert out["chunks_shown"] == 3, (
        "the fallback must still assemble a window, not return nothing"
    )
    assert "coverage" not in out, (
        "a keyless assembly must not certify the node as fully covered"
    )
    assert "retrieval_hint" not in out


def test_single_chunk_node_of_a_colliding_title_stays_single(monkeypatch):
    """The KG collision measured live is two SINGLE-chunk nodes sharing a
    title ('Weaviate'). Each must render only its own content."""
    srv = _srv()
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    rows = [
        {"title": "Weaviate", "file_path": "knowledge/tools/weaviate.md",
         "chunk_num": 1, "total_chunks": 1, "content": "CANONICAL",
         "node_type": "tool", "tags": [], "source_node_id": "x"},
        {"title": "Weaviate", "file_path": "knowledge/tools/weaviate.from-claude.md",
         "chunk_num": 1, "total_chunks": 1, "content": "IMPORTED",
         "node_type": "tool", "tags": [], "source_node_id": "y"},
    ]
    coll = _FakeCollection(rows)
    result = {
        "title": "Weaviate", "file_path": "knowledge/tools/weaviate.md",
        "node_type": "tool", "tags": [], "score": 0.9,
        "content": "CANONICAL", "chunk_number": 1, "total_chunks": 1,
    }

    out = srv._format_result_by_tier(result, "full", sidecar_db={}, coll=coll)

    assert "IMPORTED" not in out["content"]


# ── Part B — the neighbour fetch (_enrich_with_adjacent_chunks) ───────────


def test_adjacent_chunk_fetch_never_crosses_the_title_collision(monkeypatch):
    """``_fetch_adjacent_chunks`` used ``source_node_id == title``: it
    matched only MCP-written rows (source_node_id WAS the title there) and,
    under a duplicate title, picked an arbitrary node's chunk via limit=1.
    Keyed on file_path + chunk_num it returns exactly this node's
    neighbours — and returns them for kg-sync rows at all."""
    srv = _srv()
    monkeypatch.setattr(srv, "Filter", _FakeFilter)
    coll = _FakeCollection(_rows())

    enriched = srv._enrich_with_adjacent_chunks(
        coll, [_result(NODE_A_FP, 2, 3)], "VCODev_Development",
    )

    neighbours = [r for r in enriched if r.get("content", "").startswith(("A-", "B-"))]
    contents = {r["content"] for r in neighbours}
    assert contents == {"A-CHUNK-1", "A-CHUNK-3"}, (
        f"expected exactly node A's neighbours, got {sorted(contents)}"
    )


# ── Part C — the sidecar chunk-summary fetchers ──────────────────────────
#
# `chunk_summaries` is what the retrieval chunk-map header (`▶`/`·`) renders,
# and it is built by two title-keyed fetchers of their own. Both are W7's
# shape; one of them additionally read a property name that does not exist
# in the schema, so it returned nothing for every node.


class _SidecarFakeWeaviate:
    """Injectable stand-in for the `weaviate` package these scripts import."""

    def __init__(self, rows):
        self.rows = rows
        self.last_filter = None
        self.closed = False

    # -- module surface --------------------------------------------------
    def connect_to_local(self, host=None, port=None, grpc_port=None):  # noqa: ARG002
        return self

    # -- client surface --------------------------------------------------
    @property
    def collections(self):
        return types.SimpleNamespace(get=lambda _name: self)

    @property
    def query(self):
        return types.SimpleNamespace(fetch_objects=self._fetch)

    def _fetch(self, filters=None, limit=20):
        self.last_filter = filters
        objs = [
            _FakeObj(r) for r in self.rows
            if filters is None or filters.matches(r)
        ]
        return types.SimpleNamespace(objects=objs[:limit])

    def close(self):
        self.closed = True


def _install_fake_weaviate(monkeypatch, rows):
    fake = _SidecarFakeWeaviate(rows)
    query_mod = types.ModuleType("weaviate.classes.query")
    query_mod.Filter = _FakeFilter
    classes_mod = types.ModuleType("weaviate.classes")
    classes_mod.query = query_mod
    weaviate_mod = types.ModuleType("weaviate")
    weaviate_mod.connect_to_local = fake.connect_to_local
    weaviate_mod.classes = classes_mod
    monkeypatch.setitem(sys.modules, "weaviate", weaviate_mod)
    monkeypatch.setitem(sys.modules, "weaviate.classes", classes_mod)
    monkeypatch.setitem(sys.modules, "weaviate.classes.query", query_mod)
    return fake


def _load_by_path(name: str, rel: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_shipped_summary_generator_fetches_only_this_nodes_chunks(monkeypatch):
    """`templates/scripts/generate-kg-summary.py` builds the chunk_summaries
    sidecar. Title-only, it blended the colliding node's chunks in."""
    monkeypatch.setenv("KG_COLLECTION", "VCODev_Development")
    _install_fake_weaviate(monkeypatch, _rows())
    mod = _load_by_path("_w7_kgsummary", "templates/scripts/generate-kg-summary.py")

    chunks = mod.get_chunks_from_weaviate(COLLIDING_TITLE, NODE_A_FP)

    assert [c for _n, c in chunks] == ["A-CHUNK-1", "A-CHUNK-2", "A-CHUNK-3"]


def test_internal_formats_generator_reads_the_real_chunk_property(monkeypatch):
    """`generate_node_formats.py` read ``chunk_number``; the SCHEMA property
    is ``chunk_num`` (that is what every writer stores — ``chunk_number`` is
    only the MCP result formatter's name for it), so its `cn is not None`
    guard rejected every row and per-chunk summaries were never generated
    for ANY node."""
    monkeypatch.setenv("KG_COLLECTION", "VCODev_Development")
    _install_fake_weaviate(monkeypatch, _rows())
    mod = _load_by_path("_w7_nodeformats", "claude_mcp_servers/scripts/generate_node_formats.py")

    chunks = mod.get_chunks_from_weaviate(COLLIDING_TITLE, NODE_B_FP)

    assert chunks, "the fetch must find rows at all — it read the wrong property"
    assert [c for _n, c in chunks] == [f"B-CHUNK-{i}" for i in range(1, 6)]
