# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 pull-in ③ + ⑧a: MCP READ paths must never serve a SIBLING's row.

B3 fixed every DELETE / judge site that trusted a word-tokenized ``Equal`` on
``file_path`` / ``title``. The same filter also fed the MCP's READ-ONLY
lookups, where the over-match is wrong CONTEXT rather than data loss:

* ``search_code_graph``'s chunk fetcher — ``full_name Equal pkg.mod.run``
  also matched ``pkg.mod.run_all``, so that entity's chunks were assembled
  into this entity's three_chunks / full window as if they were its own;
* its same-file sibling fetcher — ``file_path Equal src/run.py`` also matched
  ``src/run_all.py`` and ``tests/src/run.py``;
* the KG neighbour-chunk fetch (``limit=1`` over ``file_path`` + ``chunk_num``)
  and chunk-window fetch (``title`` + ``file_path``);
* the WikiLink-target / node lookups by ``title`` (``limit=1``).

Every read site now confirms rows in Python through
``vco_lib.weaviate_exact_match``. Each scenario runs twice: shipped (the
sibling is NOT served) and RED-PROOF — ``fetch_matching_rows``'s accept
predicate disabled, i.e. the pre-fix "trust whatever the tokenized filter
returned" — where the sibling IS served, proving the fake is tokenized enough
to catch the defect.

⑧a: the paged collector de-duplicates by UUID (offset paging is not a
snapshot; a row can repeat across a page boundary).

In-memory fakes only — no Weaviate, no Ollama, no network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import vco_lib.weaviate_exact_match as wem
from tests.test_v02101_kg_exact_path_delete import _TokFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude_mcp_servers"))

from weaviate_mcp import server as srv  # noqa: E402

SHORT = "knowledge/concepts/knowledge-graph.md"
LONG_KG = "knowledge/concepts/orchestrator-knowledge-graph.md"


# ─── fakes ───────────────────────────────────────────────────────────────


_UID = iter(range(1, 10**9))


def _obj(props, *, uuid=None, distance=0.1, vector=None):
    return SimpleNamespace(
        uuid=uuid if uuid is not None else f"uuid-{next(_UID)}",
        properties=dict(props),
        metadata=SimpleNamespace(distance=distance, score=1.0 - distance),
        vector=vector or {},
    )


class _TokQuery:
    """``fetch_objects`` honours the (tokenized) filter + limit/offset;
    ``near_vector`` returns the configured hits."""

    def __init__(self, rows, hits=None):
        self.rows = rows
        self.hits = hits or []
        self.calls: list = []

    def fetch_objects(self, filters=None, limit=100, offset=0, **kw):
        self.calls.append({"limit": limit, "offset": offset, **kw})
        got = [r for r in self.rows if filters is None or filters.matches(r.properties)]
        return SimpleNamespace(objects=got[offset:offset + limit])

    def near_vector(self, **_kw):
        return SimpleNamespace(objects=list(self.hits))

    def near_text(self, **_kw):
        return SimpleNamespace(objects=list(self.hits))


class _TokColl:
    def __init__(self, rows, hits=None, name="Fake"):
        self.name = name
        self.query = _TokQuery(rows, hits)


@pytest.fixture
def tok_filter(monkeypatch):
    monkeypatch.setattr(srv, "Filter", _TokFilter)


@pytest.fixture
def disable_post_filter(monkeypatch):
    """Red-proof switch: ``fetch_matching_rows`` keeps every narrowed row —
    the pre-fix semantics — at the ONE shared home every read site calls."""
    real = wem.fetch_matching_rows

    def _apply():
        def _accept_all(coll, narrow_filter, _accept, **kw):
            return real(coll, narrow_filter, lambda _p: True, **kw)

        monkeypatch.setattr(wem, "fetch_matching_rows", _accept_all)

    return _apply


