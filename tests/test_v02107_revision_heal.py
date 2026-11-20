# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 (CG-3): the revision heal — a cursor over the owed rows, ONE scoped
re-extraction of their files, never a whole-repo walk and never a doubtful delete.

Owner rule under test: "be conservative in what gets deleted to never delete
something that was still needed/in use". A heal replaces a file's rows only
after the new rows are written; on any doubt (count unknown, listing
undeterminable, git unable to answer, file missing, list unwritable) no
analyzer runs and the heal stays owed.

No test here talks to Weaviate or the network: the collections are in-memory
fakes, git runs only against a throw-away ``tmp_path`` repository.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import types
from pathlib import Path

import pytest

from tests.common import cursor_fakes as cf
from tests.test_v0291_entity_reconcile import (
    PROJECT,
    _fake_client,
    _stub_embeddings,
    _wire,
)
from vco_lib import codegraph_path_reconcile as recon
from vco_lib import codegraph_resync as cr
from vco_lib import codegraph_revision_heal as rh
from vco_lib import git_meta

_ANALYZER = Path(__file__).resolve().parent.parent / "templates" / "scripts" / "analyze_code_graph.py"
_CURRENT = cr._resolve_embed_revision(_ANALYZER)


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    """The shipped analyzer, loaded as a module (no CLI, no Weaviate)."""
    spec = importlib.util.spec_from_file_location("_revheal_analyze_code_graph", str(_ANALYZER))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _not_git(_root):
    return recon.NOT_GIT, None


def _probe_finds_a_stale_row(coll):
    """The one-row existence probe (a filtered ``fetch_objects(limit=1)``, not
    a listing) answers "there is a stale row", so the cursor pass runs."""
    coll.filtered_reply = lambda *_a, **_k: types.SimpleNamespace(objects=[cf.row(9999)])
    return coll


# ═══════════════════════ the owed-row listing (cursor) ══════════════════════


def test_the_owed_count_lists_the_owed_files_in_the_same_cursor_pass(tmp_path):
    """ACT: one cursor pass counts the owed rows AND collects their paths —
    only the owed ones (a current row, an extra-root row and a deleted file's
    row are not listed), reading three small properties, always from the nil
    uuid, never a filtered listing."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    src = tmp_path.as_posix()
    coll = _probe_finds_a_stale_row(cf.CursorClass("P_CodeFunction", [
        cf.row(1, {"file_path": "a.py", "embed_revision": _CURRENT - 1, "project_source": src}),
        cf.row(2, {"file_path": "b.py", "embed_revision": _CURRENT, "project_source": src}),
        cf.row(3, {"file_path": "x.py", "embed_revision": _CURRENT - 1,
                   "project_source": "/elsewhere/extra"}),
        cf.row(4, {"file_path": "gone.py", "embed_revision": _CURRENT - 1, "project_source": src}),
    ]))
    paths: set = set()
    n = cr._count_stale_in_collection(
        coll, _CURRENT, reachable_fn=cr._make_reachability_filter(tmp_path),
        primary_sources=cr.primary_sources_for(tmp_path), index_dot_claude=False,
        owed_paths=paths,
    )
    assert n == 1 and paths == {"a.py"}
    pages = coll.listing_pages()
    assert pages and pages[0]["after"] == cf.NIL
    assert all(p.get("limit") == 1 for p in coll.page_requests if p.get("filters") is not None)
    assert all(set(p["return_properties"]) <= {"embed_revision", "file_path", "project_source"}
               for p in pages)


def test_an_unreadable_class_makes_the_listing_undeterminable(tmp_path):
    """LEAVE-ALONE: a class whose listing fails part-way yields no count, so the
    plan runs nothing (the paths it saw before the failure are not trusted)."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    coll = _probe_finds_a_stale_row(cf.CursorClass("P_CodeFunction", [
        cf.row(i, {"file_path": "a.py", "embed_revision": 0}) for i in range(1, 250)
    ], fail_on_page=2))
    paths: set = set()
    assert cr._count_stale_in_collection(
        coll, _CURRENT, reachable_fn=cr._make_reachability_filter(tmp_path),
        index_dot_claude=False, owed_paths=paths,
    ) is None
    plan = rh.plan_revision_heal(
        tmp_path, lambda p: None, linked_worktree=lambda _r: None)
    assert plan.verdict == rh.UNDETERMINABLE and not plan.files


