# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.91 (WP-C): per-file ENTITY reconcile — the entity-orphan convergence fix.

The bug these tests pin
-----------------------
Code-graph row UUIDs are deterministic on
``project::project_source::file_path::full_name``. Ordinary refactoring — the
live case: ``install.select_summary_backend`` extracted into
``vco_lib/embedding_selection.py`` — makes an ENTITY vanish while its FILE
survives. The re-walk of that file upserts only the entities that still exist,
so the vanished entity's row is never re-written (never re-stamped to the
current ``embed_revision``), the D1 orphan-clear does not touch it (its file
still exists on disk), and ``--prune-stale`` is deliberately never forwarded by
the resync driver. Result: the R-6 owed-probe has a permanent positive floor,
the ``codegraph_embed_resync_pending`` ledger entry can NEVER clear, and every
``install.py --update`` re-spawns a full background resync walk. Measured live
floor on the 2026-08-23 investigation machine: 12 rows, unchanged for a month.

DESTRUCTIVE BRANCH ⇒ BOTH SIDES ARE MANDATORY. Every test below is either an
ACT case (the row that MUST be deleted) or a LEAVE-ALONE case (a row that must
survive), and the leave-alone cases carry one assertion each:

  * unvisited files (a file this walk never touched);
  * ``--extra-path`` clone rows (a different ``project_source``) — in BOTH
    directions (a primary walk must not reap them; an extra walk must not reap
    primary/legacy rows);
  * rows carrying a different ``project`` identity (the identity sweep's job);
  * same-file entities that are STILL present;
  * a file whose walk FAILED part-way (partial walk ⇒ never authorise a delete);
  * an unresolvable primary-source set (fail-open ⇒ delete nothing).

Everything is hermetic: store-backed fake collections, stubbed embeds, no
Weaviate. The convergence assertions run the REAL ``count_stale_rows`` owed
probe against the same fake store the walk wrote to, so "the owed floor reaches
zero" is proven end-to-end rather than asserted by hand.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import vco_lib.codegraph_resync as cr

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

PROJECT = "ReconcileProj"


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_wpc_analyze_code_graph", str(_ANALYZER_PATH)
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─────────────────────────── store-backed fakes ────────────────────────────


class _Obj:
    def __init__(self, uuid: str, properties: Dict[str, Any]) -> None:
        self.uuid = uuid
        self.properties = dict(properties)


class _Data:
    def __init__(self, coll: "_Coll") -> None:
        self._c = coll

    def replace(self, uuid: str, **kw: Any) -> None:
        self._c.store[str(uuid)] = dict(kw.get("properties") or {})
        return None

    def insert(self, uuid: str, **kw: Any) -> str:
        self._c.store[str(uuid)] = dict(kw.get("properties") or {})
        return str(uuid)

    def update(self, uuid: str, **kw: Any) -> None:
        self._c.store.setdefault(str(uuid), {}).update(kw.get("properties") or {})
        return None

    def delete_by_id(self, uuid: str) -> None:
        if str(uuid) in self._c.fail_deletes:
            raise RuntimeError(f"simulated delete failure for {uuid}")
        self._c.deleted.append(str(uuid))
        self._c.store.pop(str(uuid), None)


class _Coll:
    """Collection stand-in with a real backing store.

    ``.query`` is deliberately ABSENT: the per-object content-hash point-read
    then falls through to a write (its documented fail-safe), and the WP-C
    narrowed read falls back to the full scan — the default path most tests
    want. ``_QueryColl`` below opts into the narrowed read.
    """

    def __init__(self, name: str, path_prop: str, extra_props=()) -> None:
        self.name = name
        self.path_prop = path_prop
        self.store: Dict[str, Dict[str, Any]] = {}
        self.deleted: List[str] = []
        self.fail_deletes: set = set()
        self.data = _Data(self)
        self.iter_calls = 0
        names = [path_prop, "project", "project_source", "embed_revision",
                 "content_hash", *extra_props]
        self.config = types.SimpleNamespace(
            get=lambda: types.SimpleNamespace(
                properties=[types.SimpleNamespace(name=n) for n in names]
            )
        )

    def iterator(self, return_properties=None):
        self.iter_calls += 1
        return [_Obj(u, p) for u, p in list(self.store.items())]

    def seed(self, uuid: str, **props: Any) -> str:
        self.store[str(uuid)] = dict(props)
        return str(uuid)


def _wire(analyzer_mod: types.ModuleType, project: str = PROJECT):
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = object()
    inst.module_cache = {}
    inst.class_cache = {}
    inst.function_cache = {}
    inst.module_imports = {}
    inst.visited_uuids = set()
    inst._track_visited = False
    inst._current_language = ""
    inst._current_source = ""
    inst._progress_emitter = None
    inst._cfg_pdg_data = {}
    inst.index_dot_claude = False
    inst.modules_collection = _Coll(f"{project}_CodeModule", "path")
    inst.classes_collection = _Coll(
        f"{project}_CodeClass", "file_path", ("full_name",)
    )
    inst.functions_collection = _Coll(
        f"{project}_CodeFunction", "file_path", ("full_name",)
    )
    inst.apis_collection = _Coll(f"{project}_CodeAPI", "file_path", ("full_name",))
    inst.interactions_collection = _Coll(
        f"{project}_CodeInteraction", "file_path", ("full_name",)
    )
    return inst


#: A non-empty stub vector. It matters that this is TRUTHY: a vectorless write
#: is stamped ``embed_revision = 0`` by design (R-2), which would leave every
#: freshly-written row "owed" and mask the convergence assertions below.
_STUB_VEC = [0.1] * 8


def _stub_embeddings(analyzer_mod: types.ModuleType, monkeypatch) -> None:
    monkeypatch.setattr(analyzer_mod, "generate_embedding", lambda text: list(_STUB_VEC))
    monkeypatch.setattr(analyzer_mod, "embed_module", lambda summary: list(_STUB_VEC))
    monkeypatch.setattr(
        analyzer_mod, "embed_function",
        lambda sig, body, language="python": list(_STUB_VEC),
    )
    monkeypatch.setattr(
        analyzer_mod, "embed_class",
        lambda sig, body, methods=None, language="python": list(_STUB_VEC),
    )


def _uuid_for(analyzer_mod, rel: str, full_name: str, source: str) -> str:
    """The row identity the analyzer itself would mint (same seed)."""
    return analyzer_mod._deterministic_uuid(
        PROJECT, rel, full_name, project_source=source
    )


def _seed_stale_function(
    analyzer, analyzer_mod, rel: str, full_name: str, source: str,
    project: str = PROJECT, embed_revision=None,
) -> str:
    uid = analyzer_mod._deterministic_uuid(
        project, rel, full_name, project_source=source
    )
    return analyzer.functions_collection.seed(
        uid, file_path=rel, project=project, project_source=source,
        full_name=full_name, embed_revision=embed_revision,
    )


def _fake_client(colls):
    by_name = {c.name: c for c in colls}
    return types.SimpleNamespace(
        collections=types.SimpleNamespace(
            exists=lambda n: n in by_name,
            get=lambda n: by_name[n],
        ),
        close=lambda: None,
    )


#: Captured at import so a test that monkeypatches ``cr.count_stale_rows``
#: (the driver seam) can still call the REAL probe from inside its stub.
_REAL_COUNT_STALE_ROWS = cr.count_stale_rows


def _owed_counts(analyzer, repo: Path) -> Optional[dict]:
    """The REAL R-6 owed probe, run against the walk's own fake store."""
    return _REAL_COUNT_STALE_ROWS(
        PROJECT,
        current_revision=1,
        repo_root=repo,
        index_dot_claude=False,
        client=_fake_client([
            analyzer.modules_collection,
            analyzer.classes_collection,
            analyzer.functions_collection,
        ]),
    )


def _make_repo(tmp_path: Path) -> Path:
    """A one-file repo whose surviving file is the refactor victim's home."""
    (tmp_path / "install.py").write_text(
        "def main():\n    return 1\n", encoding="utf-8"
    )
    return tmp_path


# ══════════════════════════════ ACT side ═══════════════════════════════════


def test_act_entity_extracted_to_another_module_is_deleted(
    analyzer_mod, tmp_path, monkeypatch
):
    """The live failure class, reproduced end-to-end.

    ``install.select_summary_backend`` was extracted into
    ``vco_lib/embedding_selection.py`` in v0.2.68; ``install.py`` still exists,
    so nothing ever re-writes or deletes its row. After the walk the row MUST be
    gone and the owed probe MUST read zero.

    RED-PROOF: on bd8f6836 the walk leaves the row untouched (no reconcile
    exists) — the delete assertion and the owed==0 assertion both fail.
    """
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    src = repo.as_posix()
    analyzer = _wire(analyzer_mod)
    stale = _seed_stale_function(
        analyzer, analyzer_mod, "install.py",
        "install.select_summary_backend", src,
    )

    assert (_owed_counts(analyzer, repo) or {}).get(
        f"{PROJECT}_CodeFunction"
    ) == 1, "pre-condition: the extracted entity's row is owed"

    analyzer.analyze_repository(repo)

    assert stale in analyzer.functions_collection.deleted, (
        "the row of an entity refactored OUT of a surviving file must be deleted"
    )
    counts = _owed_counts(analyzer, repo)
    assert counts is not None and sum(counts.values()) == 0, (
        f"the owed floor must reach zero after the reconciling walk: {counts}"
    )


def test_act_old_identity_duplicate_row_is_deleted(
    analyzer_mod, tmp_path, monkeypatch
):
    """Decision #11: a duplicate row for a STILL-PRESENT entity, seeded under a
    legacy (empty) ``project_source`` identity, is deleted by the same pass —
    it serves stale search results and no walk can ever re-stamp it."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    legacy = _seed_stale_function(
        analyzer, analyzer_mod, "install.py", "install.main", source="",
    )
    # The legacy row is stamped with the CURRENT source (the v0.2.47 backfill
    # does exactly this) while its UUID keeps the pre-v0.2.52 seed.
    analyzer.functions_collection.store[legacy]["project_source"] = repo.as_posix()

    analyzer.analyze_repository(repo)

    assert legacy in analyzer.functions_collection.deleted
    live = _uuid_for(analyzer_mod, "install.py", "install.main", repo.as_posix())
    assert live in analyzer.functions_collection.store, (
        "the identity the walk actually wrote must survive"
    )


def test_act_stale_module_row_for_walked_file_is_deleted(
    analyzer_mod, tmp_path, monkeypatch
):
    """The live CodeModule case (``install.sh`` held 8+ rows): a second module
    row anchored to a walked file, minted under a stale identity, is reaped."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    ghost = analyzer.modules_collection.seed(
        "ghost-module-row", path="install.py", project=PROJECT,
        project_source=repo.as_posix(), embed_revision=None,
    )

    analyzer.analyze_repository(repo)

    assert ghost in analyzer.modules_collection.deleted
    live = _uuid_for(
        analyzer_mod, "install.py", "module::install.py", repo.as_posix()
    )
    assert live in analyzer.modules_collection.store