# ─── KG: neighbour chunk (limit=1 over file_path + chunk_num) ─────────────


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_adjacent_chunk_is_never_a_token_superset_siblings_chunk(
    tok_filter, disable_post_filter, mutated
):
    """The sibling's chunk 2 is stored FIRST, so ``limit=1`` returned it."""
    rows = [
        _obj({"title": "Orchestrator Knowledge Graph", "file_path": LONG_KG,
              "chunk_num": 2, "total_chunks": 3, "content": "SIBLING chunk 2"}),
        _obj({"title": "Knowledge Graph", "file_path": SHORT,
              "chunk_num": 2, "total_chunks": 3, "content": "OWN chunk 2"}),
    ]
    if mutated:
        disable_post_filter()
    got = srv._fetch_adjacent_chunks(
        _TokColl(rows), "Knowledge Graph", 1, 3, "KG", file_path=SHORT,
    )
    contents = [g["content"] for g in got]
    if mutated:
        assert contents == ["SIBLING chunk 2"]  # pre-fix: wrong neighbour
    else:
        assert contents == ["OWN chunk 2"], contents


def test_adjacent_title_fallback_serves_legacy_rows_rejects_other_paths(tok_filter):
    """Strategy 2 (title fallback for rows with no file_path stamp): a
    path-less legacy row with the same title is still served; a row of a
    superset title, or of the same title at ANOTHER path, is not."""
    rows = [
        _obj({"title": "Knowledge Graph Extended", "content": "[chunk 2/3]\n\nSUPERSET"}),
        _obj({"title": "Knowledge Graph", "file_path": LONG_KG,
              "content": "[chunk 2/3]\n\nOTHER-PATH"}),
        _obj({"title": "knowledge graph", "content": "[chunk 2/3]\n\nLEGACY-OWN"}),
    ]
    got = srv._fetch_adjacent_chunks(
        _TokColl(rows), "Knowledge Graph", 1, 3, "KG", file_path=SHORT,
    )
    assert [g["content"].split("\n")[-1] for g in got] == ["LEGACY-OWN"]


# ─── KG: chunk window (title AND file_path) ───────────────────────────────


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_chunk_window_never_interleaves_a_siblings_chunks(
    tok_filter, disable_post_filter, mutated
):
    rows = [
        _obj({"title": "Orchestrator Knowledge Graph", "file_path": LONG_KG,
              "chunk_num": n, "content": f"SIB-{n}"})
        for n in (1, 2, 3)
    ] + [
        _obj({"title": "Knowledge Graph", "file_path": SHORT,
              "chunk_num": n, "content": f"OWN-{n}"})
        for n in (1, 2, 3)
    ]
    if mutated:
        disable_post_filter()
    got = srv._fetch_node_chunks(
        _TokColl(rows), "Knowledge Graph", 2, 3, 7, file_path=SHORT,
    )
    bodies = [c for _n, c in got]
    if mutated:
        assert any(b.startswith("SIB") for b in bodies)
    else:
        assert bodies == ["OWN-1", "OWN-2", "OWN-3"], bodies


# ─── KG: WikiLink-target lookup by title (limit=1) ─────────────────────────
#
# Migrated in round 3: these cases used to drive the never-registered
# `get_node_connections` helper (retired as SUPERSEDED by this very
# traversal). The title lookup they guarded lives on in
# `semantic_graph_search`'s connected-nodes step, so they now drive that.