# ═══════════════════════════ the plan (pure) ════════════════════════════════


def _count(counts, paths):
    def _c(out: set):
        out.update(paths)
        return counts
    return _c


def test_plan_heals_exactly_the_owed_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    plan = rh.plan_revision_heal(
        tmp_path, _count({"P_CodeFunction": 3}, {"a.py", "../outside.py", "gone.py"}),
        linked_worktree=lambda _r: None,
        select=lambda root, paths: rh.select_files(root, paths, committed=_not_git),
    )
    assert plan.verdict == rh.HEAL
    assert plan.files == [str(tmp_path / "a.py")]
    assert plan.held == {rh.HELD_OUTSIDE_ROOT: 1, rh.HELD_MISSING: 1}


@pytest.mark.parametrize("case", ["count_none", "count_none_after_partial_listing",
                                  "count_raises", "counted_not_listed",
                                  "git_unknown", "uncommitted_unknown", "select_raises"])
def test_plan_on_any_doubt_runs_nothing(tmp_path, case):
    """LEAVE-ALONE: every undeterminable input gives a plan that runs no
    analyzer (and so changes and deletes nothing)."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    def _raise(_p):
        raise RuntimeError("listing failed")

    count = {
        "count_none": lambda p: None,
        # a listing that failed part-way: paths seen, but no verdict
        "count_none_after_partial_listing": _count(None, {"a.py"}),
        "count_raises": _raise,
        "counted_not_listed": _count({"P_CodeFunction": 2}, set()),
    }.get(case, _count({"P_CodeFunction": 2}, {"a.py"}))
    def select(root, paths):
        return rh.select_files(root, paths, committed=_not_git)
    if case == "git_unknown":
        def select(root, paths):
            return rh.select_files(root, paths, committed=lambda _r: (recon.GIT_UNKNOWN, None))
    elif case == "uncommitted_unknown":
        def select(root, paths):
            return rh.select_files(root, paths, committed=lambda _r: ("git", {"a.py"}),
                                   uncommitted=lambda _r: None)
    elif case == "select_raises":
        def select(root, paths):
            raise OSError("boom")
    plan = rh.plan_revision_heal(tmp_path, count, linked_worktree=lambda _r: None,
                                 select=select)
    assert plan.verdict == rh.UNDETERMINABLE
    assert not plan.runs_analyzer and plan.files == []


def test_plan_in_a_linked_worktree_heals_nothing_and_lists_nothing(tmp_path):
    """LEAVE-ALONE: worktrees are never indexed — not even listed."""
    def _never(_p):
        raise AssertionError("a worktree must not even be listed")

    plan = rh.plan_revision_heal(tmp_path, _never, linked_worktree=lambda _r: Path("/main"))
    assert plan.verdict == rh.WORKTREE and not plan.runs_analyzer


def test_plan_positive_zero_is_nothing_owed(tmp_path):
    plan = rh.plan_revision_heal(tmp_path, _count({"P_CodeFunction": 0}, set()),
                                 linked_worktree=lambda _r: None)
    assert plan.verdict == rh.NOTHING_OWED and plan.counts == {"P_CodeFunction": 0}


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.invalid",
                    "-c", "user.name=t", *args], check=True, capture_output=True, env=env)


def test_only_committed_unchanged_files_are_re_extracted(tmp_path):
    """The graph follows the main checkout's commits: a modified (staged or
    not) tracked file and an untracked file are HELD (their commit brings them
    in); a committed, unchanged file is re-extracted. A nested project folder
    is judged relative to itself."""
    repo = tmp_path / "repo"
    proj = repo / "sub"
    proj.mkdir(parents=True)
    _git(repo, "init", "-q")
    for name in ("clean.py", "edited.py", "staged.py"):
        (proj / name).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    (proj / "edited.py").write_text("x = 2\n", encoding="utf-8")
    (proj / "staged.py").write_text("x = 3\n", encoding="utf-8")
    _git(repo, "add", "sub/staged.py")
    (proj / "untracked.py").write_text("x = 4\n", encoding="utf-8")
    assert git_meta.uncommitted_paths(proj) == {"edited.py", "staged.py"}
    files, held, why = rh.select_files(
        proj, {"clean.py", "edited.py", "staged.py", "untracked.py"})
    assert why == ""
    assert files == [str(proj / "clean.py")]
    assert held == {rh.HELD_UNCOMMITTED: 3}


def test_a_project_that_is_not_a_git_checkout_heals_its_present_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    files, held, _why = rh.select_files(tmp_path, {"a.py"}, committed=_not_git)
    assert files == [str(tmp_path / "a.py")] and held == {}


def test_uncommitted_paths_is_none_when_git_cannot_answer(tmp_path):
    """``None`` = cannot tell (no HEAD yet), never "clean"."""
    _git(tmp_path, "init", "-q")
    assert git_meta.uncommitted_paths(tmp_path) is None


# ═══════════════════════════ the driver ═════════════════════════════════════


def _driver(monkeypatch, tmp_path, *, owed_paths, pre, post, cleanup=0, worktree=None,
            fill=False):
    """Neutralise every non-heal gate of the driver; feed the owed count."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(cr, "walk_uses_container", lambda *_a, **_k: False)
    monkeypatch.setattr(cr, "code_backend_down_waiting", lambda *_a, **_k: (None, False))
    monkeypatch.setattr(cr, "_maybe_run_exposure_heal", lambda *_a, **_k: False)
    monkeypatch.setattr(cr, "_maybe_run_vectorless_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "identity_sweep_if_stale", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "spawn_code_summary_after_walk", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "_collect_terminal_stats", lambda *_a, **_k: ({}, None))
    monkeypatch.setattr(cr, "count_orphan_interaction_rows", lambda *_a, **_k: 0)
    monkeypatch.setattr(cr, "_resolve_orphan_interactions_deferral", lambda *_a, **_k: None)
    monkeypatch.setattr(cr, "count_cleanup_owed_rows", lambda *_a, **_k: cleanup)
    monkeypatch.setattr(cr, "_code_model_fill_owed", lambda _p: fill)
    monkeypatch.setattr(git_meta, "linked_worktree_main", lambda _r: worktree)
    monkeypatch.setattr(recon, "committed_tree", _not_git)
    seen = {"runs": [], "lists": [], "terminal": [], "unconverged": [], "resolved": 0,
            "counts": []}
    sequence = [pre, post]

    def _count(project_name, *, owed_paths=None, **_k):
        if owed_paths is not None:
            owed_paths.update(owed_paths_in)
        seen["counts"].append(owed_paths is not None)
        return sequence.pop(0) if sequence else post

    owed_paths_in = set(owed_paths)
    monkeypatch.setattr(cr, "count_stale_rows", _count)

    def _run(argv, **_kw):
        seen["runs"].append(list(argv))
        if "--only-files-from" in argv:
            listed = Path(argv[argv.index("--only-files-from") + 1])
            seen["lists"].append(listed.read_text(encoding="utf-8").splitlines())
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cr.subprocess, "run", _run)
    monkeypatch.setattr(cr, "_report_terminal_to_hub",
                        lambda *a, **k: seen["terminal"].append((a, k)))
    monkeypatch.setattr(cr, "_record_unconverged_deferral",
                        lambda *a, **k: seen["unconverged"].append(a))

    def _resolved(_root):
        seen["resolved"] += 1

    monkeypatch.setattr(cr, "_resolve_persisted_resync_deferral", _resolved)
    return seen