# ═════════════════════════ LEAVE-ALONE side ════════════════════════════════


def test_leave_alone_rows_of_unvisited_files(analyzer_mod, tmp_path, monkeypatch):
    """A single-file walk touches ONLY the named file; rows anchored to any
    other file are out of scope BY CONSTRUCTION."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    (repo / "helper.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    analyzer = _wire(analyzer_mod)
    untouched = _seed_stale_function(
        analyzer, analyzer_mod, "helper.py", "helper.gone", repo.as_posix(),
    )

    analyzer.analyze_repository(repo, only_file=repo / "install.py")

    assert untouched not in analyzer.functions_collection.deleted, (
        "a file this walk never visited must not have its rows judged"
    )


def test_leave_alone_extra_path_clone_rows(analyzer_mod, tmp_path, monkeypatch):
    """B1 tenant isolation: a row stamped with a DIFFERENT source root is an
    ``--extra-path`` clone. A primary walk can neither re-stamp nor judge it."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    clone = _seed_stale_function(
        analyzer, analyzer_mod, "install.py", "install.select_summary_backend",
        source="/some/other/clone",
    )

    analyzer.analyze_repository(repo)

    assert clone not in analyzer.functions_collection.deleted, (
        "extra-path rows converge on their own root's walk — never touch them"
    )