def _semantic_connected(monkeypatch, rows, primary):
    coll = _TokColl(rows, [primary])
    monkeypatch.setattr(srv, "Filter", _TokFilter)
    monkeypatch.setattr(srv, "EMBEDDING_SOURCE", "weaviate", raising=False)
    monkeypatch.setattr(srv, "_kg_collections_to_search", lambda **_k: [srv.KG_COLLECTION])
    monkeypatch.setattr(srv, "_stale_filter_for", lambda *_a, **_k: None)
    client = SimpleNamespace(collections=SimpleNamespace(get=lambda _n: coll))
    raw = asyncio.run(srv._semantic_graph_search_body(
        client, "primary", limit=3, depth=2, detail="titles", include_stale=True,
    ))
    return json.loads(raw)


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_connected_node_is_the_exactly_titled_link_target(
    monkeypatch, disable_post_filter, mutated
):
    """`[[uses::weaviate]]` must resolve to the node titled "Weaviate" (case
    tolerance kept), not to "Weaviate Windows Ports Gotcha" stored first."""
    primary = _obj({"title": "Primary Note", "content": "See [[uses::weaviate]].",
                    "file_path": "knowledge/concepts/primary-note.md"}, distance=0.1)
    rows = [
        _obj({"title": "Weaviate Windows Ports Gotcha", "content": "superset",
              "file_path": "knowledge/insights/weaviate-windows-ports.md"}),
        _obj({"title": "Weaviate", "content": "the tool",
              "file_path": "knowledge/tools/weaviate.md"}),
        primary,
    ]
    if mutated:
        disable_post_filter()
    data = _semantic_connected(monkeypatch, rows, primary)
    titles = [n.get("title") for n in data.get("connected_nodes", [])]
    if mutated:
        assert titles == ["Weaviate Windows Ports Gotcha"], data
    else:
        assert titles == ["Weaviate"], data


def test_connected_node_superset_only_is_not_served(monkeypatch):
    """LEAVE-ALONE inverse: when only a superset title exists, no connected
    node is served rather than the wrong one."""
    primary = _obj({"title": "Primary Note", "content": "See [[Weaviate]].",
                    "file_path": "knowledge/concepts/primary-note.md"}, distance=0.1)
    rows = [_obj({"title": "Weaviate Windows Ports Gotcha", "content": "superset",
                  "file_path": "knowledge/insights/weaviate-windows-ports.md"}), primary]
    data = _semantic_connected(monkeypatch, rows, primary)
    assert data.get("connected_nodes", []) == [], data


# ─── code graph: search_code_graph chunk + sibling fetch ──────────────────


def _wire_code(monkeypatch, per_collection, *, pipeline_tier=None):
    monkeypatch.setattr(srv, "_assert_workspace_unchanged", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "CODE_GRAPH_PROJECT", "MyProj", raising=False)
    monkeypatch.setattr(srv, "DUAL_EMBEDDING_ENABLED", False, raising=False)
    monkeypatch.setattr(srv, "_parse_csv_env", lambda _name: [])

    async def _fake_embed(_q):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(srv, "get_code_query_embedding", _fake_embed)
    monkeypatch.setattr(
        srv, "get_weaviate_client",
        lambda *a, **k: SimpleNamespace(
            collections=SimpleNamespace(get=per_collection)),
    )

    def _pipeline(rows, *_a, **kw):
        rows = rows[: kw.get("limit", 8)]
        if pipeline_tier:
            for r in rows:
                r["_tier"] = pipeline_tier
        return rows

    monkeypatch.setattr(srv, "run_code_retrieval_pipeline", _pipeline, raising=False)


def _code_collections(fn_rows, fn_hits):
    prefix = srv._code_sanitize_collection_prefix("MyProj")
    fn = _TokColl(fn_rows, fn_hits, name=f"{prefix}_CodeFunction")
    empty = {}

    def _get(name):
        if name == f"{prefix}_CodeFunction":
            return fn
        return empty.setdefault(name, _TokColl([], [], name=name))

    return _get, fn


def _search(**kw):
    return json.loads(asyncio.run(srv.search_code_graph(**kw)))


