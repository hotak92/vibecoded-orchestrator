# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.92 — ``--force-rewalk``: bypass the per-FILE gate, keep every per-ENTITY gate.

THE DEFECT THESE PIN
--------------------
``CodeGraphAnalyzer._get_existing_module`` short-circuits a whole file whose
stored module row has the same ``file_hash`` at the current ``embed_revision``.
It runs at the top of every walker on EVERY walk — ``--incremental`` *and*
full. That is right for a source change and exactly wrong for an EXTRACTOR
change: v0.2.92 taught the Python producer to emit ``CodeAPI`` (it emitted
none) and fixed C# route attribution, and neither reaches an existing graph
because the source files did not change. Measured on the real orchestrator
tree: deleting every ``CodeAPI`` row and running a FULL, non-incremental
analyze restored **zero** of them.

``--force-rewalk`` bypasses that one gate and nothing else. The per-entity
content-hash + embed-revision fingerprint (``_dedup_insert`` →
``_resolve_deferred_embed`` → ``guards.classify_row``) still decides every
write and every embed, so an already-converged project pays one cheap
re-extraction pass and re-embeds NOTHING.

Both halves are pinned end to end over a REAL mini-repo with a REAL producer
and the REAL write path, against a fake store that is faithful about the two
things the decision depends on: point-reads by UUID, and ``replace()``
requiring the object to pre-exist.
"""
from __future__ import annotations

import importlib.util
import textwrap
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest

from vco_lib import codegraph_extractor_generation as ceg

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_SRC = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

pytest.importorskip("weaviate")


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_acg_v0292_rewalk",
                                                  str(ANALYZER_SRC))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except SystemExit:  # pragma: no cover
        pytest.fail("analyzer refused to import (weaviate-client / vco_lib missing)")
    return mod


# ---------------------------------------------------------------------------
# A store that is faithful where it matters
# ---------------------------------------------------------------------------
class _Row:
    __slots__ = ("uuid", "properties", "vector", "beacons")

    def __init__(self, uuid: str, properties: Dict[str, Any], vector=None):
        self.uuid = uuid
        self.properties = dict(properties)
        self.vector = vector
        self.beacons: Dict[str, List[str]] = {}


class _View:
    def __init__(self, row: _Row):
        self.uuid = row.uuid
        self.properties = dict(row.properties)
        self.references = None
        self.vector = row.vector


def _matches(row: _Row, flt) -> bool:
    """Evaluate a weaviate ``Filter`` tree. The per-file gate ANDs two
    property equalities, so a fake that ignored filters would answer "this
    file exists, skip it" for every file and make these tests meaningless."""
    if flt is None:
        return True
    subs = getattr(flt, "filters", None)
    if subs is not None:
        op = str(getattr(flt, "operator", "")).lower()
        return (any if "or" in op else all)(_matches(row, s) for s in subs)
    target = getattr(flt, "target", None)
    if target is None:
        return True
    return row.properties.get(target) == getattr(flt, "value", None)


class _Query:
    def __init__(self, coll):
        self._c = coll

    def fetch_object_by_id(self, uuid, return_properties=None, **_kw):
        row = self._c.rows.get(str(uuid))
        return None if row is None else _View(row)

    def fetch_objects(self, filters=None, limit=None, **_kw):
        rows = [r for r in self._c.rows.values() if _matches(r, filters)]
        if limit is not None:
            rows = rows[:limit]
        return types.SimpleNamespace(objects=[_View(r) for r in rows])


class _Data:
    def __init__(self, coll):
        self._c = coll

    def replace(self, uuid, properties, vector=None, references=None):
        u = str(uuid)
        if u not in self._c.rows:
            # The real client's signal; the analyzer falls through to insert().
            raise RuntimeError(f"no object with id '{u}'")
        self._c.stats["replace"] += 1
        self._c.rows[u] = _Row(u, properties, vector)

    def insert(self, uuid=None, properties=None, vector=None, references=None):
        u = str(uuid)
        self._c.stats["insert"] += 1
        self._c.rows[u] = _Row(u, properties or {}, vector)
        return u

    def update(self, uuid, properties):
        u = str(uuid)
        if u in self._c.rows:
            self._c.stats["update"] += 1
            self._c.rows[u].properties.update(properties)

    def delete_by_id(self, uuid):
        self._c.rows.pop(str(uuid), None)

    def delete_many(self, where=None, **_kw):
        return types.SimpleNamespace(successful=0, failed=0, matches=0)

    def reference_add(self, from_uuid, from_property, to):
        row = self._c.rows.get(str(from_uuid))
        if row is not None:
            row.beacons.setdefault(from_property, []).append(str(to))