def test_driver_heals_by_one_scoped_run_over_the_owed_files(monkeypatch, tmp_path, capsys):
    """ACT: the driver's walk is ONE ``--only-files-from <owed files>
    --revision-heal`` run — no whole-repo walk, no --force-rewalk — the list
    is the owed files only and is removed after the run; convergence clears
    the ledger entry."""
    seen = _driver(monkeypatch, tmp_path, owed_paths={"a.py", "gone.py"},
                   pre={"P_CodeFunction": 4}, post={"P_CodeFunction": 0})
    assert cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER) == 0
    assert len(seen["runs"]) == 1
    argv = seen["runs"][0]
    assert "--revision-heal" in argv and "--force-rewalk" not in argv
    assert "--prune-stale" not in argv
    assert seen["lists"] == [[str(tmp_path / "a.py")]]
    assert not (tmp_path / ".claude" / "state" / rh.LIST_FILE_NAME).exists()
    assert seen["resolved"] == 1 and seen["unconverged"] == []
    assert "held: missing=1" in capsys.readouterr().out


@pytest.mark.parametrize("pre,owed", [(None, set()), ({"P_CodeFunction": 2}, set())])
def test_driver_on_doubt_runs_no_analyzer_and_keeps_the_heal_owed(
        monkeypatch, tmp_path, pre, owed):
    """LEAVE-ALONE: count unknown, or owed rows that were not listed → no
    analyzer, a ``skipped`` terminal report, the unconverged entry recorded."""
    seen = _driver(monkeypatch, tmp_path, owed_paths=owed, pre=pre, post=pre)
    assert cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER) == 0
    assert seen["runs"] == []
    assert [k[0][1] for k in seen["terminal"]] == ["skipped"]
    assert len(seen["unconverged"]) == 1 and seen["resolved"] == 0