def test_leave_alone_primary_rows_during_an_extra_path_walk(analyzer_mod):
    """The inverse direction, at the engine level: a walk of an EXTRA root must
    not reap primary / legacy-empty rows anchored to the same relative path."""
    primary_sources = {"/repo"}
    coll = _Coll(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    legacy = coll.seed(
        "legacy", file_path="src/x.py", project=PROJECT, project_source="",
        full_name="x.gone", embed_revision=None,
    )
    primary = coll.seed(
        "primary", file_path="src/x.py", project=PROJECT,
        project_source="/repo", full_name="x.gone", embed_revision=None,
    )
    extra_orphan = coll.seed(
        "extra", file_path="src/x.py", project=PROJECT,
        project_source="/extra", full_name="x.gone", embed_revision=None,
    )

    deleted, failures = cr.reconcile_walked_file_rows(
        ((coll, "file_path"),),
        {("/extra", "src/x.py"): {coll.name: {"extra-live"}}},
        project_name=PROJECT,
        primary_sources=primary_sources,
        deleter=cr.delete_file_rows_exact,
    )

    assert failures == 0
    assert legacy not in coll.deleted, "legacy/empty-source rows are PRIMARY"
    assert primary not in coll.deleted, "primary rows belong to the primary walk"
    assert extra_orphan in coll.deleted, (
        "the extra walk still reconciles its OWN tenant's rows"
    )
    assert deleted == 1


def test_leave_alone_rows_of_another_project_identity(
    analyzer_mod, tmp_path, monkeypatch
):
    """A row carrying a different ``project`` value belongs to the identity
    sweep (v0.2.84 D4/P1), not to this pass."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    other = _seed_stale_function(
        analyzer, analyzer_mod, "install.py", "install.gone", repo.as_posix(),
        project="SomeOtherIdentity",
    )

    analyzer.analyze_repository(repo)

    assert other not in analyzer.functions_collection.deleted


def test_leave_alone_same_file_entities_that_are_still_present(
    analyzer_mod, tmp_path, monkeypatch
):
    """The entity the walk DID write survives — including on the cheap path
    where the per-object content-hash skip suppressed the write."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)

    analyzer.analyze_repository(repo)
    live = _uuid_for(analyzer_mod, "install.py", "install.main", repo.as_posix())
    assert live in analyzer.functions_collection.store
    assert live not in analyzer.functions_collection.deleted

    # Second walk over the unchanged repo: nothing may be deleted, and the row
    # must still be there (the file re-walks only if a stale row anchors it).
    analyzer.analyze_repository(repo)
    assert analyzer.functions_collection.deleted == []
    assert live in analyzer.functions_collection.store


def test_leave_alone_when_a_files_walk_failed_part_way(
    analyzer_mod, tmp_path, monkeypatch
):
    """A per-file write failure leaves the reconcile scope UNCOMMITTED, so the
    partially-written file's surviving rows are never judged."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    other = _seed_stale_function(
        analyzer, analyzer_mod, "install.py", "install.select_summary_backend",
        repo.as_posix(),
    )

    real_store_entity = analyzer.store_entity

    def _boom(entity):
        if getattr(entity, "kind", "") == "function":
            raise analyzer_mod._DedupInsertError(
                RuntimeError("simulated"), analyzer.functions_collection.name, "u"
            )
        return real_store_entity(entity)

    monkeypatch.setattr(analyzer, "store_entity", _boom)

    stats = analyzer.analyze_repository(repo)

    assert stats["insert_errors"] >= 1, "pre-condition: the file's walk failed"
    assert other not in analyzer.functions_collection.deleted, (
        "a partially-walked file must never authorise a delete"
    )


def test_leave_alone_worktree_scoped_walk_never_deletes_canonical_rows(
    analyzer_mod, tmp_path, monkeypatch
):
    """The per-edit hook analyses a git WORKTREE file while stamping the
    CANONICAL main-repo root (``--canonical-source``, v0.2.66 Bug 3) so both
    checkouts converge on one row. That walk read a DIFFERENT tree than the
    tenant it stamps — the worktree's branch may legitimately lack an entity the
    canonical checkout still has — so it must never authorise a delete."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "install.py").write_text(
        "def main():\n    return 1\n", encoding="utf-8"
    )
    canonical = (tmp_path / "main-checkout").as_posix()
    analyzer = _wire(analyzer_mod)
    canonical_row = _seed_stale_function(
        analyzer, analyzer_mod, "install.py",
        "install.only_on_the_main_branch", canonical,
    )

    analyzer.analyze_repository(
        worktree, only_file=worktree / "install.py", canonical_source=canonical,
    )

    assert canonical_row not in analyzer.functions_collection.deleted, (
        "a walk that stamped a root whose files it did not read must not delete"
    )

    # Non-vacuity: the SAME evidence, minus the walked-sources guard, WOULD
    # have deleted the canonical row — so the assertion above is pinning the
    # guard, not an accident of the fixture.
    unguarded = _Coll(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    row = unguarded.seed(
        "canon", file_path="install.py", project=PROJECT,
        project_source=canonical, full_name="install.only_on_the_main_branch",
        embed_revision=None,
    )
    cr.reconcile_walked_file_rows(
        ((unguarded, "file_path"),),
        {(canonical, "install.py"): {unguarded.name: {"live"}}},
        project_name=PROJECT,
        primary_sources={worktree.as_posix()},
        deleter=cr.delete_file_rows_exact,  # no walked_sources ⇒ no guard
    )
    assert row in unguarded.deleted


def test_leave_alone_when_primary_sources_unresolvable(analyzer_mod):
    """Fail-open: no positively-resolved primary root ⇒ tenancy is unknowable
    ⇒ delete nothing (same rule as the D1 orphan-clear)."""
    coll = _Coll(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    coll.seed(
        "row", file_path="src/x.py", project=PROJECT, project_source="",
        full_name="x.gone", embed_revision=None,
    )

    deleted, failures = cr.reconcile_walked_file_rows(
        ((coll, "file_path"),),
        {("", "src/x.py"): {coll.name: set()}},
        project_name=PROJECT,
        primary_sources=None,
        deleter=cr.delete_file_rows_exact,
    )

    assert (deleted, failures) == (0, 0)
    assert coll.deleted == []


def test_leave_alone_pathless_and_other_collections(analyzer_mod):
    """A pathless row (the classifier's own ``purgeable`` class, owned by the
    orphan-clear) is not matched here — the reconcile keys on an exact path."""
    coll = _Coll(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    coll.seed(
        "pathless", file_path="", project=PROJECT, project_source="",
        full_name="ghost", embed_revision=None,
    )

    deleted, _ = cr.reconcile_walked_file_rows(
        ((coll, "file_path"),),
        {("", "src/x.py"): {coll.name: set()}},
        project_name=PROJECT,
        primary_sources={"/repo"},
        deleter=cr.delete_file_rows_exact,
    )

    assert deleted == 0 and coll.deleted == []


# ═════════════════════════ audit trail (decision #11) ══════════════════════


def test_deleted_identities_are_audited(analyzer_mod, tmp_path, monkeypatch, capsys):
    """Every deleted identity is logged (UUID + full_name + path) and the run
    leaves a durable ``auto-resolutions.jsonl`` row."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    stale = _seed_stale_function(
        analyzer, analyzer_mod, "install.py",
        "install.select_summary_backend", repo.as_posix(),
    )

    analyzer.analyze_repository(repo)
    out = capsys.readouterr().out

    assert stale in out and "install.select_summary_backend" in out
    assert "install.py" in out

    trail = repo / ".claude" / "logs" / "auto-resolutions.jsonl"
    assert trail.is_file(), "the destructive branch must leave a durable trail"
    rows = [json.loads(line) for line in trail.read_text().splitlines() if line]
    hit = [r for r in rows
           if r.get("condition_id") == "codegraph_entity_rows_reconciled"]
    assert hit, rows
    assert stale in hit[-1]["detail"]


def test_delete_failures_flow_into_the_partial_status_chain(
    analyzer_mod, tmp_path, monkeypatch
):
    """A failed delete must surface as ``prune_failures`` (success→partial),
    never as a clean success with the row silently surviving."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    stale = _seed_stale_function(
        analyzer, analyzer_mod, "install.py",
        "install.select_summary_backend", repo.as_posix(),
    )
    analyzer.functions_collection.fail_deletes.add(stale)

    stats = analyzer.analyze_repository(repo)

    assert stats["prune_failures"] >= 1
    assert stale in analyzer.functions_collection.store


def test_an_already_gone_row_is_not_counted_as_a_delete_failure(analyzer_mod):
    """v0.2.91 fix-round NIT-3 — idempotent delete.

    The narrowed candidate read pages with ``offset``. Under concurrent mutation
    (a per-edit hook drain running while a resync walk reconciles) the same row
    can be listed twice, or vanish between the read and the delete. Counting
    "it is already gone" as a FAILURE propagates ``success -> partial`` through
    ``_prune_failures``, which defers the revision advance and re-spawns the
    whole background resync next update — a self-perpetuating partial produced
    by an operation that achieved exactly what it set out to achieve.

    RED-PROOF: pre-fix the bare ``except Exception`` counted every raise, so
    ``failures`` is 1 here and the assertion below fails.
    """
    coll = _Coll(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    gone = coll.seed(
        "already-gone", file_path="install.py", project=PROJECT,
        project_source="/repo", full_name="install.vanished", embed_revision=None,
    )

    class _NotFound(Exception):
        pass

    def _raise_not_found(uuid: str) -> None:
        raise _NotFound(f"object with id {uuid} not found")

    coll.data.delete_by_id = _raise_not_found  # type: ignore[assignment]

    deleted, failures = cr.delete_file_rows_exact(
        coll, "file_path", lambda raw, props: raw == "install.py",
        project=PROJECT, log_prefix="test",
    )

    assert failures == 0, (
        "an already-gone row satisfies the delete's post-condition; counting it "
        "as a failure manufactures a permanent 'partial' run"
    )
    assert deleted == 0, "nothing was actually removed by US"
    assert gone in coll.store, "the fake store is untouched — only the raise matters"


def test_an_unknown_delete_error_is_still_counted_as_a_failure(analyzer_mod):
    """Both-sides leg for NIT-3: only an EXPLICIT not-found signal is absolved.

    A permission error / 500 / timeout leaves the row's fate UNKNOWN, so the
    post-condition really is unproven and the caller must still flip to partial.
    """
    coll = _Coll(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    coll.seed(
        "boom", file_path="install.py", project=PROJECT,
        project_source="/repo", full_name="install.vanished", embed_revision=None,
    )
    coll.fail_deletes.add("boom")  # raises RuntimeError("simulated delete failure")

    deleted, failures = cr.delete_file_rows_exact(
        coll, "file_path", lambda raw, props: raw == "install.py",
        project=PROJECT, log_prefix="test",
    )

    assert (deleted, failures) == (0, 1)


def test_already_gone_classifier_covers_the_client_shapes():
    """The classifier is deliberately NARROW — pin both sides explicitly."""
    class _Status(Exception):
        status_code = 404

    assert cr._delete_is_already_gone(_Status("nope"))
    assert cr._delete_is_already_gone(RuntimeError("no object with id abc"))
    assert cr._delete_is_already_gone(RuntimeError("Object was not found"))
    assert cr._delete_is_already_gone(RuntimeError("uuid abc does not exist"))
    assert not cr._delete_is_already_gone(RuntimeError("permission denied"))
    assert not cr._delete_is_already_gone(TimeoutError("deadline exceeded"))
    # The one that matters most: Weaviate's live 500 signature carries the
    # words "not found" but is a SCHEMA-property failure, not a missing row.
    # `tests/test_codegraph_prune_failure_status.py` pins that it still reaches
    # the prune-failure chain — and it caught this exact over-broad match
    # during the fix round, which is why the subject check exists.
    assert not cr._delete_is_already_gone(
        RuntimeError('500 "subtract prop lengths: property not found"')
    )
    assert not cr._delete_is_already_gone(RuntimeError("class not found"))


def test_a_carried_status_code_is_authoritative_over_prose():
    """Re-review MINOR-A: the live v4 client raises delete errors with the
    boilerplate prefix "Object could not be deleted." + the response body, so
    every live REST delete error contains the word "object". A 5xx in that
    REAL raised shape must never be absolved by prose matching — when the
    exception carries a status code, the status code decides."""
    class _LiveShape(Exception):
        def __init__(self, status_code: int, message: str):
            super().__init__(message)
            self.status_code = status_code

    live_500 = _LiveShape(
        500,
        "Object could not be deleted.! Unexpected status code: 500, with "
        'response body: {"error":[{"message":"subtract prop lengths: '
        'property not found"}]}',
    )
    assert not cr._delete_is_already_gone(live_500)
    # Even an unambiguous already-gone PHRASE loses to a non-404 status code:
    # the transport said 500, so the row's fate is unproven.
    assert not cr._delete_is_already_gone(
        _LiveShape(500, "Object could not be deleted.! no object with id abc")
    )
    # And the carried 404 stays authoritative regardless of the prose.
    assert cr._delete_is_already_gone(
        _LiveShape(404, "Object could not be deleted.! anything at all")
    )


# ═══════════════ multi-pass commit hazard (fix-round MINOR-5) ══════════════


def _two_pass_extraction(analyzer_mod, rel: str, full_names):
    """A FileExtraction carrying ``full_names`` as top-level functions."""
    from datetime import datetime, timezone

    from vco_lib.codegraph_entities import (
        CodeEntity,
        FileExtraction,
        KIND_FUNCTION,
        ModuleDescriptor,
    )

    entities = [
        CodeEntity(
            kind=KIND_FUNCTION, file_path_rel=rel, name=fn.rsplit(".", 1)[-1],
            full_name=fn, body=f"function {fn}() {{}}", signature=f"{fn}()",
            doc="", start_line=1, end_line=2, project=PROJECT,
            extras={"type_uses": []},
        )
        for fn in full_names
    ]
    return FileExtraction(
        module=ModuleDescriptor(
            path=rel, language="JavaScript", loc=2, complexity=1.0,
            last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
            file_hash="deadbeef", imports=[], module_summary=f"Module: {rel}",
        ),
        entities=entities,
        stats={"modules": 1, "classes": 0, "functions": len(entities)},
    )


def test_a_failed_second_pass_revokes_the_first_passs_authorisation(
    analyzer_mod, monkeypatch
):
    """v0.2.91 fix-round MINOR-5 — RED-PROOF.

    The reconcile scope is committed per ``write_file_extraction`` CALL and
    merged by ``(project_source, path)``, but a file can be walked by TWO passes
    (a ``.svelte`` file goes through the svelte pass and the javascript pass;
    ``--extra-path`` roots re-walk shared paths). Pre-fix, pass A's commit left
    the file AUTHORISED while pass B died mid-write — so pass B's valid rows
    were absent from the keep-set and the reconcile deleted them. Self-healing
    on the next full walk, but a breach of the invariant the whole design rests
    on: **a partial walk never authorises a delete.**

    Pre-fix this test fails on the final assertion (the key is still present in
    ``_reconcile_walked``, carrying only pass A's UUIDs).
    """
    _stub_embeddings(analyzer_mod, monkeypatch)
    analyzer = _wire(analyzer_mod)
    analyzer._reconcile_walked = {}
    analyzer._reconcile_pending = None
    analyzer._current_source = "/repo"
    rel = "app.svelte"
    key = ("/repo", rel)

    # Pass A succeeds and authorises the file.
    analyzer.write_file_extraction(_two_pass_extraction(analyzer_mod, rel, ["a.one"]))
    assert key in analyzer._reconcile_walked, "pre-condition: pass A committed"
    pass_a_uuids = {
        u for uids in analyzer._reconcile_walked[key].values() for u in uids
    }
    assert pass_a_uuids

    # Pass B dies mid-file.
    real_store_entity = analyzer.store_entity

    def _boom(entity):
        raise analyzer_mod._DedupInsertError(
            RuntimeError("simulated"), analyzer.functions_collection.name, "u"
        )

    monkeypatch.setattr(analyzer, "store_entity", _boom)
    with pytest.raises(analyzer_mod._DedupInsertError):
        analyzer.write_file_extraction(
            _two_pass_extraction(analyzer_mod, rel, ["b.two"])
        )
    monkeypatch.setattr(analyzer, "store_entity", real_store_entity)

    assert key not in analyzer._reconcile_walked, (
        "a file whose LAST pass failed must not stay authorised by an EARLIER "
        "pass's commit — pass B's rows are missing from the keep-set, so the "
        "reconcile would delete them"
    )


def test_two_successful_passes_still_keep_the_union(analyzer_mod, monkeypatch):
    """Both-sides ACT leg: the withdrawal must not cost the merge semantics.

    Two passes over the same file that BOTH succeed keep the UNION of their
    rows in the keep-set — otherwise pass B would authorise deleting pass A's
    perfectly valid entities.
    """
    _stub_embeddings(analyzer_mod, monkeypatch)
    analyzer = _wire(analyzer_mod)
    analyzer._reconcile_walked = {}
    analyzer._reconcile_pending = None
    analyzer._current_source = "/repo"
    rel = "app.svelte"
    key = ("/repo", rel)

    analyzer.write_file_extraction(_two_pass_extraction(analyzer_mod, rel, ["a.one"]))
    after_a = {u for uids in analyzer._reconcile_walked[key].values() for u in uids}
    analyzer.write_file_extraction(_two_pass_extraction(analyzer_mod, rel, ["b.two"]))
    after_b = {u for uids in analyzer._reconcile_walked[key].values() for u in uids}

    assert after_a, "pre-condition"
    assert after_a.issubset(after_b), (
        "pass B must RESTORE pass A's authorisation together with its own — "
        f"lost {after_a - after_b}"
    )
    assert len(after_b) > len(after_a), "pass B's own rows must be added too"


def test_a_syntax_broken_python_file_never_enters_the_keep_set(
    analyzer_mod, tmp_path, monkeypatch
):
    """v0.2.91 fix-round NIT-4 (disposition pin).

    Concern: a per-edit hook firing on a mid-typing save could reduce the entity
    set and let the reconcile delete temporarily-unparseable entities. For the
    ONE language whose extractor actually parses (python, ``ast.parse``), a
    syntax error returns a module-less ``FileExtraction`` — the writer no-ops
    BEFORE opening a reconcile scope, so the file is never authorised and its
    rows are never judged. That is the conservative leave-alone the finding
    asks for, and this pins it so a future refactor of the syntax-error path
    cannot quietly turn a broken save into a delete authorisation.
    """
    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = tmp_path
    (repo / "broken.py").write_text("def main(:\n    pass\n", encoding="utf-8")
    analyzer = _wire(analyzer_mod)
    existing = _seed_stale_function(
        analyzer, analyzer_mod, "broken.py", "broken.main", repo.as_posix(),
    )

    analyzer.analyze_repository(repo)

    assert ("", "broken.py") not in (analyzer._reconcile_walked or {})
    assert (repo.as_posix(), "broken.py") not in (analyzer._reconcile_walked or {})
    assert existing not in analyzer.functions_collection.deleted, (
        "an unparseable file yields no entity evidence, so it must never "
        "authorise deleting the entities it used to declare"
    )


# ═════════════════════ narrowed candidate read (cost) ══════════════════════


def _tokens(path: str):
    return [t for t in str(path).replace("/", " ").replace(".", " ").split() if t]


class _QueryColl(_Coll):
    """Collection whose ``.query.fetch_objects`` honours a sentinel filter.

    The real filter is a weaviate ``_Filters`` object we cannot introspect, so
    the reconcile's filter builder is stubbed to a sentinel carrying the path
    set — enough to prove the narrowed read is USED and that it does not change
    the delete decision.
    """

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.fetch_calls = 0
        self.fetch_raises = False
        outer = self

        class _Q:
            def fetch_objects(self, filters=None, limit=0, offset=0,
                              return_properties=None):
                outer.fetch_calls += 1
                if outer.fetch_raises:
                    raise RuntimeError("simulated narrowed-read failure")
                # Emulate Weaviate's WORD-TOKENIZED `.equal()`: the narrowed
                # read returns a SUPERSET — every row whose path shares a token
                # with a requested path. Live-verified 2026-08-26: a narrowed
                # read for `install.py` also returns
                # `tests/test_codegraph_ts_install_plan.py`. Only the Python
                # exact-compare inside the primitive may decide a delete.
                want = set()
                for path in (getattr(filters, "paths", ()) or ()):
                    want |= set(_tokens(path))
                rows = [
                    _Obj(u, p) for u, p in list(outer.store.items())
                    if want & set(_tokens(p.get(outer.path_prop) or ""))
                ]
                return types.SimpleNamespace(objects=rows[offset:offset + limit])

        self.query = _Q()


def _sentinel_filter(monkeypatch):
    monkeypatch.setattr(
        cr, "_narrow_filter_for_paths",
        lambda path_prop, paths: types.SimpleNamespace(paths=set(paths)),
    )


def test_narrowed_read_is_used_for_a_small_walk_and_decides_identically(
    monkeypatch,
):
    _sentinel_filter(monkeypatch)
    coll = _QueryColl(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    stale = coll.seed(
        "stale", file_path="src/x.py", project=PROJECT, project_source="/repo",
        full_name="x.gone", embed_revision=None,
    )
    keep_other_file = coll.seed(
        "other", file_path="src/y.py", project=PROJECT, project_source="/repo",
        full_name="y.here", embed_revision=None,
    )
    # The over-delete guard: a token-sharing sibling the narrowed read WILL
    # return (`src/x.py` vs `src/x_helper.py` share the `src` + `x` tokens).
    token_sibling = coll.seed(
        "token-sibling", file_path="src/x_helper.py", project=PROJECT,
        project_source="/repo", full_name="x_helper.gone", embed_revision=None,
    )

    deleted, _ = cr.reconcile_walked_file_rows(
        ((coll, "file_path"),),
        {("/repo", "src/x.py"): {coll.name: {"live"}}},
        project_name=PROJECT,
        primary_sources={"/repo"},
        deleter=cr.delete_file_rows_exact,
    )

    assert coll.fetch_calls >= 1, "a 1-path walk must not pay a full scan"
    assert coll.iter_calls == 0
    assert deleted == 1 and stale in coll.deleted
    assert keep_other_file not in coll.deleted
    assert token_sibling not in coll.deleted, (
        "the narrowed read over-fetches token-sharing siblings — only the "
        "Python exact-compare may authorise a delete"
    )


def test_narrowed_read_failure_falls_back_to_the_full_scan(monkeypatch):
    """Completeness is never traded for cost: a narrowed read that cannot be
    completed falls back to the full scan (same decision, more reads)."""
    _sentinel_filter(monkeypatch)
    coll = _QueryColl(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    coll.fetch_raises = True
    stale = coll.seed(
        "stale", file_path="src/x.py", project=PROJECT, project_source="/repo",
        full_name="x.gone", embed_revision=None,
    )

    deleted, _ = cr.reconcile_walked_file_rows(
        ((coll, "file_path"),),
        {("/repo", "src/x.py"): {coll.name: {"live"}}},
        project_name=PROJECT,
        primary_sources={"/repo"},
        deleter=cr.delete_file_rows_exact,
    )

    assert coll.iter_calls == 1 and deleted == 1 and stale in coll.deleted


def test_many_walked_paths_use_one_full_scan(monkeypatch):
    """Above the threshold a whole-repo walk pays ONE scan per collection
    instead of hundreds of narrowed reads."""
    _sentinel_filter(monkeypatch)
    coll = _QueryColl(f"{PROJECT}_CodeFunction", "file_path", ("full_name",))
    walked = {
        ("/repo", f"src/f{i}.py"): {coll.name: {"live"}}
        for i in range(cr._RECONCILE_NARROW_MAX_PATHS + 1)
    }
    cr.reconcile_walked_file_rows(
        ((coll, "file_path"),), walked,
        project_name=PROJECT, primary_sources={"/repo"},
        deleter=cr.delete_file_rows_exact,
    )
    assert coll.fetch_calls == 0 and coll.iter_calls == 1


# ══════════════ item 2: convergence report agrees with the owed gate ═══════


def _kind_client(rows_by_base, prefix="P"):
    colls = {}
    for base, path_prop in (("CodeModule", "path"), ("CodeClass", "file_path"),
                            ("CodeFunction", "file_path")):
        c = _Coll(f"{prefix}_{base}", path_prop,
                  () if base == "CodeModule" else ("full_name",))
        for uid, props in rows_by_base.get(base, []):
            c.seed(uid, **props)
        colls[c.name] = c
    return _fake_client(list(colls.values())), colls


def test_classify_stale_kinds_agrees_with_the_owed_gate(monkeypatch, tmp_path):
    """RED-PROOF (item 2): pre-WP-C ``classify_stale_kinds`` took no
    ``repo_root``, so it counted RAW revision-stale rows — extra-path clones and
    deleted-file orphans included — and the driver logged
    ``stale rows: 12, embed_owed=1896``. With the root the split covers exactly
    the rows the owed gate counts."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    (tmp_path / "live.py").write_text("x = 1\n", encoding="utf-8")
    src = tmp_path.as_posix()
    rows = {"CodeFunction": [
        ("owed", {"file_path": "live.py", "project": "P", "project_source": src,
                  "full_name": "live.f", "embed_revision": None}),
        ("extra", {"file_path": "live.py", "project": "P",
                   "project_source": "/other/root", "full_name": "live.f",
                   "embed_revision": None}),
        ("orphan", {"file_path": "gone.py", "project": "P",
                    "project_source": src, "full_name": "gone.f",
                    "embed_revision": None}),
        ("current", {"file_path": "live.py", "project": "P",
                     "project_source": src, "full_name": "live.g",
                     "embed_revision": 1}),
    ]}
    client, _ = _kind_client(rows)

    counts = cr.count_stale_rows(
        "P", current_revision=1, repo_root=tmp_path, client=client,
    )
    split = cr.classify_stale_kinds(
        "P", current_revision=1, floor_revision=1, repo_root=tmp_path,
        client=client,
    )

    assert counts is not None and sum(counts.values()) == 1
    assert split == {"embed_owed": 1, "stamp_owed": 0}
    assert split["embed_owed"] + split["stamp_owed"] == sum(counts.values()), (
        "the convergence REPORT must agree with the owed GATE"
    )


def test_classify_stale_kinds_without_root_keeps_the_legacy_split(monkeypatch):
    """Back-compat: callers with no root keep the revision-only behaviour."""
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    rows = {"CodeFunction": [
        (f"r{i}", {"file_path": "x.py", "project": "P", "project_source": "",
                   "full_name": "x.f", "embed_revision": rev})
        for i, rev in enumerate((None, 0, 1, 2, 3))
    ]}
    client, _ = _kind_client(rows)
    split = cr.classify_stale_kinds(
        "P", current_revision=3, floor_revision=2, client=client,
    )
    # Same matrix as the shipped WP-2 pin: NULL/0/floor-1 are embed_owed,
    # floor is stamp_owed, current is neither. No root ⇒ no owed-gate filter,
    # so the "x.py does not exist on disk" orphan class is still counted.
    assert split == {"embed_owed": 3, "stamp_owed": 1}


# ═════════════ item 3: no-progress runs name the stuck identities ══════════


def test_list_owed_row_identities_lists_exactly_the_owed_rows(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(cr, "_collection_prefix", lambda name: "P")
    (tmp_path / "live.py").write_text("x = 1\n", encoding="utf-8")
    src = tmp_path.as_posix()
    rows = {"CodeFunction": [
        ("owed-1", {"file_path": "live.py", "project": "P",
                    "project_source": src, "full_name": "live.stuck",
                    "embed_revision": None}),
        ("extra", {"file_path": "live.py", "project": "P",
                   "project_source": "/other", "full_name": "live.other",
                   "embed_revision": None}),
    ]}
    client, _ = _kind_client(rows)

    idents = cr.list_owed_row_identities(
        "P", repo_root=tmp_path, current_revision=1, client=client,
    )

    assert idents == [{
        "collection": "P_CodeFunction", "uuid": "owed-1",
        "full_name": "live.stuck", "path": "live.py",
    }]


def test_driver_no_progress_names_the_stuck_rows_in_the_ledger(
    monkeypatch, tmp_path
):
    """RED-PROOF (item 3): pre-WP-C the driver had no pre-walk probe and
    ``build_unconverged_deferral`` had no stuck-identity argument, so a stuck
    project's entry could only ever say how MANY rows were owed."""
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: 0)
    monkeypatch.setattr(
        cr.subprocess, "run",
        lambda argv, **kw: types.SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        cr, "count_stale_rows", lambda *a, **k: {"P_CodeFunction": 9},
    )
    monkeypatch.setattr(cr, "classify_stale_kinds", lambda *a, **k: None)
    monkeypatch.setattr(
        cr, "list_owed_row_identities",
        lambda *a, **k: [{
            "collection": "P_CodeFunction", "uuid": "u-1",
            "full_name": "install.select_summary_backend", "path": "install.py",
        }],
    )

    cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    )

    from vco_lib.deferral_report import DeferralReport

    entry = [e for e in DeferralReport.read(tmp_path).entries
             if e.condition_id == "codegraph_embed_resync_pending"][0]
    assert "NO progress" in entry.detected
    assert "install.select_summary_backend" in entry.detected
    assert "u-1" in entry.detected
    assert "\n" not in entry.detected, "A-3: ledger fields must stay single-line"


def test_driver_progress_run_does_not_claim_no_progress(monkeypatch, tmp_path):
    """Leave-alone for the no-progress branch: a walk that reduced the owed set
    is NOT reported as stuck and does not enumerate identities."""
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: 0)
    monkeypatch.setattr(
        cr.subprocess, "run",
        lambda argv, **kw: types.SimpleNamespace(returncode=0),
    )
    seq = [{"P_CodeFunction": 9}, {"P_CodeFunction": 4}]
    monkeypatch.setattr(cr, "count_stale_rows", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(cr, "classify_stale_kinds", lambda *a, **k: None)

    def _never(*a, **k):
        raise AssertionError("progress was made — do not enumerate identities")

    monkeypatch.setattr(cr, "list_owed_row_identities", _never)

    cr.run_resync_and_verify(
        "MyProj", tmp_path, tmp_path / "analyze_code_graph.py"
    )

    from vco_lib.deferral_report import DeferralReport

    entry = [e for e in DeferralReport.read(tmp_path).entries
             if e.condition_id == "codegraph_embed_resync_pending"][0]
    assert "NO progress" not in entry.detected


def test_unconverged_entry_text_is_honest_about_auto_clearing(tmp_path):
    """The documented contract that made the entry immortal ("clears
    automatically when a later update's probe confirms zero stale rows") is now
    TRUE for the refactor-drift class — say so, and say why."""
    entry = cr.build_unconverged_deferral(
        "P", {"P_CodeFunction": 3}, "cmd",
    )
    assert entry is not None
    assert "refactored out of a file it re-walked" in entry.why_deferred
    assert "clears automatically once a later update's probe confirms" in (
        entry.why_deferred
    )


# ═══════════════ end-to-end: the immortal deferral now clears ══════════════


def test_end_to_end_stuck_deferral_clears_after_a_reconciling_walk(
    analyzer_mod, tmp_path, monkeypatch
):
    """The whole complaint, start to finish.

    A project sitting on the entity-orphan floor with a persisted
    ``codegraph_embed_resync_pending`` entry: the driver runs the walk (here the
    REAL analyzer against fake collections), the post-walk owed probe reads a
    positive ZERO, and the ledger entry is resolved.

    RED-PROOF: on bd8f6836 the walk cannot delete the orphan, the post-walk
    probe still reports 1 owed row, and the entry survives (re-written with a
    fresh timestamp) — exactly the field behaviour reported on 2026-07-25 and
    reproduced on this machine on 2026-07-31.
    """
    from vco_lib.deferral_report import DeferralEntry, DeferralReport

    _stub_embeddings(analyzer_mod, monkeypatch)
    repo = _make_repo(tmp_path)
    analyzer = _wire(analyzer_mod)
    _seed_stale_function(
        analyzer, analyzer_mod, "install.py",
        "install.select_summary_backend", repo.as_posix(),
    )

    rep = DeferralReport.read(repo)
    rep.add_entry(DeferralEntry(
        condition_id="codegraph_embed_resync_pending",
        title="Code-graph resync did not fully converge",
        detected="stale rows remain (CodeFunction: 1)",
        why_deferred="w", command_to_apply="cmd", severity="warning",
    ))
    rep.write(repo)

    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *a, **k: 0)
    monkeypatch.setattr(cr, "count_stale_rows",
                        lambda *a, **k: _owed_counts(analyzer, repo))

    def _walk(argv, **kw):
        analyzer.analyze_repository(repo)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cr.subprocess, "run", _walk)

    rc = cr.run_resync_and_verify(
        PROJECT, repo, repo / "analyze_code_graph.py",
    )

    assert rc == 0
    assert not DeferralReport.read(repo).has_condition(
        "codegraph_embed_resync_pending"
    ), "the immortal entry must clear once the walk can actually converge"
