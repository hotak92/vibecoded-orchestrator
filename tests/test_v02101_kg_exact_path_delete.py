# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.101 B3: KG sync / MCP store must never delete a TOKEN-SUBSET sibling.

``file_path`` and ``title`` are ``TEXT`` with Weaviate's default ``word``
tokenization, so ``Filter.by_property("file_path").equal(p)`` matches every
row whose token set CONTAINS ``p``'s tokens. Live-confirmed: ``Equal
"knowledge/concepts/knowledge-graph.md"`` returned the rows of five files,
including ``orchestrator-knowledge-graph.md`` and ``orchestrator-code-graph.md``.
Every caller then deleted that whole set — so syncing (or archiving) the
short-named node silently wiped its active siblings, and the embed-skip gate,
judging the polluted set, never fired for the killer either.

The fake ``Filter`` below reproduces exactly that tokenized ``Equal`` (the
in-suite fakes elsewhere compare exact strings, which is why the hole was
invisible to them). Each destructive path is exercised twice:

* with the shipped code — the siblings survive;
* with the Python exact-match check MUTATED to accept every narrowed row (the
  pre-fix semantics) — the sibling is deleted. That leg is the red-proof: it
  shows the fake is tokenized enough to catch the defect, so a future edit that
  drops the exact check fails here rather than in a user's KG.

In-memory fakes only — no Weaviate, no Ollama, no fixture-class writes.
"""

from __future__ import annotations

import itertools
import re
from types import SimpleNamespace

import pytest

import vco_lib.weaviate_exact_match as wem
from tests.test_v0270_kg_sync_batch_title_collision import (
    _FakeServer,
    _load_sync_module,
    _write,
)
from tests.test_v0273_kg_write_path import (
    _FakeCollection as _McpFakeCollection,
    _FakeObj as _McpFakeObj,
    _patch_server_for_store,
    _store,
)

SHORT = "knowledge/concepts/knowledge-graph.md"
LONG_KG = "knowledge/concepts/orchestrator-knowledge-graph.md"
LONG_CG = "knowledge/concepts/orchestrator-code-graph.md"


# ─── Tokenized fake Filter (Weaviate `word` tokenization semantics) ──────


def _tokens(value) -> set:
    return set(re.findall(r"[^\W_]+", str(value).lower()))


class _TokPredicate:
    def __init__(self, fn):
        self._fn = fn

    def matches(self, props: dict) -> bool:
        return bool(self._fn(props))

    def __and__(self, other):
        return _TokPredicate(lambda p: self.matches(p) and other.matches(p))


class _TokProp:
    def __init__(self, name: str):
        self._name = name

    def equal(self, value):
        name = self._name
        if isinstance(value, str):
            want = _tokens(value)

            def _match(p):
                raw = p.get(name)
                return isinstance(raw, str) and want <= _tokens(raw)

            return _TokPredicate(_match)
        return _TokPredicate(lambda p: p.get(name) == value)


class _TokFilter:
    @staticmethod
    def by_property(name: str) -> _TokProp:
        return _TokProp(name)

    @staticmethod
    def any_of(preds):
        preds = list(preds)
        return _TokPredicate(lambda p: any(pr.matches(p) for pr in preds))


def test_fake_filter_reproduces_the_live_tokenized_overmatch():
    """Guard the guard: the fake must over-match exactly like live Weaviate."""
    f = _TokFilter.by_property("file_path").equal(SHORT)
    assert f.matches({"file_path": SHORT})
    assert f.matches({"file_path": LONG_KG})
    assert f.matches({"file_path": LONG_CG})
    assert not f.matches({"file_path": "knowledge/concepts/semantic-search.md"})


@pytest.fixture
def mutate_exact_check(monkeypatch):
    """Red-proof switch: make the exact-path predicate accept every narrowed
    row — i.e. restore the pre-fix 'delete whatever the tokenized filter
    returned' semantics at the ONE shared home both writers call."""
    def _apply():
        monkeypatch.setattr(wem, "is_exact_path", lambda raw, canonical: True)
    return _apply


# ─── Shared helper: unit behaviour ───────────────────────────────────────


def test_is_exact_path_accepts_both_spellings_only():
    assert wem.is_exact_path(SHORT, SHORT)
    assert wem.is_exact_path(SHORT.replace("/", "\\"), SHORT)
    assert not wem.is_exact_path(LONG_KG, SHORT)
    assert not wem.is_exact_path("", SHORT)
    assert not wem.is_exact_path(None, SHORT)