def _fn(full_name, file_path, *, chunk=1, total=1, body=None, start=1, project="MyProj"):
    return {
        "full_name": full_name, "name": full_name.rsplit(".", 1)[-1],
        "file_path": file_path, "project": project, "signature": "def x()",
        "chunk_num": chunk, "total_chunks": total, "start_line": start,
        "function_body": body if body is not None else f"[chunk {chunk}/{total}]\n\n{full_name}",
    }


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_code_chunk_window_never_assembles_a_sibling_entitys_chunks(
    monkeypatch, tok_filter, disable_post_filter, mutated
):
    """``pkg.mod.run`` (3 chunks) beside ``pkg.mod.run_all`` in the SAME file
    and a same-``full_name`` entity in a token-superset path: the full-tier
    window must hold only ``pkg.mod.run``'s own chunks."""
    own = [_fn("pkg.mod.run", "src/pkg/mod.py", chunk=n, total=3,
               body=f"[chunk {n}/3]\n\nOWN-{n}") for n in (1, 2, 3)]
    sib = [_fn("pkg.mod.run_all", "src/pkg/mod.py", chunk=n, total=3,
               body=f"[chunk {n}/3]\n\nSIB-{n}") for n in (1, 2, 3)]
    other_file = [_fn("pkg.mod.run", "src/pkg/mod_extra.py", chunk=n, total=3,
                      body=f"[chunk {n}/3]\n\nOTHERFILE-{n}") for n in (2, 3)]
    rows = [_obj(p) for p in sib + other_file + own]  # siblings stored first
    hit = _obj(own[0], vector={"codesage_embed": [0.1, 0.2, 0.3]})
    get, _fn_coll = _code_collections(rows, [hit])
    _wire_code(monkeypatch, get, pipeline_tier="full")
    if mutated:
        disable_post_filter()

    data = _search(query="run", scope="code", limit=3, project="MyProj")
    assert data["success"] is True, data
    blob = json.dumps(data["results"])
    if mutated:
        assert "SIB-" in blob or "OTHERFILE-" in blob  # pre-fix interleave
    else:
        assert "OWN-2" in blob and "OWN-3" in blob, blob
        assert "SIB-" not in blob and "OTHERFILE-" not in blob, blob


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_code_same_file_siblings_exclude_token_superset_files(
    monkeypatch, tok_filter, disable_post_filter, mutated
):
    hit_props = _fn("pkg.run.main", "src/run.py", start=10)
    rows = [
        _obj(_fn("pkg.run_all.go", "src/run_all.py", start=11)),
        _obj(_fn("tests.src.run.check", "tests/src/run.py", start=12)),
        _obj(_fn("pkg.run.legacy", "src/run.py", start=13, project="MyProj Legacy")),
        _obj(_fn("pkg.run.helper", "src/run.py", start=30)),
        _obj(hit_props),
    ]
    hit = _obj(hit_props, vector={"codesage_embed": [0.1, 0.2, 0.3]})
    get, _fn_coll = _code_collections(rows, [hit])
    _wire_code(monkeypatch, get, pipeline_tier=None)
    if mutated:
        disable_post_filter()

    data = _search(query="main", scope="code", limit=3, project="MyProj",
                   detail="auto")
    assert data["success"] is True, data
    top = data["results"][0]
    # Sibling refs carry only the identity (full_name) — the file is implied.
    sib_names = sorted(s.get("full_name") for s in top.get("siblings", []))
    if mutated:
        assert "pkg.run_all.go" in sib_names  # pre-fix: another file's entity
    else:
        assert sib_names == ["pkg.run.helper"], top.get("siblings")


# ─── shared helper: predicate builder + first-row reader ──────────────────


def test_exact_row_predicate_skips_empty_clauses_and_checks_each_kind():
    acc = wem.exact_row_predicate(
        paths={"file_path": SHORT}, values={"full_name": "a.b"},
        same_tokens={"project": "MyProj"},
    )
    good = {"file_path": SHORT.replace("/", "\\"), "full_name": "a.b", "project": "myproj"}
    assert acc(good)
    assert not acc({**good, "file_path": LONG_KG})
    assert not acc({**good, "full_name": "a.b_c"})
    assert not acc({**good, "project": "MyProj Legacy"})
    # Empty wanted values are skipped (the read site ANDed no clause).
    assert wem.exact_row_predicate(paths={"file_path": ""}, values={"x": None})({})
    # missing_path_ok: absent path passes, a DIFFERENT path still fails.
    lax = wem.exact_row_predicate(paths={"file_path": SHORT}, missing_path_ok=True)
    assert lax({}) and lax({"file_path": "  "}) and not lax({"file_path": LONG_KG})