class _Aggregate:
    def __init__(self, coll):
        self._c = coll

    def over_all(self, total_count=False, filters=None, **_kw):
        rows = [r for r in self._c.rows.values() if _matches(r, filters)]
        return types.SimpleNamespace(total_count=len(rows))


class _Collection:
    def __init__(self, name: str, stats: dict):
        self.name = name
        self.rows: Dict[str, _Row] = {}
        self.stats = stats
        self.query = _Query(self)
        self.data = _Data(self)
        self.aggregate = _Aggregate(self)

    def iterator(self, **_kw):
        for row in list(self.rows.values()):
            yield _View(row)


class _Client:
    def __init__(self, store):
        self.collections = types.SimpleNamespace(
            exists=lambda n: n in store,
            get=lambda n: store[n],
            list_all=lambda simple=True: dict.fromkeys(store),
        )

    def close(self):
        pass


_SLOTS = (
    ("coll_module", "modules_collection", "CodeModule"),
    ("coll_class", "classes_collection", "CodeClass"),
    ("coll_function", "functions_collection", "CodeFunction"),
    ("coll_api", "apis_collection", "CodeAPI"),
    ("coll_interaction", "interactions_collection", "CodeInteraction"),
)


class World:
    """One project's fake code graph + the embed-call counter."""

    def __init__(self, analyzer_mod, project="ReindexProj"):
        self.mod = analyzer_mod
        self.project = project
        self.stats = {"insert": 0, "replace": 0, "update": 0}
        self.store: Dict[str, _Collection] = {}
        self.embeds = 0

    def analyzer(self, *, force_rewalk=False):
        a = self.mod.CodeGraphAnalyzer(self.project)
        for attr, coll_attr, base in _SLOTS:
            name = getattr(a, attr, None) or self.mod._collection_name(
                base, self.project)
            self.store.setdefault(name, _Collection(name, self.stats))
            setattr(a, coll_attr, self.store[name])
        a.client = _Client(self.store)
        a.index_dot_claude = False
        a.force_rewalk = force_rewalk
        return a

    def collection(self, suffix: str) -> _Collection:
        return next(c for n, c in self.store.items() if n.endswith(suffix))

    def reset_counters(self):
        self.embeds = 0
        for k in self.stats:
            self.stats[k] = 0

    def walk(self, repo: Path, *, force_rewalk=False):
        self.reset_counters()
        self.analyzer(force_rewalk=force_rewalk).analyze_repository(
            repo, prune_stale=False, incremental=False)
        return self.embeds


@pytest.fixture
def world(analyzer_mod, monkeypatch) -> World:
    w = World(analyzer_mod)

    def _embed(*_a, **_kw):
        w.embeds += 1
        return [0.0] * 8

    # Late-bound module globals: the analyzer's instance delegators resolve
    # these by bare name at call time, so patching here governs every producer.
    monkeypatch.setattr(analyzer_mod, "generate_embedding", _embed)
    monkeypatch.setattr(analyzer_mod, "embed_function", _embed)
    monkeypatch.setattr(analyzer_mod, "embed_class", _embed)
    return w


# ---------------------------------------------------------------------------
# A tiny but REAL repo: plain functions + a FastAPI-shaped route.
# ---------------------------------------------------------------------------
@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "miniproj"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "plain.py").write_text(textwrap.dedent('''
        """No routes here at all."""

        def add(a, b):
            """Add two numbers."""
            return a + b


        class Calculator:
            """Adds things."""

            def total(self, xs):
                return sum(xs)
    ''').lstrip(), encoding="utf-8")
    (repo / "pkg" / "api.py").write_text(textwrap.dedent('''
        """A module the pre-v0.2.92 Python producer extracted NO CodeAPI from."""
        from fastapi import APIRouter

        router = APIRouter(prefix="/v1")


        @router.get("/items")
        def list_items():
            """Return every item."""
            return []


        @router.post("/items")
        def create_item(payload):
            """Create one item."""
            return payload
    ''').lstrip(), encoding="utf-8")
    return repo


# ===========================================================================
# THE HEADLINE: a plain full walk delivers nothing; --force-rewalk delivers.
# ===========================================================================
def test_python_routes_are_extracted_at_all(world: World, mini_repo: Path):
    """Precondition for everything below: the CURRENT producer emits CodeAPI."""
    world.walk(mini_repo)
    assert len(world.collection("CodeAPI").rows) >= 2, (
        "the v0.2.92 Python producer must emit a CodeAPI row per route"
    )