def test_is_same_title_rejects_token_supersets_keeps_case_punct_insensitivity():
    assert wem.is_same_title("Weaviate", "weaviate")
    assert wem.is_same_title("Knowledge Graph", "knowledge-graph")
    assert not wem.is_same_title("Weaviate Windows Ports Gotcha", "Weaviate")
    assert not wem.is_same_title("Orchestrator Knowledge Graph", "Knowledge Graph")
    assert not wem.is_same_title("Anything", "")


class _PagingColl:
    """Minimal collection whose fetch honours limit/offset and the filter."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.query = self

    def fetch_objects(self, filters=None, limit=100, offset=0, **kw):
        self.calls.append({"limit": limit, "offset": offset, **kw})
        hits = [r for r in self.rows if filters is None or filters.matches(r.properties)]
        return SimpleNamespace(objects=hits[offset:offset + limit])


_ROW_SEQ = itertools.count(1)


def _row(fp, **props):
    # A UNIQUE uuid per row: the shared reader de-duplicates by UUID (v0.2.101
    # ⑧a), and the former ``id(props)`` key was recycled once a kwargs dict
    # was garbage-collected, so distinct fake rows could share a UUID.
    return SimpleNamespace(uuid=f"u-{next(_ROW_SEQ)}-{fp}", properties={"file_path": fp, **props})


def test_fetch_exact_path_rows_pages_past_sibling_flood():
    """A tokenized narrowing read can be FILLED by siblings: 250 sibling rows
    precede the target's 3 — a single limit=100 read would miss them all."""
    rows = [_row(LONG_KG, chunk_num=i) for i in range(250)]
    rows += [_row(SHORT, chunk_num=i) for i in range(1, 4)]
    coll = _PagingColl(rows)
    flt = wem.path_narrowing_filter(_TokFilter, SHORT)
    got = wem.fetch_exact_path_rows(coll, flt, SHORT, return_properties=["chunk_num"])
    assert [r.properties["file_path"] for r in got] == [SHORT] * 3
    # file_path was added to the caller's return_properties (predicate input).
    assert "file_path" in coll.calls[0]["return_properties"]
    # First page carries no `offset` kwarg (byte-identical first read).
    assert "offset" not in coll.calls[0] or coll.calls[0]["offset"] == 0


def test_predicate_error_never_selects_a_row():
    coll = _PagingColl([_row(SHORT)])

    def _boom(_props):
        raise RuntimeError("predicate bug")

    assert wem.fetch_matching_rows(coll, None, _boom) == []


# ─── sync_node / archived path / dev collection (kg-sync script) ─────────


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    # _load_sync_module writes these straight into os.environ; registering
    # them with monkeypatch first restores the originals at teardown.
    for key in ("KG_BASE_DIR", "KG_COLLECTION", "DEVELOPMENT_COLLECTION",
                "DUAL_EMBEDDING_ENABLED", "VCT_DISABLE_HUB_RESOLVER"):
        monkeypatch.setenv(key, "placeholder")
    mod = _load_sync_module(tmp_path)
    setattr(mod, "Filter", _TokFilter)
    server = _FakeServer()
    return mod, server, tmp_path


def _kg_paths(mod, server, coll_name=None) -> list:
    store = server.client.collections.get(coll_name or mod.COLLECTION_NAME)._store
    return sorted(o.properties.get("file_path") for o in store.values())


def _seed_siblings(mod, server, root):
    _write(root / LONG_KG, "Orchestrator Knowledge Graph", "active", "Long KG body.")
    _write(root / LONG_CG, "Orchestrator Code Graph", "active", "Long CG body.")
    assert mod.sync_node(server, root / LONG_KG)
    assert mod.sync_node(server, root / LONG_CG)
    assert _kg_paths(mod, server) == sorted([LONG_KG, LONG_CG])


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_sync_node_short_name_does_not_delete_token_superset_siblings(
    sync_env, mutate_exact_check, mutated
):
    mod, server, root = sync_env
    _seed_siblings(mod, server, root)
    if mutated:
        mutate_exact_check()

    _write(root / SHORT, "Knowledge Graph", "active", "Short body v1.")
    assert mod.sync_node(server, root / SHORT)
    _write(root / SHORT, "Knowledge Graph", "active", "Short body v2, changed.")
    assert mod.sync_node(server, root / SHORT)

    paths = _kg_paths(mod, server)
    if mutated:
        # Pre-fix semantics: the short node's upsert deleted its siblings.
        assert LONG_KG not in paths and LONG_CG not in paths
    else:
        assert paths == sorted([SHORT, LONG_KG, LONG_CG]), paths