def test_fetch_first_matching_row_pages_past_a_sibling_flood():
    rows = [_obj({"file_path": LONG_KG}) for _ in range(150)] + [_obj({"file_path": SHORT})]
    coll = _TokColl(rows)
    got = wem.fetch_first_matching_row(
        coll, wem.path_narrowing_filter(_TokFilter, SHORT),
        wem.exact_row_predicate(paths={"file_path": SHORT}),
    )
    assert got is not None and got.properties["file_path"] == SHORT
    assert [c["offset"] for c in coll.query.calls] == [0, 100]
    assert wem.fetch_first_matching_row(
        _TokColl([_obj({"file_path": LONG_KG})]),
        None, wem.exact_row_predicate(paths={"file_path": SHORT}),
    ) is None


# ─── ⑧a: UUID de-duplication across page boundaries ──────────────────────


class _ShiftingColl:
    """Offset paging under a concurrent insert: page 2 re-serves the last row
    of page 1 (the window shifted by one between the two reads)."""

    def __init__(self, page1, page2):
        self._pages = {0: page1, len(page1): page2}
        self.query = self
        self.name = "Shifting"

    def fetch_objects(self, filters=None, limit=100, offset=0, **_kw):
        return SimpleNamespace(objects=list(self._pages.get(offset, [])))


def test_fetch_matching_rows_dedupes_a_uuid_repeated_across_pages():
    page1 = [_obj({"file_path": SHORT}, uuid=f"u{i}") for i in range(100)]
    page2 = [page1[-1], _obj({"file_path": SHORT}, uuid="u100")]
    rows = wem.fetch_matching_rows(_ShiftingColl(page1, page2), None, lambda _p: True)
    uuids = [str(r.uuid) for r in rows]
    assert len(uuids) == len(set(uuids)) == 101


def test_dedupe_repeat_does_not_count_towards_max_matches():
    page1 = [_obj({"file_path": LONG_KG}, uuid=f"s{i}") for i in range(99)]
    page1.append(_obj({"file_path": SHORT}, uuid="own-1"))
    page2 = [page1[-1], _obj({"file_path": SHORT}, uuid="own-2")]
    rows = wem.fetch_matching_rows(
        _ShiftingColl(page1, page2), None,
        lambda p: p.get("file_path") == SHORT, max_matches=2,
    )
    assert [str(r.uuid) for r in rows] == ["own-1", "own-2"]


def test_rows_without_uuid_are_never_collapsed():
    rows = [SimpleNamespace(properties={"file_path": SHORT}) for _ in range(3)]
    coll = SimpleNamespace(
        name="NoUuid",
        query=SimpleNamespace(
            fetch_objects=lambda **_kw: SimpleNamespace(objects=rows)),
    )
    assert len(wem.fetch_matching_rows(coll, None, lambda _p: True)) == 3


# ═══ Round 2 (owner rulings): exact-then-fallback lookups + `calls` edges ═══
#
# * The ~15 `full_name` / `path` lookups in `query_code_structure`, the
#   `search_code_graph` expansion and the CG-2 language probe now go through
#   `_code_identity_rows` → `fetch_rows_exact_then_tolerant`: exact rows win
#   when any exist; on a miss the pre-fix tolerant read answers, so a lookup
#   that resolved before still resolves.
# * `calls` is a cross-REFERENCE (live-confirmed: 91/300 rows carry edges).
#   The call expansion and the `path` BFS read `properties["calls"]` — always
#   absent — so both served empty. They now walk the resolved edges.

from tests.test_v02101_kg_exact_path_delete import _TokPredicate  # noqa: E402

PROJ = "RefProj"


class _TokRefFilter(_TokFilter):
    """Tokenized fake plus a `by_ref(...).by_id().equal(...)` that matches
    nothing (the interaction-edge reads are not under test here)."""

    @staticmethod
    def by_ref(_name):
        return SimpleNamespace(by_id=lambda: SimpleNamespace(
            equal=lambda _v: _TokPredicate(lambda _p: False)))