def test_driver_held_files_only_runs_no_analyzer(monkeypatch, tmp_path):
    """LEAVE-ALONE: every owed file is missing → nothing to re-extract, nothing
    deleted, the heal stays owed."""
    seen = _driver(monkeypatch, tmp_path, owed_paths={"gone.py"},
                   pre={"P_CodeFunction": 2}, post={"P_CodeFunction": 2})
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert seen["runs"] == [] and len(seen["unconverged"]) == 1


def test_driver_in_a_worktree_runs_nothing(monkeypatch, tmp_path):
    seen = _driver(monkeypatch, tmp_path, owed_paths={"a.py"},
                   pre={"P_CodeFunction": 2}, post=None, worktree=Path("/main"))
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert seen["runs"] == []


def test_driver_list_file_unwritable_runs_nothing(monkeypatch, tmp_path):
    seen = _driver(monkeypatch, tmp_path, owed_paths={"a.py"},
                   pre={"P_CodeFunction": 2}, post={"P_CodeFunction": 2})
    monkeypatch.setattr(rh, "write_list_file", lambda *_a, **_k: None)
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert seen["runs"] == [] and len(seen["unconverged"]) == 1


def test_driver_nothing_owed_runs_nothing_and_clears(monkeypatch, tmp_path):
    seen = _driver(monkeypatch, tmp_path, owed_paths=set(),
                   pre={"P_CodeFunction": 0}, post={"P_CodeFunction": 0})
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert seen["runs"] == [] and seen["resolved"] == 1


@pytest.mark.parametrize("cleanup,walks", [(3, 1), (None, 0), (0, 0)])
def test_driver_deleted_file_rows_get_the_whole_repo_sweep_only_on_a_positive_count(
        monkeypatch, tmp_path, cleanup, walks):
    """Nothing revision-owed but deleted-file rows positively counted → the
    whole-repo walk (its sweep is what the spawn gate launched the driver
    for); an unknown count → no walk (no sweep on doubt)."""
    seen = _driver(monkeypatch, tmp_path, owed_paths=set(), cleanup=cleanup,
                   pre={"P_CodeFunction": 0}, post={"P_CodeFunction": 0})
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert len(seen["runs"]) == walks
    for argv in seen["runs"]:
        assert "--only-files-from" not in argv


@pytest.mark.parametrize("flag", ["force_rewalk", "prune_stale"])
def test_a_forced_or_pruning_driver_keeps_the_whole_repo_walk(monkeypatch, tmp_path, flag):
    """An extractor-generation rung (``--force-rewalk``) needs every file;
    the heal never replaces that walk (CGWRITE-02: a forced walk that did not
    run stays owed through the generation stamp, not through this plan)."""
    seen = _driver(monkeypatch, tmp_path, owed_paths={"a.py"},
                   pre={"P_CodeFunction": 2}, post={"P_CodeFunction": 0})
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER, **{flag: True})
    assert len(seen["runs"]) == 1
    assert "--only-files-from" not in seen["runs"][0]
    assert seen["counts"][0] is False, "the forced walk takes no heal listing"