def test_plain_full_rewalk_does_not_restore_missing_api_rows(
    world: World, mini_repo: Path,
):
    """RED-PROOF for the whole feature.

    Wiping the CodeAPI slice reproduces EXACTLY what every existing install
    looks like today (pre-v0.2.92 Python emitted none). A full, non-incremental
    re-analyze of the unchanged tree restores none of it, because the per-FILE
    gate short-circuits before the producer ever runs. Without ``--force-rewalk``
    an updating user is left in this state with no error anywhere.
    """
    world.walk(mini_repo)
    world.collection("CodeAPI").rows.clear()

    embeds = world.walk(mini_repo)

    assert len(world.collection("CodeAPI").rows) == 0, (
        "a plain full re-walk must be shown NOT to deliver the fix — if this "
        "ever passes by restoring rows, the per-file gate changed and this "
        "feature's premise needs re-deriving"
    )
    assert embeds == 0
    assert world.stats["insert"] == 0


def test_force_rewalk_restores_missing_api_rows(world: World, mini_repo: Path):
    """ACT: the same wipe, with the bypass, delivers the fix."""
    world.walk(mini_repo)
    expected = len(world.collection("CodeAPI").rows)
    world.collection("CodeAPI").rows.clear()

    embeds = world.walk(mini_repo, force_rewalk=True)

    assert len(world.collection("CodeAPI").rows) == expected
    assert world.stats["insert"] == expected
    # Only the missing rows embedded. Nothing else in the tree did.
    assert embeds == expected, (
        f"expected exactly {expected} embeds (the restored API rows), got {embeds}"
    )


def test_force_rewalk_on_a_converged_project_embeds_and_writes_nothing(
    world: World, mini_repo: Path,
):
    """LEAVE-ALONE — the user's actual requirement: "avoid unnecessary
    re-embeddings". Re-extraction is cheap; the per-entity gate keeps the
    expensive half at zero."""
    world.walk(mini_repo)

    embeds = world.walk(mini_repo, force_rewalk=True)

    assert embeds == 0, "a converged project must re-embed NOTHING"
    assert world.stats["insert"] == 0
    assert world.stats["replace"] == 0


def test_force_rewalk_re_embeds_exactly_the_changed_entity(
    world: World, mini_repo: Path,
):
    """The C#-fix shape: an entity whose extracted content CHANGED (here, an
    endpoint string) must re-embed — exactly once, and alone."""
    world.walk(mini_repo)
    api = world.collection("CodeAPI")
    victim = next(iter(api.rows.values()))
    victim.properties["endpoint"] = "items"          # the pre-fix, slash-less form
    victim.properties["content_hash"] = "stale-hash-from-the-old-extractor"

    embeds = world.walk(mini_repo, force_rewalk=True)

    assert embeds == 1
    assert world.stats["replace"] == 1
    assert world.stats["insert"] == 0


def test_force_rewalk_does_not_touch_files_it_cannot_improve(
    world: World, mini_repo: Path,
):
    """The route-free module's rows are byte-identical after the bypass —
    same UUIDs, same content hashes, same vectors."""
    world.walk(mini_repo)
    before = {
        u: (r.properties.get("content_hash"), r.properties.get("embed_revision"))
        for u, r in world.collection("CodeFunction").rows.items()
    }

    world.walk(mini_repo, force_rewalk=True)

    after = {
        u: (r.properties.get("content_hash"), r.properties.get("embed_revision"))
        for u, r in world.collection("CodeFunction").rows.items()
    }
    assert after == before


def test_the_bypass_is_file_gate_only(world: World, mini_repo: Path,
                                      analyzer_mod):
    """Structural pin: ``force_rewalk`` must relax ``_get_existing_module`` and
    NOTHING downstream. If a future edit made it skip the fingerprint gate too,
    every row would re-embed on every update — the outcome the user forbade."""
    a = world.analyzer(force_rewalk=True)
    assert a._get_existing_module("pkg/plain.py", "any-hash") is None

    b = world.analyzer(force_rewalk=False)
    # Without the flag the gate does its normal work (no module row ⇒ None,
    # but via the query path rather than the short-circuit).
    assert b._get_existing_module("pkg/plain.py", "any-hash") is None
    world.walk(mini_repo)
    mod_row = next(iter(world.collection("CodeModule").rows.values()))
    c = world.analyzer(force_rewalk=False)
    assert c._get_existing_module(
        mod_row.properties["path"], mod_row.properties["file_hash"]
    ) is not None, "the ordinary gate must still skip an unchanged file"
    d = world.analyzer(force_rewalk=True)
    assert d._get_existing_module(
        mod_row.properties["path"], mod_row.properties["file_hash"]
    ) is None, "force_rewalk must bypass that same skip"