class _RefColl:
    """Tokenized fetch + `return_references` resolution from a link table,
    shaped like the real client (targets on `.objects`, not a list)."""

    def __init__(self, rows, links=None, name="RefColl", hits=None):
        self.rows = rows
        self.links = links or {}
        self.name = name
        self.hits = hits or []
        self.query = self
        self.calls: list = []

    def near_vector(self, **_kw):
        return SimpleNamespace(objects=list(self.hits))

    def _attach(self, obj, return_references):
        if not return_references:
            obj.references = None
            return obj
        items = return_references if isinstance(return_references, (list, tuple)) else [return_references]
        refs = {}
        for item in items:
            targets = self.links.get(item.link_on, {}).get(str(obj.uuid), [])
            if targets:
                refs[item.link_on] = SimpleNamespace(objects=list(targets))
        obj.references = refs
        return obj

    def fetch_objects(self, filters=None, limit=100, offset=0, return_references=None, **kw):
        self.calls.append({"limit": limit, "offset": offset, **kw})
        got = [r for r in self.rows if filters is None or filters.matches(r.properties)]
        return SimpleNamespace(objects=[self._attach(o, return_references)
                                        for o in got[offset:offset + limit]])

    def fetch_object_by_id(self, uuid, return_references=None, **_kw):
        for o in self.rows:
            if str(o.uuid) == str(uuid):
                return self._attach(o, return_references)
        return None


def _fnrow(uuid, full_name, file_path, **extra):
    props = {"full_name": full_name, "name": full_name.rsplit(".", 1)[-1],
             "file_path": file_path, "project": PROJ, "chunk_num": 0,
             "total_chunks": 1, "signature": f"def {full_name}()",
             "function_body": full_name, **extra}
    return _obj(props, uuid=uuid)


def _structure(monkeypatch, colls: dict, query_type: str, target: str) -> dict:
    prefix = srv._code_sanitize_collection_prefix(PROJ)
    named = {f"{prefix}_{base}": c for base, c in colls.items()}

    def _get(name):
        if name not in named:
            raise RuntimeError(f"could not find class {name}")
        return named[name]

    monkeypatch.setattr(srv, "Filter", _TokRefFilter)
    monkeypatch.setattr(
        srv, "get_weaviate_client",
        lambda *a, **k: SimpleNamespace(collections=SimpleNamespace(get=_get)),
    )
    return json.loads(srv.query_code_structure(query_type, target, project=PROJ))


@pytest.fixture
def pre_fix_calls_read(monkeypatch):
    """Red-proof switch for the `calls` fix: read `calls` the pre-fix way —
    as a text list in `properties` (always absent for a reference)."""
    def _apply():
        monkeypatch.setattr(
            srv, "_code_call_targets",
            lambda fn_obj: [] if fn_obj is None
            else [SimpleNamespace(properties={"full_name": n}, uuid=None)
                  for n in ((fn_obj.properties or {}).get("calls") or [])],
        )
    return _apply


def _call_graph():
    a = _fnrow("ua", "pkg.a", "src/a.py")
    b = _fnrow("ub", "pkg.b", "src/b.py")
    c = _fnrow("uc", "pkg.c", "src/c.py")
    return [a, b, c], {"calls": {"ua": [b, b], "ub": [c]}}  # duplicate beacon on purpose


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_path_query_walks_the_calls_references(monkeypatch, pre_fix_calls_read, mutated):
    rows, links = _call_graph()
    if mutated:
        pre_fix_calls_read()
    data = _structure(monkeypatch, {"CodeFunction": _RefColl(rows, links)},
                      "path", "pkg.a->pkg.c")
    assert data["success"] is True, data
    if mutated:
        assert data["path_found"] is False  # pre-fix: the BFS never left pkg.a
    else:
        # A found path carries no `path_found` key (only the miss does).
        assert data.get("path_found", True) is True and data["count"] == 3, data
        assert [(r["full_name"], r["file_path"], r["hop"]) for r in data["results"]] == [
            ("pkg.a", "src/a.py", 0), ("pkg.b", "src/b.py", 1), ("pkg.c", "src/c.py", 2),
        ]