def test_a_pending_code_model_fill_keeps_the_whole_graph_walk(monkeypatch, tmp_path):
    """A slot fill embeds the new model's vector on EVERY row (current ones
    included), so it needs the whole-graph walk; a scoped heal run would only
    hand the fill back to another driver."""
    seen = _driver(monkeypatch, tmp_path, owed_paths={"a.py"}, fill=True,
                   pre={"P_CodeFunction": 2}, post={"P_CodeFunction": 0})
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert len(seen["runs"]) == 1 and "--only-files-from" not in seen["runs"][0]


def test_the_heal_run_skipped_by_a_running_walk_stays_owed_and_drops_its_list(
        monkeypatch, tmp_path):
    from vco_lib import codegraph_walk

    seen = _driver(monkeypatch, tmp_path, owed_paths={"a.py"},
                   pre={"P_CodeFunction": 2}, post={"P_CodeFunction": 2})

    class _Held:
        def __enter__(self):
            raise codegraph_walk.WalkAlreadyRunning("held")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(codegraph_walk, "walk_claim", lambda _p: _Held())
    cr._run_resync_and_verify_slotted("P", tmp_path, _ANALYZER)
    assert seen["runs"] == [] and seen["resolved"] == 0
    assert not (tmp_path / ".claude" / "state" / rh.LIST_FILE_NAME).exists()


# ═══════════════════════ the analyzer's heal run ════════════════════════════


def _seed(coll, analyzer_mod, rel, full_name, source, **props):
    uid = analyzer_mod._deterministic_uuid(PROJECT, rel, full_name, project_source=source)
    return coll.seed(uid, file_path=rel, project=PROJECT, project_source=source,
                     full_name=full_name, **props)