def test_sync_node_embed_skip_fires_beside_token_superset_siblings(sync_env):
    """The killer was re-embedded on every pass too: the gate judged a set
    polluted with the siblings' (different-hash) rows. Now it skips."""
    mod, server, root = sync_env
    _seed_siblings(mod, server, root)
    _write(root / SHORT, "Knowledge Graph", "active", "Short body.")
    assert mod.sync_node(server, root / SHORT)
    again = mod.sync_node(server, root / SHORT)
    assert again.status == mod.OUTCOME_EMBED_SKIPPED, again.reason
    assert _kg_paths(mod, server) == sorted([SHORT, LONG_KG, LONG_CG])


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_archiving_short_name_removes_only_its_own_rows(
    sync_env, mutate_exact_check, mutated
):
    mod, server, root = sync_env
    _seed_siblings(mod, server, root)
    _write(root / SHORT, "Knowledge Graph", "active", "Short body.")
    assert mod.sync_node(server, root / SHORT)
    if mutated:
        mutate_exact_check()

    # The node is archived since its last sync — the v0.2.101 archived-removal
    # routing sends exactly this file through sync_node's archived leg.
    _write(root / SHORT, "Knowledge Graph", "archived", "Short body.")
    outcome = mod.sync_node(server, root / SHORT)
    assert outcome.status in (
        mod.OUTCOME_ARCHIVED_SKIPPED, mod.OUTCOME_FRONTMATTER_SKIPPED,
    ), outcome.status

    paths = _kg_paths(mod, server)
    assert SHORT not in paths
    if mutated:
        assert LONG_KG not in paths and LONG_CG not in paths
    else:
        assert paths == sorted([LONG_KG, LONG_CG]), paths


def test_delete_node_by_file_path_removes_legacy_backslash_row_not_sibling(sync_env):
    mod, server, _root = sync_env
    coll = server.client.collections.get(mod.COLLECTION_NAME)
    coll.data.insert(properties={"title": "Knowledge Graph",
                                 "file_path": SHORT.replace("/", "\\")})
    coll.data.insert(properties={"title": "Orchestrator Knowledge Graph",
                                 "file_path": LONG_KG})
    assert mod._delete_node_by_file_path(server, SHORT) == 1
    assert _kg_paths(mod, server) == [LONG_KG]


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_dev_collection_doc_delete_spares_token_superset_doc(
    sync_env, mutate_exact_check, mutated
):
    mod, server, _root = sync_env
    if mutated:
        mutate_exact_check()
    coll = server.client.collections.get(mod.DEV_COLLECTION_NAME)
    coll.data.insert(properties={"title": "README", "file_path": "docs/README.md"})
    coll.data.insert(properties={"title": "Setup", "file_path": "docs/setup/README.md"})

    removed = mod._delete_doc_by_file_path(server, "docs/README.md")
    paths = _kg_paths(mod, server, mod.DEV_COLLECTION_NAME)
    if mutated:
        assert removed == 2 and paths == []
    else:
        assert removed == 1 and paths == ["docs/setup/README.md"]


def test_tag_inference_reads_only_the_exactly_titled_link_target(sync_env):
    """B3 follow-up: `[[uses::Weaviate]]` used to inherit the tags of whichever
    title CONTAINING "weaviate" came first."""
    mod, server, _root = sync_env
    coll = server.client.collections.get(mod.COLLECTION_NAME)
    coll.data.insert(properties={"title": "Weaviate Windows Ports Gotcha",
                                 "chunk_num": 1, "tags": ["windows", "ports"]})
    coll.data.insert(properties={"title": "Weaviate", "chunk_num": 1,
                                 "tags": ["vector-db"]})
    node = {"tags": [], "typed_links": [
        {"relation_type": "uses", "target_title": "weaviate"},
    ]}
    assert mod.infer_tags_from_typed_links(server, node) == ["vector-db"]
    # And no tags at all when only a CONTAINING title exists.
    node_only_superset = {"tags": [], "typed_links": [
        {"relation_type": "uses", "target_title": "Ports"},
    ]}
    assert mod.infer_tags_from_typed_links(server, node_only_superset) == []


