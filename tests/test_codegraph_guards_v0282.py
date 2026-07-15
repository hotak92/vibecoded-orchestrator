# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.82 (G1): the unified re-embed/patch/skip guard, and its wiring through
the analyzer so a metadata-only ``CODEGRAPH_EMBED_REVISION`` bump re-embeds
NOTHING across ALL five collection types (incl. multi-chunk entities).

WHY THESE TESTS EXIST (the golden-suite blind spots they cover)
--------------------------------------------------------------
The golden suite (``tests/test_codegraph_golden.py``) runs over a FRESH empty
fake store: fingerprints never match, so the SKIP / STAMP / prune logic is
UNEXERCISED, embed CALL COUNTS are not pinned (a mass re-embed passes golden),
and ``data.update`` patches are invisible. This file pins exactly those:

  * T1 — the G1 HEADLINE: a future metadata-only revision bump STAMPs every
    collection type (Module/Class/Function/API/Interaction) and a multi-chunk
    function with ZERO embed calls. Proven fail-on-base for the eager types +
    multi-chunk (see the docstring on ``test_T1_*``).
  * T2 — floor raised → all rows re-embed (the legitimate trigger still fires).
  * T3 — NULL-revision hash-matched row → EMBED (pins C3; passes on base too).
  * T4 — vectorless (rev=0) row → EMBED even when the hash matches.
  * T5 — chunk-count drift (stored 3, computed 2) → EMBED.
  * T6 — rust routing hits ``run_pure_extractor`` (G4); no direct
    ``_get_existing_module`` in rust.py.
  * T7 — module-write failure keeps a file in the preserve set (rider b).