def test_heal_run_re_extracts_the_listed_file_and_touches_no_other(
        analyzer_mod, tmp_path, monkeypatch):
    """ACT, end to end on the real analyzer: the owed row of the listed file is
    rewritten at the current revision; the unlisted file's row is not
    rewritten; nothing is deleted."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def g():\n    return 2\n", encoding="utf-8")
    src = tmp_path.as_posix()
    analyzer = _wire(analyzer_mod)
    analyzer.force_rewalk, analyzer.revision_heal = True, True
    owed = _seed(analyzer.functions_collection, analyzer_mod, "a.py", "a.f", src,
                 embed_revision=_CURRENT - 1)
    other = _seed(analyzer.functions_collection, analyzer_mod, "b.py", "b.g", src,
                  embed_revision=_CURRENT - 1, marker="untouched")
    paths: set = set()
    counts = cr.count_stale_rows(
        PROJECT, current_revision=_CURRENT, repo_root=tmp_path, index_dot_claude=False,
        client=_fake_client([analyzer.modules_collection, analyzer.classes_collection,
                             analyzer.functions_collection]),
        owed_paths=paths,
    )
    assert counts and paths == {"a.py", "b.py"}
    listed = tmp_path / "list.txt"
    listed.write_text(f"{tmp_path / 'a.py'}\n", encoding="utf-8")
    analyzer.analyze_repository(tmp_path, only_files_from=listed)
    store = analyzer.functions_collection.store
    assert store[owed]["embed_revision"] == _CURRENT
    assert store[other].get("marker") == "untouched"
    assert analyzer.functions_collection.deleted == []


@pytest.mark.parametrize("heal,kept", [(True, True), (False, False)])
def test_a_listed_file_that_vanished_keeps_its_rows_under_the_heal(
        analyzer_mod, tmp_path, monkeypatch, heal, kept):
    """LEAVE-ALONE (heal) vs ACT (the catch-up's ordinary batch): a listed path
    gone from disk is pruned by an ordinary ``--only-files-from`` run, never by
    the heal — a missing file is a doubt, not evidence."""
    _stub_embeddings(analyzer_mod, monkeypatch)
    (tmp_path / "keep.py").write_text("def k():\n    return 0\n", encoding="utf-8")
    src = tmp_path.as_posix()
    analyzer = _wire(analyzer_mod)
    analyzer.force_rewalk, analyzer.revision_heal = heal, heal
    uid = _seed(analyzer.functions_collection, analyzer_mod, "gone.py", "gone.f", src,
                embed_revision=_CURRENT - 1)
    listed = tmp_path / "list.txt"
    listed.write_text(f"{tmp_path / 'gone.py'}\n", encoding="utf-8")
    analyzer.analyze_repository(tmp_path, only_files_from=listed)
    assert (uid in analyzer.functions_collection.store) is kept


def test_cli_revision_heal_needs_a_file_list(analyzer_mod, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("sys.argv", ["analyze_code_graph.py", str(tmp_path),
                                     "--project", "P", "--revision-heal"])
    assert analyzer_mod.main() == 1
    assert "--revision-heal needs --only-files-from" in capsys.readouterr().err


# ═══════════════ chunk tail: deleted after the file's writes ════════════════


class _F3Data:
    def __init__(self):
        self.replaced = []

    def replace(self, uuid, **_kw):
        self.replaced.append(str(uuid))


class _F3Coll:
    name = "TProj_CodeFunction"

    def __init__(self):
        self.data = _F3Data()
        self.query = types.SimpleNamespace(fetch_object_by_id=lambda *_a, **_k: types.SimpleNamespace(
            properties={"content_hash": "old", "total_chunks": 3}))
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(properties=[]))


@pytest.mark.parametrize("scope_open,deletes_now", [(True, False), (False, True)])
def test_a_shrunk_entity_tail_waits_for_the_file_scope(analyzer_mod, scope_open, deletes_now):
    """Inside a file's open reconcile scope the tail rows of a shrunk entity are
    NOT deleted before the new row is written — the per-file reconcile deletes
    the unwritten chunk uuids once every write of the file succeeded. Outside a
    scope (a direct writer) the immediate cleanup is unchanged."""
    analyzer = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    analyzer.project_name = "TProj"
    analyzer.visited_uuids = set()
    analyzer._track_visited = False
    analyzer._current_language = ""
    analyzer._current_source = ""
    if scope_open:
        analyzer._reconcile_pending = (("", "src/m.py"), {}, None)
    calls: list = []
    analyzer._delete_stale_chunk_rows = lambda *a: calls.append(a)
    coll = _F3Coll()
    props = {"full_name": "mod.f", "file_path": "src/m.py", "signature": "def f()",
             "function_body": "def f():\n    pass", "chunk_num": 0, "total_chunks": 1}
    analyzer._write_one_object(coll, "u-0", {"properties": props}, "mod.f")
    assert bool(calls) is deletes_now
    assert coll.data.replaced, "the new row is written either way"


def test_the_reconcile_removes_the_unwritten_tail_after_the_file_scope_commits(tmp_path):
    """The other half of the deferral: the per-file reconcile deletes the tail
    chunk rows (uuids this walk did not write) of a walked file — and only
    those."""
    src = tmp_path.as_posix()
    coll = cf.CursorClass("P_CodeFunction", [
        cf.row(1, {"file_path": "m.py", "project": "P", "project_source": src,
                   "full_name": "m.f", "chunk_num": 0}),
        cf.row(2, {"file_path": "m.py", "project": "P", "project_source": src,
                   "full_name": "m.f", "chunk_num": 1}),
        cf.row(3, {"file_path": "other.py", "project": "P", "project_source": src,
                   "full_name": "o.g", "chunk_num": 1}),
    ])
    walked = {(src, "m.py"): {"P_CodeFunction": {cf.make_uuid(1)}}}
    deleted, failures = cr.reconcile_walked_file_rows(
        [(coll, "file_path")], walked, project_name="P",
        primary_sources=cr.primary_sources_for(tmp_path),
        deleter=cr.delete_file_rows_exact, narrow_max_paths=0,
    )
    assert (deleted, failures) == (1, 0)
    assert coll.deleted == [cf.make_uuid(2)]