# ===========================================================================
# Multi-chunk entities: the all-SKIP fast path
# ===========================================================================
def _fp(content_hash, rev, total):
    return {"content_hash": content_hash, "embed_revision": rev,
            "total_chunks": total}


def test_converged_multichunk_entity_skips_without_embedding_or_patching(
    world: World, analyzer_mod,
):
    """RED-PROOF for the amplification fix.

    ``guards.all_chunks_stampable`` returns True only for STAMP candidates
    (``floor <= rev < current``). A FULLY CONVERGED multi-chunk entity is SKIP,
    so the precheck returned False and the caller re-embedded every chunk of
    every over-budget Function/Class. Normally the per-file gate hides it;
    ``--force-rewalk`` would fire it on every file (measured: 109 wasted
    embeds — 14.2% of a converged 766-entity tree — with zero resulting
    writes; 1,229 on the orchestrator repo).
    """
    a = world.analyzer()
    a._track_visited = True          # what `--prune-stale` turns on
    coll = world.collection("CodeFunction")
    uuids = ["u0", "u1", "u2"]
    hashes = ["h0", "h1", "h2"]
    for u, h in zip(uuids, hashes):
        coll.rows[u] = _Row(u, {"content_hash": h, "total_chunks": 3,
                                "embed_revision":
                                    analyzer_mod.CODEGRAPH_EMBED_REVISION})

    assert a._maybe_stamp_all_chunks(coll, uuids, hashes, 3) is True
    assert world.stats["update"] == 0, "an all-SKIP entity needs no revision patch"
    # And every chunk is recorded visited, so a concurrent --prune-stale
    # cannot delete these live rows just because the walk skipped them.
    assert {u for _name, u in a.visited_uuids} >= set(uuids)


def test_multichunk_entity_with_a_changed_chunk_still_re_embeds(
    world: World, analyzer_mod,
):
    """LEAVE-ALONE's mirror: any non-SKIP chunk falls through to the embed."""
    a = world.analyzer()
    coll = world.collection("CodeFunction")
    uuids = ["u0", "u1", "u2"]
    for u, h in zip(uuids, ["h0", "CHANGED", "h2"]):
        coll.rows[u] = _Row(u, {"content_hash": h, "total_chunks": 3,
                                "embed_revision":
                                    analyzer_mod.CODEGRAPH_EMBED_REVISION})

    assert a._maybe_stamp_all_chunks(coll, uuids, ["h0", "h1", "h2"], 3) is False


def test_multichunk_entity_with_a_missing_chunk_re_embeds(
    world: World, analyzer_mod,
):
    """An absent chunk row is UNKNOWN, and unknown must never SKIP."""
    a = world.analyzer()
    coll = world.collection("CodeFunction")
    coll.rows["u0"] = _Row("u0", {"content_hash": "h0", "total_chunks": 3,
                                  "embed_revision":
                                      analyzer_mod.CODEGRAPH_EMBED_REVISION})
    assert a._maybe_stamp_all_chunks(
        coll, ["u0", "u1", "u2"], ["h0", "h1", "h2"], 3) is False


def test_multichunk_entity_at_a_stale_revision_still_takes_the_stamp_path(
    world: World, analyzer_mod, monkeypatch,
):
    """The v0.2.82 STAMP fast path must survive the new SKIP branch: a
    content-identical entity whose revision is old-but-compatible is PATCHED,
    not re-embedded."""
    monkeypatch.setattr(analyzer_mod, "CODEGRAPH_EMBED_REVISION", 2)
    monkeypatch.setattr(analyzer_mod, "_EMBED_SPACE_COMPATIBLE_FROM_REVISION", 1)
    a = world.analyzer()
    coll = world.collection("CodeFunction")
    uuids = ["u0", "u1"]
    for u, h in zip(uuids, ["h0", "h1"]):
        coll.rows[u] = _Row(u, {"content_hash": h, "total_chunks": 2,
                                "embed_revision": 1})

    assert a._maybe_stamp_all_chunks(coll, uuids, ["h0", "h1"], 2) is True
    assert world.stats["update"] == 2, "each chunk's revision must be patched"
    for u in uuids:
        assert coll.rows[u].properties["embed_revision"] == 2