Plus a full semantics-table pin over ``guards.classify_row`` /
``is_row_revision_stale`` / ``classify_stale_kind``.
"""
from __future__ import annotations

import importlib.util
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from vco_lib import codegraph_guards as guards
from vco_lib.codegraph_guards import RowAction

_THIS_DIR = Path(__file__).parent
_ANALYZER_PATH = _THIS_DIR.parent / "templates" / "scripts" / "analyze_code_graph.py"


def _load_analyzer() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_v0282_guards_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:  # pragma: no cover
        pytest.fail("weaviate-client not installed — CI env regression")
    return mod


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    return _load_analyzer()


# ═══════════════════════════════════════════════════════════════════════════
# PART A — pure guard semantics table (classify_row / is_row_revision_stale /
# classify_stale_kind). Every row of the plan's semantics table is pinned.
# ═══════════════════════════════════════════════════════════════════════════

_CUR = 5
_FLOOR = 3
_VLESS = 0


def _c(stored_hash, stored_rev, computed_hash, **kw) -> RowAction:
    return guards.classify_row(
        stored_hash, stored_rev, computed_hash,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
        **kw,
    )


def test_classify_hash_mismatch_embeds():
    assert _c("aaa", _CUR, "bbb") is RowAction.EMBED


def test_classify_empty_hashes_embed():
    assert _c("", _CUR, "bbb") is RowAction.EMBED
    assert _c("aaa", _CUR, "") is RowAction.EMBED
    assert _c(None, _CUR, "bbb") is RowAction.EMBED


def test_classify_match_at_current_skips():
    assert _c("h", _CUR, "h") is RowAction.SKIP


def test_classify_match_forward_dated_skips():
    # rev > current (a downgrade) → content valid → SKIP, never a re-embed.
    assert _c("h", _CUR + 3, "h") is RowAction.SKIP


def test_classify_null_revision_embeds():
    # C3: NULL / non-int → pre-revision-tracking ⇒ pre-floor break → EMBED.
    assert _c("h", None, "h") is RowAction.EMBED
    assert _c("h", "junk", "h") is RowAction.EMBED
    assert _c("h", True, "h") is RowAction.EMBED  # bool is junk, not int 1


def test_classify_vectorless_and_negative_embed():
    assert _c("h", 0, "h") is RowAction.EMBED       # vectorless sentinel
    assert _c("h", -1, "h") is RowAction.EMBED       # negative


def test_classify_below_floor_embeds():
    # 0 < rev < floor → vector in a stale embedding space → EMBED.
    assert _c("h", _FLOOR - 1, "h") is RowAction.EMBED


def test_classify_stamp_window_single_chunk():
    # floor ≤ rev < current, single-chunk → STAMP.
    assert _c("h", _FLOOR, "h") is RowAction.STAMP
    assert _c("h", _CUR - 1, "h") is RowAction.STAMP


def test_classify_stamp_window_multichunk_count_match():
    # Multi-chunk, count unchanged, all hashes match → STAMP (per chunk).
    assert _c(
        "h", _FLOOR, "h", is_chunkable=True,
        stored_total_chunks=3, computed_total_chunks=3,
    ) is RowAction.STAMP


def test_classify_stamp_window_multichunk_count_drift_embeds():
    # Count drift → genuine re-chunk → EMBED.
    assert _c(
        "h", _FLOOR, "h", is_chunkable=True,
        stored_total_chunks=3, computed_total_chunks=2,
    ) is RowAction.EMBED


def test_classify_embedding_space_mismatch_does_not_change_action_this_release():
    # This release: embedding_space_matches=False does NOT change the action
    # (enforcement is staged — plan DEFERRAL D1). The caller warns.
    assert _c("h", _FLOOR, "h", embedding_space_matches=False) is RowAction.STAMP
    assert _c("h", _CUR, "h", embedding_space_matches=False) is RowAction.SKIP


def test_is_row_revision_stale():
    assert guards.is_row_revision_stale(None, _CUR) is True
    assert guards.is_row_revision_stale("junk", _CUR) is True
    assert guards.is_row_revision_stale(_CUR - 1, _CUR) is True
    assert guards.is_row_revision_stale(0, _CUR) is True
    assert guards.is_row_revision_stale(_CUR, _CUR) is False
    # Documented asymmetry vs classify_row: is_row_revision_stale mirrors the
    # analyzer's per-file gate (skip only at EXACT equality), so a forward-dated
    # rev != current counts as stale here — the file re-walks, then the
    # per-object gate SKIPs it (classify_row returns SKIP for rev > current).
    assert guards.is_row_revision_stale(_CUR + 1, _CUR) is True


def test_classify_stale_kind():
    def k(rev):
        return guards.classify_stale_kind(
            rev, current_revision=_CUR, floor_revision=_FLOOR,
            vectorless_sentinel=_VLESS,
        )
    assert k(_CUR) == "current"
    assert k(_CUR + 1) == "current"
    assert k(None) == "embed_owed"
    assert k("junk") == "embed_owed"
    assert k(0) == "embed_owed"
    assert k(_FLOOR - 1) == "embed_owed"
    assert k(_FLOOR) == "stamp_owed"
    assert k(_CUR - 1) == "stamp_owed"


def test_all_chunks_stampable():
    def fp(h, rev, total):
        return {"content_hash": h, "embed_revision": rev, "total_chunks": total}
    # all stampable
    assert guards.all_chunks_stampable(
        [fp("a", _FLOOR, 3), fp("b", _FLOOR, 3), fp("c", _FLOOR, 3)],
        ["a", "b", "c"], 3,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
    ) is True
    # one chunk hash-mismatched → not all stampable
    assert guards.all_chunks_stampable(
        [fp("a", _FLOOR, 3), fp("X", _FLOOR, 3), fp("c", _FLOOR, 3)],
        ["a", "b", "c"], 3,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
    ) is False
    # one chunk absent (None fp) → not all stampable
    assert guards.all_chunks_stampable(
        [fp("a", _FLOOR, 3), None, fp("c", _FLOOR, 3)],
        ["a", "b", "c"], 3,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
    ) is False
    # one chunk at current (SKIP not STAMP) → not all stampable
    assert guards.all_chunks_stampable(
        [fp("a", _FLOOR, 3), fp("b", _CUR, 3), fp("c", _FLOOR, 3)],
        ["a", "b", "c"], 3,
        current_revision=_CUR, floor_revision=_FLOOR, vectorless_sentinel=_VLESS,
    ) is False


# ═══════════════════════════════════════════════════════════════════════════
# PART B — analyzer wiring. Fake collections with recording data.update /
# fetch_object_by_id, an embed COUNTER, and the real _dedup_insert path.
# ═══════════════════════════════════════════════════════════════════════════


class _RecData:
    def __init__(self):
        self.replaced: List[dict] = []
        self.inserted: List[dict] = []
        self.updated: List[dict] = []

    def replace(self, uuid, **kw):
        self.replaced.append({"uuid": uuid, **kw})

    def insert(self, uuid, **kw):
        self.inserted.append({"uuid": uuid, **kw})

    def update(self, uuid=None, properties=None, **kw):
        self.updated.append({"uuid": uuid, "properties": properties})


class _Existing:
    def __init__(self, properties):
        self.properties = properties


class _RecQuery:
    def __init__(self, stored: Dict[str, dict]):
        self.stored = stored

    def fetch_object_by_id(self, uuid, return_properties=None):
        props = self.stored.get(uuid)
        if props is None:
            return None
        if return_properties:
            props = {k: props.get(k) for k in return_properties}
        return _Existing(props)


class _RecColl:
    def __init__(self, name: str, stored: Optional[Dict[str, dict]] = None):
        self.name = name
        self.data = _RecData()
        self.query = _RecQuery(stored or {})


def _make_analyzer(analyzer_mod):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = "P"
    inst._track_visited = True
    inst._current_language = ""
    inst._current_source = ""
    inst.visited_uuids = set()
    inst.module_cache = {}
    inst._embed_skip_fingerprint = None
    return inst


# One (collection base, insert_params-builder) per entity type. Every builder
# returns a DEFERRED embed closure that bumps the shared counter, so we can
# assert ZERO embeds on the STAMP path.


def _props_for(base: str) -> Dict[str, Any]:
    if base == "CodeModule":
        return {"path": "src/a.py", "module_summary": "m", "import_names": []}
    if base == "CodeClass":
        return {
            "full_name": "a.C", "signature": "class C", "class_body": "class C: ...",
            "methods": [], "composes": [],
        }
    if base == "CodeFunction":
        return {
            "full_name": "a.f", "signature": "def f()",
            "function_body": "def f(): return 1", "type_uses": [],
            "cfg_summary": "", "data_flow_vars": [],
        }
    if base == "CodeAPI":
        return {
            "endpoint": "/x", "method": "GET", "api_description": "d",
            "parameters": [], "returns": "",
        }
    if base == "CodeInteraction":
        return {
            "interaction_type": "http", "protocol": "GET", "endpoint": "/x",
            "raw_target": "http://x/x", "direction": "outbound", "description": "d",
        }
    raise AssertionError(base)


def _identity_for(base: str) -> str:
    if base == "CodeModule":
        return "module::src/a.py"
    if base == "CodeAPI":
        return "/x:GET"
    if base == "CodeInteraction":
        return "ix::src::/x"
    return "a.C" if base == "CodeClass" else "a.f"


_ALL_BASES = ["CodeModule", "CodeClass", "CodeFunction", "CodeAPI", "CodeInteraction"]


def _drive_single(analyzer_mod, inst, base, current, floor, stored_rev):
    """Run one entity through _dedup_insert with a stored hash-matched row at
    `stored_rev`, under (current, floor). Returns (embed_calls, coll)."""
    props = _props_for(base)
    identity = _identity_for(base)
    coll = _RecColl(f"P_{base}")
    det = analyzer_mod._deterministic_uuid("P", "src/a.py", identity, project_source="")
    # Stored row: byte-identical content (single chunk) at `stored_rev`.
    stored_props = dict(props)
    if base in ("CodeClass", "CodeFunction"):
        stored_props["chunk_num"] = 0
        stored_props["total_chunks"] = 1
    stored_hash = analyzer_mod._content_hash_for_object(f"P_{base}", stored_props)
    coll.query.stored[det] = {
        "content_hash": stored_hash, "embed_revision": stored_rev,
        "total_chunks": 1,
    }
    calls = {"n": 0}

    def _embed():
        calls["n"] += 1
        return [0.1, 0.2, 0.3]

    insert_params = {"properties": dict(props), "_deferred_embed": _embed}
    with _patched_revision(analyzer_mod, current, floor):
        inst._dedup_insert(coll, insert_params, identity, file_path_rel="src/a.py")
    return calls["n"], coll


class _patched_revision:
    """Context manager: temporarily set CODEGRAPH_EMBED_REVISION + the floor on
    the analyzer module (the guard reads them via the analyzer's module-level
    constants threaded into the guard params)."""

    def __init__(self, analyzer_mod, current, floor):
        self.m = analyzer_mod
        self.current = current
        self.floor = floor

    def __enter__(self):
        self._old_cur = self.m.CODEGRAPH_EMBED_REVISION
        self._old_floor = self.m._EMBED_SPACE_COMPATIBLE_FROM_REVISION
        self.m.CODEGRAPH_EMBED_REVISION = self.current
        self.m._EMBED_SPACE_COMPATIBLE_FROM_REVISION = self.floor
        return self

    def __exit__(self, *a):
        self.m.CODEGRAPH_EMBED_REVISION = self._old_cur
        self.m._EMBED_SPACE_COMPATIBLE_FROM_REVISION = self._old_floor
        return False


# ── T1: the G1 headline — future metadata-only bump STAMPs every type, 0 embeds ─


@pytest.mark.parametrize("base", _ALL_BASES)
def test_T1_metadata_bump_stamps_every_type_zero_embeds(analyzer_mod, base):
    """Simulate a metadata-only revision bump: rows are hash-matched at rev=1;
    current=2, floor=1. EVERY collection type must PATCH embed_revision=2 via
    data.update and record ZERO embed calls, ZERO replace()/insert().

    FAIL-ON-BASE: on base 12ed39c9 the eager types (Module/API/Interaction)
    embedded EAGERLY (no _deferred_embed key → _resolve_deferred_embed no-op →
    the eager `vector` path re-embeds every visit); this asserts the closure is
    NOT called and data.update IS called — both false on base for those types.
    """
    inst = _make_analyzer(analyzer_mod)
    embed_calls, coll = _drive_single(
        analyzer_mod, inst, base, current=2, floor=1, stored_rev=1,
    )
    assert embed_calls == 0, f"{base}: STAMP path must not embed (got {embed_calls})"
    assert len(coll.data.updated) == 1, (
        f"{base}: expected exactly one embed_revision PATCH, "
        f"got {coll.data.updated}"
    )
    assert coll.data.updated[0]["properties"] == {"embed_revision": 2}
    assert coll.data.replaced == [] and coll.data.inserted == [], (
        f"{base}: STAMP path must not replace()/insert()"
    )


def test_T1_multichunk_function_stamps_zero_perchunk_embeds(analyzer_mod):
    """The multi-chunk half of T1: an over-budget function whose 3 chunk rows
    are hash-matched at rev=1 → current=2, floor=1 → all 3 chunks PATCH
    embed_revision=2 and the PER-CHUNK mass re-embed (module-level
    ``generate_embedding``, one call per chunk on the base) is AVOIDED entirely.

    NOTE on the one full-body precheck embed: for a chunkable entity
    ``_resolve_deferred_embed`` computes a SINGLE-chunk hash (total=1) that can
    never match a stored multi-chunk row (total=3), so it fires the deferred
    embedder once on the full body — a PRE-EXISTING cost that happens on every
    over-budget visit at ANY revision, independent of G1. What T1 pins is the
    N-per-chunk mass re-embed via ``generate_embedding``: ZERO on the stamp
    path. FAIL-ON-BASE: base re-embeds every chunk in ``_maybe_chunk_and_write``
    (no stamp path existed) → 3 generate_embedding calls; here → 0, and 3
    data.update PATCHes.
    """
    inst = _make_analyzer(analyzer_mod)
    coll = _RecColl("P_CodeFunction")
    identity = "a.big"
    chunk_bodies = ["def big(): #c0", "    #c1", "    #c2"]

    def _fake_chunk(signature, body, **kw):
        return chunk_bodies

    perchunk = {"n": 0}

    def _fake_generate_embedding(text):
        perchunk["n"] += 1
        return [0.9]

    old_chunk = analyzer_mod.chunk_or_truncate_for_embedding
    old_gen = analyzer_mod.generate_embedding
    analyzer_mod.chunk_or_truncate_for_embedding = _fake_chunk
    analyzer_mod.generate_embedding = _fake_generate_embedding
    fullbody = {"n": 0}

    def _fullbody_embed():
        fullbody["n"] += 1
        return [0.1]

    try:
        base_props = {
            "full_name": "a.big", "signature": "def big()",
            "function_body": "def big(): ...", "type_uses": [],
            "cfg_summary": "", "data_flow_vars": [],
        }
        for i, cb in enumerate(chunk_bodies):
            key = identity if i == 0 else f"{identity}::{i}"
            det = analyzer_mod._deterministic_uuid("P", "src/a.py", key, project_source="")
            hp = dict(base_props)
            hp["function_body"] = cb
            hp["chunk_num"] = i
            hp["total_chunks"] = 3
            h = analyzer_mod._content_hash_for_object("P_CodeFunction", hp)
            coll.query.stored[det] = {
                "content_hash": h, "embed_revision": 1, "total_chunks": 3,
            }
        insert_params = {
            "properties": dict(base_props), "_deferred_embed": _fullbody_embed,
        }
        with _patched_revision(analyzer_mod, current=2, floor=1):
            inst._dedup_insert(coll, insert_params, identity, file_path_rel="src/a.py")
    finally:
        analyzer_mod.chunk_or_truncate_for_embedding = old_chunk
        analyzer_mod.generate_embedding = old_gen

    # THE HEADLINE: the per-chunk mass re-embed is avoided.
    assert perchunk["n"] == 0, (
        f"multi-chunk STAMP must avoid the per-chunk mass re-embed "
        f"(got {perchunk['n']} generate_embedding calls)"
    )
    assert len(coll.data.updated) == 3, (
        f"expected 3 per-chunk embed_revision PATCHes, got {coll.data.updated}"
    )
    assert all(u["properties"] == {"embed_revision": 2} for u in coll.data.updated)
    assert coll.data.replaced == [] and coll.data.inserted == []


# ── T2: floor raised → all rows re-embed (legitimate trigger still fires) ─────


@pytest.mark.parametrize("base", _ALL_BASES)
def test_T2_floor_raised_reembeds_every_type(analyzer_mod, base):
    """current=2, floor=2 (a model/chunking bump): a rev=1 row is BELOW the
    floor → must re-embed (embed called, replace/insert happens, no STAMP)."""
    inst = _make_analyzer(analyzer_mod)
    embed_calls, coll = _drive_single(
        analyzer_mod, inst, base, current=2, floor=2, stored_rev=1,
    )
    assert embed_calls == 1, f"{base}: below-floor row must re-embed"
    assert coll.data.updated == [], f"{base}: below-floor must NOT stamp"
    assert coll.data.replaced or coll.data.inserted, f"{base}: must write"


# ── T3: NULL-revision hash-matched row → EMBED (C3; passes on base too) ───────


@pytest.mark.parametrize("base", _ALL_BASES)
def test_T3_null_revision_reembeds(analyzer_mod, base):
    inst = _make_analyzer(analyzer_mod)
    embed_calls, coll = _drive_single(
        analyzer_mod, inst, base, current=1, floor=1, stored_rev=None,
    )
    assert embed_calls == 1, f"{base}: NULL-revision row must re-embed (C3)"
    assert coll.data.updated == [], f"{base}: NULL-revision must NOT stamp"


# ── T4: vectorless (rev=0) hash-matched row → EMBED ──────────────────────────


@pytest.mark.parametrize("base", _ALL_BASES)
def test_T4_vectorless_reembeds(analyzer_mod, base):
    inst = _make_analyzer(analyzer_mod)
    embed_calls, coll = _drive_single(
        analyzer_mod, inst, base, current=2, floor=1, stored_rev=0,
    )
    assert embed_calls == 1, f"{base}: vectorless (rev=0) row must re-embed"
    assert coll.data.updated == [], f"{base}: vectorless must NOT stamp"


# ── T5: chunk-count drift (stored 3, computed 2) → EMBED, F3 shrink schedules ─


def test_T5_chunk_count_drift_reembeds(analyzer_mod):
    """A function stored as 3 chunks but this run computes 2: count drift →
    ``all_chunks_stampable`` is False → re-embed each of the 2 computed chunks
    (via module ``generate_embedding``), NOT a stamp."""
    inst = _make_analyzer(analyzer_mod)
    coll = _RecColl("P_CodeFunction")
    identity = "a.shrink"
    chunk_bodies = ["def shrink(): #c0", "    #c1"]  # computed 2 chunks

    def _fake_chunk(signature, body, **kw):
        return chunk_bodies

    perchunk = {"n": 0}

    def _fake_gen(text):
        perchunk["n"] += 1
        return [0.9]

    old_chunk = analyzer_mod.chunk_or_truncate_for_embedding
    old_gen = analyzer_mod.generate_embedding
    analyzer_mod.chunk_or_truncate_for_embedding = _fake_chunk
    analyzer_mod.generate_embedding = _fake_gen

    try:
        base_props = {
            "full_name": "a.shrink", "signature": "def shrink()",
            "function_body": "def shrink(): ...", "type_uses": [],
            "cfg_summary": "", "data_flow_vars": [],
        }
        # Seed 2 stored chunk rows claiming total_chunks=3 (drift).
        for i, cb in enumerate(chunk_bodies):
            key = identity if i == 0 else f"{identity}::{i}"
            det = analyzer_mod._deterministic_uuid("P", "src/a.py", key, project_source="")
            hp = dict(base_props)
            hp["function_body"] = cb
            hp["chunk_num"] = i
            hp["total_chunks"] = 3  # stored says 3, computed will be 2
            h = analyzer_mod._content_hash_for_object("P_CodeFunction", hp)
            coll.query.stored[det] = {
                "content_hash": h, "embed_revision": 1, "total_chunks": 3,
            }
        insert_params = {
            "properties": dict(base_props), "_deferred_embed": lambda: [0.1],
        }
        with _patched_revision(analyzer_mod, current=2, floor=1):
            inst._dedup_insert(coll, insert_params, identity, file_path_rel="src/a.py")
    finally:
        analyzer_mod.chunk_or_truncate_for_embedding = old_chunk
        analyzer_mod.generate_embedding = old_gen

    # Count drift → NOT a stamp → re-embed each of the 2 computed chunks.
    assert perchunk["n"] == 2, f"count drift must re-embed 2 chunks (got {perchunk['n']})"
    assert coll.data.updated == [], "count drift must NOT stamp"


# ── T6: rust routing (G4) — behavioral + source-shape ────────────────────────


def test_T6_rust_routes_through_run_pure_extractor(analyzer_mod, tmp_path):
    """A rust file through the dispatcher hits run_pure_extractor's gates:
    _get_existing_module is consulted (unchanged-skip), and on a skip the
    stats are the empty dict verbatim."""
    from vco_lib.codegraph_lang import rust as rust_mod

    repo = tmp_path
    f = repo / "lib.rs"
    f.write_text("pub fn hello() -> i32 { 1 }\n", encoding="utf-8")

    calls = {"get_existing": 0}

    class _Ctx:
        def _get_existing_module(self, rel, file_hash):
            calls["get_existing"] += 1
            return "UUID-EXISTS"  # force the unchanged-skip path

        def write_file_extraction(self, fx):  # pragma: no cover — skip means unreached
            raise AssertionError("write_file_extraction must NOT run on a skip")

    stats = rust_mod.analyze_rust_file(_Ctx(), f, repo)
    assert calls["get_existing"] == 1, "rust must consult _get_existing_module"
    assert stats == {"modules": 0, "classes": 0, "functions": 0}, (
        "skip must return the empty-stats dict verbatim"
    )


def test_T6_rust_source_has_no_direct_get_existing_module():
    """Source-shape guard: rust.py must NOT call _get_existing_module directly
    (the hand-copied twin was deleted; the gate lives in run_pure_extractor)."""
    rust_src = (
        _THIS_DIR.parent / "vco_lib" / "codegraph_lang" / "rust.py"
    ).read_text(encoding="utf-8")
    # Strip docstrings/comments so a doc mention of the gate name doesn't false-
    # positive; assert no CALL to the per-file gate remains.
    import ast as _ast

    tree = _ast.parse(rust_src)
    calls = [
        n for n in _ast.walk(tree)
        if isinstance(n, _ast.Attribute) and n.attr == "_get_existing_module"
    ]
    assert not calls, (
        "rust.py must route through run_pure_extractor, not call the per-file "
        "gate `_get_existing_module` directly (G4 de-duplication)"
    )
    assert "run_pure_extractor" in rust_src


# ── T7 (rider b): module write failure keeps the file in the preserve set ────


def test_T7_module_write_failure_preserves_file(analyzer_mod):
    """When the module write raises, the file must NOT be marked walked — so
    `discovered − walked` keeps it in the preserve set (never pruned).

    FAIL-ON-BASE: base marked the file walked at the ENTRY of
    _create_or_update_module (before the write could raise), so a failed write
    dropped the file from the preserve set.
    """
    inst = _make_analyzer(analyzer_mod)
    inst._prune_walked_paths = set()

    class _BoomColl:
        name = "P_CodeModule"

        class data:  # noqa: N801
            @staticmethod
            def update(*a, **k):
                raise RuntimeError("boom")

    inst.modules_collection = _BoomColl()

    # store_entity → _dedup_insert path: force the write to raise. Simplest: make
    # module_cache MISS (insert path) and store_entity raise. We drive the
    # public _create_or_update_module and expect it to propagate/leave-unmarked.
    def _boom_store_entity(entity):
        raise RuntimeError("boom")

    inst.store_entity = _boom_store_entity
    from datetime import datetime, timezone

    with pytest.raises(RuntimeError):
        inst._create_or_update_module(
            "src/broken.py", "python", 1, 1.0,
            datetime.now(tz=timezone.utc), "hash", [], "summary",
        )
    assert "src/broken.py" not in inst._prune_walked_paths, (
        "a file whose module write RAISED must stay OUT of the walked set "
        "(preserve set), never pruned (rider b)"
    )


def test_T7_module_write_success_marks_walked(analyzer_mod):
    """The leave-alone twin: a SUCCESSFUL module write DOES mark the file
    walked (so an unchanged-but-present file isn't spuriously preserved)."""
    inst = _make_analyzer(analyzer_mod)
    inst._prune_walked_paths = set()
    inst.modules_collection = _RecColl("P_CodeModule")

    def _ok_store_entity(entity):
        return "UUID-OK"

    inst.store_entity = _ok_store_entity
    from datetime import datetime, timezone

    inst._create_or_update_module(
        "src/ok.py", "python", 1, 1.0,
        datetime.now(tz=timezone.utc), "hash", [], "summary",
    )
    assert "src/ok.py" in inst._prune_walked_paths


# ── eager→deferred conversion source-shape (the extractor-wiring half of T1) ─


def test_eager_embed_sites_converted_to_deferred(analyzer_mod):
    """Source-shape pin: the Module / Interaction store sites (analyzer) and the
    API / class store sites (extractors) now pass ``deferred_embed`` — NOT an
    eager ``vector`` — so ``_resolve_deferred_embed`` covers every type.

    FAIL-ON-BASE: on base 12ed39c9 these sites set ``vector=...``; a metadata-
    only revision bump then re-embeds them (the guard is bypassed). This asserts
    the conversion happened.
    """
    analyzer_src = _ANALYZER_PATH.read_text(encoding="utf-8")
    # Module + Interaction store sites live in the analyzer; both must defer.
    assert "lambda ms=module_summary: embed_module(ms)" in analyzer_src, (
        "module store must defer its embed (was eager `vector`)"
    )
    assert "lambda d=description: generate_embedding(d)" in analyzer_src, (
        "interaction store must defer its embed (was eager `vector`)"
    )
    # No eager API/interaction/module `vector=_shape_for_insert(...)` remains in
    # the analyzer (the per-chunk fan-out sets chunk_params["vector"], a
    # different shape).
    assert "vector=_shape_for_insert(embedding) if embedding else None" not in analyzer_src

    # API extractors: no eager `vector=helpers.shape_for_insert(...)` anywhere.
    lang_dir = _THIS_DIR.parent / "vco_lib" / "codegraph_lang"
    offenders = []
    for f in lang_dir.glob("*.py"):
        if "vector=helpers.shape_for_insert" in f.read_text(encoding="utf-8"):
            offenders.append(f.name)
    assert not offenders, (
        f"these extractors still embed EAGERLY (convert to deferred_embed): "
        f"{offenders}"
    )


# ── provenance line format (WP-3 cross-WP contract) ──────────────────────────


def test_provenance_line_format():
    line = guards.provenance_line("codesage-large-v2", 2048, 1, "/nonexistent/repo")
    assert line.startswith("CODEGRAPH_PROVENANCE ")
    assert "model=codesage-large-v2" in line
    assert "dim=2048" in line
    assert "embed_revision=1" in line
    # non-git path → analyzed_commit=none
    assert "analyzed_commit=none" in line


def test_provenance_line_soft_fails_on_bad_inputs():
    line = guards.provenance_line(None, "junk", 3, "/nope")
    assert "model=unknown" in line
    assert "dim=0" in line
    assert "embed_revision=3" in line