def test_path_query_destination_tolerates_separator_spelling(monkeypatch):
    rows, links = _call_graph()
    data = _structure(monkeypatch, {"CodeFunction": _RefColl(rows, links)},
                      "path", "pkg.a->pkg::c")
    assert data.get("path_found", True) is True and data["results"], data
    assert data["results"][-1]["full_name"] == "pkg.c"


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_path_seed_is_the_exact_function_not_a_token_superset(
    monkeypatch, disable_post_filter, mutated
):
    """`pkg.run_all` (stored first) is a token superset of `pkg.run`; it
    calls the destination directly, `pkg.run` reaches it through `pkg.mid`."""
    run_all = _fnrow("u-all", "pkg.run_all", "src/run_all.py")
    run = _fnrow("u-run", "pkg.run", "src/run.py")
    mid = _fnrow("u-mid", "pkg.mid", "src/mid.py")
    dest = _fnrow("u-dest", "pkg.dest", "src/dest.py")
    links = {"calls": {"u-all": [dest], "u-run": [mid], "u-mid": [dest]}}
    if mutated:
        disable_post_filter()
    data = _structure(monkeypatch, {"CodeFunction": _RefColl([run_all, run, mid, dest], links)},
                      "path", "pkg.run->pkg.dest")
    hops = [r["full_name"] for r in data["results"]]
    if mutated:
        assert hops == ["pkg.run", "pkg.dest"]  # walked run_all's edges
    else:
        assert hops == ["pkg.run", "pkg.mid", "pkg.dest"], data


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_search_expansion_follows_calls_references(
    monkeypatch, tok_filter, pre_fix_calls_read, mutated
):
    rows, links = _call_graph()
    prefix = srv._code_sanitize_collection_prefix("MyProj")
    for r in rows:
        r.properties["project"] = "MyProj"
    fn = _RefColl(rows, links, name=f"{prefix}_CodeFunction",
                  hits=[_obj(rows[0].properties, vector={"codesage_embed": [0.1, 0.2, 0.3]})])
    others: dict = {}

    def _get(name):
        if name == f"{prefix}_CodeFunction":
            return fn
        return others.setdefault(name, _TokColl([], [], name=name))

    _wire_code(monkeypatch, _get)
    monkeypatch.setattr(srv, "Filter", _TokRefFilter)
    if mutated:
        pre_fix_calls_read()
    data = _search(query="a", scope="code", limit=1, project="MyProj", expand_hops=2)
    expanded = [(r["full_name"], r["file_path"], r["hop"])
                for r in data["results"] if r.get("expanded")]
    if mutated:
        assert expanded == []  # pre-fix: call expansion never fired
    else:
        assert expanded == [("pkg.b", "src/b.py", 1), ("pkg.c", "src/c.py", 2)], data


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_methods_answers_for_the_exact_class(monkeypatch, disable_post_filter, mutated):
    wrong = _obj({"full_name": "pkg.Foo_Bar", "project": PROJ, "chunk_num": 0,
                  "file_path": "src/foo_bar.py", "methods": ["wrong"]}, uuid="c1")
    right = _obj({"full_name": "pkg.Foo", "project": PROJ, "chunk_num": 0,
                  "file_path": "src/foo.py", "methods": ["right"]}, uuid="c2")
    if mutated:
        disable_post_filter()
    data = _structure(monkeypatch, {"CodeClass": _RefColl([wrong, right])},
                      "methods", "pkg.Foo")
    names = [r["name"] for r in data["results"]]
    assert names == (["wrong"] if mutated else ["right"]), data