def test_wikilink_resolution_points_at_the_exact_title(sync_env):
    mod, server, _root = sync_env
    coll = server.client.collections.get(mod.COLLECTION_NAME)
    coll.data.insert(properties={"title": "Orchestrator Knowledge Graph", "chunk_num": 1})
    want = coll.data.insert(properties={"title": "Knowledge Graph", "chunk_num": 1})
    assert mod.resolve_wikilinks_to_uuids(server, ["Knowledge Graph"]) == [want]


# ─── MCP store_knowledge_node ────────────────────────────────────────────


def _mcp_coll(*props_list) -> _McpFakeCollection:
    coll = _McpFakeCollection()
    for props in props_list:
        coll.objects.append(_McpFakeObj(dict(props)))
    return coll


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_mcp_store_does_not_delete_token_subset_title_and_path_sibling(
    monkeypatch, tmp_path, mutate_exact_check, mutated
):
    """Title "Knowledge Graph" ⊂ "Orchestrator Knowledge Graph" AND path
    knowledge-graph.md ⊂ orchestrator-knowledge-graph.md: ANDing the two
    tokenized predicates did not separate them."""
    coll = _mcp_coll(
        {"title": "Knowledge Graph", "file_path": SHORT},
        {"title": "Orchestrator Knowledge Graph", "file_path": LONG_KG},
    )
    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    monkeypatch.setattr(srv, "Filter", _TokFilter)
    if mutated:
        mutate_exact_check()

    result = _store(srv, title="Knowledge Graph", file_path=SHORT)
    assert result.get("success") is True

    paths = sorted(str(o.properties.get("file_path")) for o in coll.objects)
    if mutated:
        assert paths == [SHORT]  # the sibling was wiped (pre-fix)
    else:
        assert paths == sorted([SHORT, LONG_KG]), paths


def test_mcp_store_replaces_both_spellings_and_pages_past_100(monkeypatch, tmp_path):
    """C-7 behaviour (replaces the former source pins): every old row of the
    node — POSIX and legacy backslash spellings, 150 of them — is replaced,
    and the token-superset sibling survives."""
    fp = "knowledge/concepts/sample_a.md"
    old = [{"title": "Sample Title", "file_path": fp} for _ in range(120)]
    old += [{"title": "Sample Title", "file_path": fp.replace("/", "\\")} for _ in range(30)]
    sibling = {"title": "Sample Title Extended",
               "file_path": "knowledge/concepts/sample_a_extended.md"}
    coll = _mcp_coll(*old, sibling)
    srv = _patch_server_for_store(monkeypatch, tmp_path, coll)
    monkeypatch.setattr(srv, "Filter", _TokFilter)

    result = _store(srv, title="Sample Title", file_path=fp)
    assert result.get("success") is True
    paths = sorted(str(o.properties.get("file_path")) for o in coll.objects)
    assert paths == sorted([fp, sibling["file_path"]]), paths


# ─── Diagram index delete (same hazard, `<Project>_Diagrams`) ─────────────


class _DiagColl:
    def __init__(self, rows):
        self.rows = {r.uuid: r for r in rows}
        self.query = self
        self.data = self

    def fetch_objects(self, filters=None, limit=100, offset=0, **_kw):
        hits = [r for r in self.rows.values() if filters.matches(r.properties)]
        return SimpleNamespace(objects=hits[offset:offset + limit])

    def delete_by_id(self, uid):
        self.rows.pop(uid, None)


@pytest.mark.parametrize("mutated", [False, True], ids=["shipped", "red-proof"])
def test_diagram_delete_spares_token_superset_diagram(mutate_exact_check, mutated):
    from unittest import mock

    from vco_lib import diagram_indexer as di

    short = ".claude/diagrams/auth.mmd"
    longer = ".claude/diagrams/auth-flow.mmd"
    coll = _DiagColl([
        SimpleNamespace(uuid="a", properties={"file_path": short}),
        SimpleNamespace(uuid="b", properties={"file_path": longer}),
    ])
    client = SimpleNamespace(
        collections=SimpleNamespace(get=lambda _name: coll), close=lambda: None,
    )
    if mutated:
        mutate_exact_check()
    with mock.patch("weaviate.connect_to_custom", return_value=client), \
            mock.patch("weaviate.classes.query.Filter", _TokFilter):
        assert di._weaviate_delete_by_file_path(
            short, weaviate_url="http://localhost:8081",
            collection_name="Proj_Diagrams",
        )
    remaining = sorted(r.properties["file_path"] for r in coll.rows.values())
    assert remaining == ([] if mutated else [longer])