# ===========================================================================
# --force-rewalk resolution + the completion stamp
# ===========================================================================
@pytest.mark.parametrize(
    "cli,env,expected",
    [
        (True, None, True),
        (False, None, False),
        (False, "1", True),
        (False, "true", True),
        (False, "YES", True),
        (False, "on", True),
        (False, "0", False),
        (False, "", False),
        (False, "no", False),
        (True, "0", True),          # explicit flag wins over a falsy env
    ],
)
def test_force_rewalk_resolution(monkeypatch, cli, env, expected):
    """ONE resolver, so the CLI flag and its env transport cannot disagree.
    The env exists because the background trigger reaches the analyzer across
    two spawn hops whose argv it does not build."""
    if env is None:
        monkeypatch.delenv("VCT_CODEGRAPH_FORCE_REWALK", raising=False)
    else:
        monkeypatch.setenv("VCT_CODEGRAPH_FORCE_REWALK", env)
    assert ceg.resolve_force_rewalk(cli) is expected


def test_force_rewalk_flag_is_on_the_cli(analyzer_mod):
    parsed = analyzer_mod._build_arg_parser().parse_args(
        ["/tmp/x", "--force-rewalk"])
    assert parsed.force_rewalk is True
    assert analyzer_mod._build_arg_parser().parse_args(
        ["/tmp/x"]).force_rewalk is False


@pytest.mark.parametrize(
    "kwargs,expected,why",
    [
        (dict(), True, "a clean whole-repo force walk certifies"),
        (dict(force_rewalk=False), False, "no bypass ⇒ unchanged files were skipped"),
        (dict(only_file=Path("a.py")), False, "single-file walk covers one file"),
        (dict(only_files_from=Path("q.txt")), False, "batch walk covers a subset"),
        (dict(language="python"), False, "language-scoped walk covers one language"),
        (dict(insert_errors=1), False, "a failed insert may be a missing entity"),
        (dict(files_analyzed=0), False, "nothing was walked"),
    ],
)
def test_only_a_clean_whole_repo_force_walk_certifies(kwargs, expected, why):
    base = dict(only_file=None, only_files_from=None, language=None,
                insert_errors=0, files_analyzed=12)
    force = kwargs.pop("force_rewalk", True)
    base.update(kwargs)
    assert ceg.walk_certifies_generation(force, **base) is expected, why


def test_an_interrupted_walk_leaves_no_stamp_so_the_work_stays_owed(
    tmp_path: Path,
):
    """The resumability contract.

    The stamp is written only AFTER the walk returns clean. A crash mid-walk
    therefore leaves the stamp absent, ``decide`` reads absent as OWED, and the
    next update re-triggers — the walk itself being idempotent (converged rows
    hash-skip), the retry finishes what the first attempt started.
    """
    # An interrupted run: force_rewalk was on, but insert errors were recorded.
    assert ceg.stamp_after_walk(
        tmp_path, force_rewalk=True, insert_errors=3, files_analyzed=40) is None
    assert ceg.read_stamp_generation(tmp_path) is None
    assert ceg.decide(prev_version="0.2.91", running_version="0.2.92",
                      stamp_generation=None,
                      graph_exists=True).needs_reindex is True

    # The completing retry stamps it, and the project stops being owed.
    assert ceg.stamp_after_walk(
        tmp_path, force_rewalk=True, insert_errors=0, files_analyzed=40
    ) == ceg.CURRENT_EXTRACTOR_GENERATION
    assert ceg.read_stamp_generation(tmp_path) == ceg.CURRENT_EXTRACTOR_GENERATION
    assert ceg.decide(prev_version="0.2.91", running_version="0.2.92",
                      stamp_generation=ceg.read_stamp_generation(tmp_path),
                      graph_exists=True).needs_reindex is False


def test_stamp_write_failure_never_breaks_the_build(tmp_path: Path, monkeypatch):
    """A stamp is best-effort: it may never raise into the analyzer's tail."""
    def _boom(*_a, **_kw):
        raise OSError("nope")

    monkeypatch.setattr(ceg, "write_stamp", _boom)
    assert ceg.stamp_after_walk(
        tmp_path, force_rewalk=True, insert_errors=0, files_analyzed=4) is None


def test_the_analyzer_only_keeps_io_seams(analyzer_mod):
    """Modularity pin (``tests/test_analyze_code_graph_ratchet.py`` is a
    downward-only ratchet on this 7k-line template script): the DECISIONS live
    in ``vco_lib``; the analyzer holds the flag, the gate bypass and the calls.
    """
    for gone in ("_resolve_force_rewalk", "force_rewalk_certifies_generation",
                 "_stamp_generation_after_force_rewalk"):
        assert not hasattr(analyzer_mod, gone), (
            f"{gone} belongs in vco_lib.codegraph_extractor_generation"
        )
    assert analyzer_mod._extractor_gen is ceg