def test_methods_tolerant_fallback_keeps_a_lookup_that_resolved_before(monkeypatch):
    """Availability: a typed `pkg::Foo` has no exact row; the pre-fix
    tolerant read still resolves it."""
    only = _obj({"full_name": "pkg.Foo", "project": PROJ, "chunk_num": 0,
                 "file_path": "src/foo.py", "methods": ["right"]}, uuid="c2")
    data = _structure(monkeypatch, {"CodeClass": _RefColl([only])}, "methods", "pkg::Foo")
    assert data["success"] is True and [r["name"] for r in data["results"]] == ["right"]


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_dependencies_answers_for_the_exact_module_path(monkeypatch, disable_post_filter, mutated):
    ab = _obj({"path": "src/a_b.py", "project": PROJ}, uuid="m-ab")
    a = _obj({"path": "src/a.py", "project": PROJ}, uuid="m-a")
    x = _obj({"path": "src/x.py", "project": PROJ}, uuid="m-x")
    b = _obj({"path": "src/b.py", "project": PROJ}, uuid="m-b")
    links = {"imports": {"m-ab": [x], "m-a": [b]}}
    if mutated:
        disable_post_filter()
    data = _structure(monkeypatch, {"CodeModule": _RefColl([ab, a, x, b], links)},
                      "dependencies", "src/a.py")
    paths = [r["path"] for r in data["results"]]
    assert paths == (["src/x.py"] if mutated else ["src/b.py"]), data


def test_fetch_rows_exact_then_tolerant_contract():
    rows = [_obj({"file_path": LONG_KG}), _obj({"file_path": SHORT})]
    coll = _TokColl(rows)
    flt = wem.path_narrowing_filter(_TokFilter, SHORT)
    exact = wem.exact_row_predicate(paths={"file_path": SHORT})
    got = wem.fetch_rows_exact_then_tolerant(coll, flt, exact, tolerant_limit=1)
    assert [r.properties["file_path"] for r in got] == [SHORT]
    assert len(coll.query.calls) == 1  # no fallback read on an exact hit

    miss = _TokColl([_obj({"file_path": LONG_KG}), _obj({"file_path": LONG_KG + ".bak"})])
    got = wem.fetch_rows_exact_then_tolerant(miss, flt, exact, tolerant_limit=1)
    # The fallback is the pre-fix read verbatim: first `tolerant_limit` rows.
    assert [r.properties["file_path"] for r in got] == [LONG_KG]
    assert miss.query.calls[-1]["limit"] == 1


# ─── Round 4 (re-review nit 1): interaction expansion never regresses ────


class _TokIxFilter(_TokRefFilter):
    """`by_ref("source_function").by_id().equal(uuid)` matches interaction
    rows whose fake `_source_function` field carries that uuid."""

    @staticmethod
    def by_ref(name):
        def _equal(value):
            return _TokPredicate(lambda p: p.get(f"_{name}") == value)
        return SimpleNamespace(by_id=lambda: SimpleNamespace(equal=_equal))


def test_interaction_expansion_survives_a_project_mismatched_function(monkeypatch):
    """The pre-fix step-2 re-fetch had no project clause, so a function
    whose stored `project` differs from the effective one still expanded its
    interactions. The project-scoped resolve misses it; the clause-less
    retry must find it and the interaction must still be expanded."""
    prefix = srv._code_sanitize_collection_prefix("MyProj")
    fn_row = _fnrow("ua", "pkg.a", "src/a.py")
    fn_row.properties["project"] = "LegacyName"  # mismatches the effective project
    fn = _RefColl([fn_row], {}, name=f"{prefix}_CodeFunction",
                  hits=[_obj(fn_row.properties, vector={"codesage_embed": [0.1, 0.2, 0.3]})])
    ix = _TokColl([_obj({"_source_function": "ua", "endpoint": "/api/x",
                         "interaction_type": "http", "direction": "outbound",
                         "protocol": "http", "file_path": "src/a.py"})],
                  name=f"{prefix}_CodeInteraction")
    others: dict = {}

    def _get(name):
        if name == f"{prefix}_CodeFunction":
            return fn
        if name == f"{prefix}_CodeInteraction":
            return ix
        return others.setdefault(name, _TokColl([], [], name=name))

    _wire_code(monkeypatch, _get)
    monkeypatch.setattr(srv, "Filter", _TokIxFilter)
    data = _search(query="a", scope="code", limit=1, project="MyProj", expand_hops=1)
    endpoints = [r.get("endpoint") for r in data["results"]
                 if r.get("expanded") and r.get("collection") == "CodeInteraction"]
    assert endpoints == ["/api/x"], data
