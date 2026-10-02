# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 — an incremental code-graph sync removes the entities of files
deleted (or renamed away) since the last indexed commit.

Before: ``--incremental --since-commit A`` (the extra-path Sync button and the
automatic extra-path re-index) only re-walked CHANGED files; a file deleted in
``A..HEAD`` kept its Module/Class/Function/API/Interaction rows forever.

Covers (real git repos in tmp_path, fake Weaviate collections — no service):
  * act: deleted file → its rows removed from all five collections;
  * act: renamed file → old path's rows removed, new path in the walk set;
  * leave-alone: unchanged files; another project's row; another source
    root's (extra path's) row with the same relative path; a legacy row with
    no project_source; a file deleted in git but present on disk;
  * failure: a failing delete / an uncomputable diff is counted, folds into
    ``_prune_failures``, and the extra-path refresh records NO commit;
  * wiring: ``analyze_repository(incremental=True)`` runs the prune once per
    source root (primary AND ``--extra-path``) and reports the counts.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from vco_lib import codegraph_deleted_files as cdf
from vco_lib import codegraph_extras_refresh as cer

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ANALYZER_PATH = _REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git required")


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_v02100_deleted_prune_acg", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── git helpers ──────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


def _repo(root: Path, files: dict) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD")


# ── Weaviate fakes ───────────────────────────────────────────────────────────


class _Obj:
    def __init__(self, uuid, props):
        self.uuid = uuid
        self.properties = props


class _Data:
    def __init__(self):
        self.deleted: list = []
        self.fail_uuids: set = set()

    def delete_by_id(self, uuid):
        if uuid in self.fail_uuids:
            raise RuntimeError(f"simulated delete failure for {uuid}")
        self.deleted.append(uuid)


class _Coll:
    def __init__(self, name, anchor, rows):
        self.name = name
        self._rows = [_Obj(u, p) for u, p in rows]
        self.data = _Data()
        props = [types.SimpleNamespace(name=n) for n in (anchor, "project", "project_source")]
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(properties=props))

    def iterator(self, return_properties=None):
        return iter(self._rows)


_ANCHORS = {
    "modules_collection": "path",
    "functions_collection": "file_path",
    "classes_collection": "file_path",
    "apis_collection": "file_path",
    "interactions_collection": "file_path",
}


def _stub(analyzer_mod, rows_by_attr: dict, project="Acme"):
    """Bare analyzer with fake collections; rows_by_attr: attr -> [(uuid, props)]."""
    inst = analyzer_mod.CodeGraphAnalyzer.__new__(analyzer_mod.CodeGraphAnalyzer)
    inst.project_name = project
    inst.client = None
    inst._prune_failures = 0
    inst._git_deleted_report = analyzer_mod._PruneReport()
    for attr, anchor in _ANCHORS.items():
        setattr(inst, attr, _Coll(f"{project}_{attr}", anchor, rows_by_attr.get(attr, [])))
    return inst


def _row(uuid, anchor, path, source, project="Acme"):
    return (uuid, {anchor: path, "project": project, "project_source": source})


def _all_deleted(inst) -> set:
    out: set = set()
    for attr in _ANCHORS:
        out |= set(getattr(inst, attr).data.deleted)
    return out


# ── act: deleted file ────────────────────────────────────────────────────────


def test_deleted_file_rows_removed_from_all_five_collections(analyzer_mod, tmp_path):
    root = tmp_path / "repo"
    base = _repo(root, {"keep.py": "a = 1\n", "gone.py": "b = 1\n"})
    _git(root, "rm", "-q", "gone.py")
    _git(root, "commit", "-qm", "delete gone")
    src = root.as_posix()
    rows = {attr: [_row(f"{attr}-gone", a, "gone.py", src), _row(f"{attr}-keep", a, "keep.py", src)]
            for attr, a in _ANCHORS.items()}
    inst = _stub(analyzer_mod, rows)

    inst._prune_git_deleted(root, base)

    assert _all_deleted(inst) == {f"{attr}-gone" for attr in _ANCHORS}
    rep = inst._git_deleted_report
    assert (rep.files, rep.deleted, rep.failures) == (1, 5, 0)
    assert inst._prune_failures == 0


# ── act: renamed file ────────────────────────────────────────────────────────


def test_renamed_file_old_path_removed_new_path_walked(analyzer_mod, tmp_path):
    root = tmp_path / "repo"
    body = "def f():\n    return 1\n" * 20  # big enough for rename detection
    base = _repo(root, {"pkg/old_name.py": body, "keep.py": "a = 1\n"})
    _git(root, "mv", "pkg/old_name.py", "pkg/new_name.py")
    _git(root, "commit", "-qm", "rename")
    assert any(line.startswith("R")
               for line in _git(root, "diff", "--name-status", "-M", base, "HEAD").splitlines())
    src = root.as_posix()
    inst = _stub(analyzer_mod, {
        "functions_collection": [_row("old", "file_path", "pkg/old_name.py", src),
                                 _row("keep", "file_path", "keep.py", src)],
        "modules_collection": [_row("old-mod", "path", "pkg/old_name.py", src)],
    })

    inst._prune_git_deleted(root, base)

    assert _all_deleted(inst) == {"old", "old-mod"}
    # The new path is in the incremental walk set (it gets indexed).
    walked = inst._filter_changed_files(root, [root / "pkg/new_name.py", root / "keep.py"],
                                        since_commit=base)
    assert walked == [root / "pkg/new_name.py"]


# ── leave-alone ──────────────────────────────────────────────────────────────


def test_same_relative_path_of_other_project_other_root_and_legacy_rows_survive(
    analyzer_mod, tmp_path,
):
    root = tmp_path / "extra_clone"
    base = _repo(root, {"lib/util.py": "x = 1\n"})
    _git(root, "rm", "-q", "lib/util.py")
    _git(root, "commit", "-qm", "delete util")
    mine = root.as_posix()
    other_root = (tmp_path / "primary").as_posix()
    inst = _stub(analyzer_mod, {"functions_collection": [
        _row("mine", "file_path", "lib/util.py", mine),
        _row("other-root", "file_path", "lib/util.py", other_root),
        _row("other-project", "file_path", "lib/util.py", mine, project="Other"),
        _row("legacy-no-source", "file_path", "lib/util.py", ""),
        _row("prefix-sibling", "file_path", "lib/util.py.bak", mine),
    ]})

    inst._prune_git_deleted(root, base)

    assert inst.functions_collection.data.deleted == ["mine"]


def test_file_deleted_in_git_but_present_on_disk_is_kept(analyzer_mod, tmp_path):
    root = tmp_path / "repo"
    base = _repo(root, {"back.py": "a = 1\n"})
    _git(root, "rm", "-q", "--cached", "back.py")
    _git(root, "commit", "-qm", "untrack")
    assert (root / "back.py").exists()
    inst = _stub(analyzer_mod, {"functions_collection": [
        _row("back", "file_path", "back.py", root.as_posix())]})

    inst._prune_git_deleted(root, base)

    assert _all_deleted(inst) == set()
    assert inst._git_deleted_report.files == 0


def test_unchanged_history_and_non_git_root_prune_nothing(analyzer_mod, tmp_path):
    root = tmp_path / "repo"
    head = _repo(root, {"a.py": "a = 1\n"})
    plain = tmp_path / "plain"
    plain.mkdir()
    rows = {"functions_collection": [_row("a", "file_path", "a.py", root.as_posix())]}
    inst = _stub(analyzer_mod, rows)

    inst._prune_git_deleted(root, head)        # nothing deleted since HEAD
    inst._prune_git_deleted(plain, None)       # no .git → full-walk path owns it
    inst._prune_git_deleted(root, "deadbeef")  # unknown base → full-walk fallback

    assert _all_deleted(inst) == set()
    assert inst._git_deleted_report.failures == 0


# ── failure ──────────────────────────────────────────────────────────────────


def test_failing_delete_is_counted_and_folds_into_prune_failures(analyzer_mod, tmp_path):
    root = tmp_path / "repo"
    base = _repo(root, {"gone.py": "b = 1\n", "keep.py": "a = 1\n"})
    _git(root, "rm", "-q", "gone.py")
    _git(root, "commit", "-qm", "delete")
    inst = _stub(analyzer_mod, {"functions_collection": [
        _row("gone", "file_path", "gone.py", root.as_posix())]})
    inst.functions_collection.data.fail_uuids = {"gone"}

    inst._prune_git_deleted(root, base)

    assert inst._git_deleted_report.failures == 1
    assert inst._prune_failures == 1


def test_uncomputable_diff_is_one_failure(tmp_path):
    root = tmp_path / "repo"
    _repo(root, {"a.py": "a = 1\n"})

    def _run(argv, **kw):
        if "diff" in argv:
            return types.SimpleNamespace(returncode=128, stdout="", stderr="fatal: boom")
        return subprocess.run(argv, **kw)

    rep = cdf.prune_git_deleted_files([], root, "HEAD", project="Acme",
                                      deleter=lambda *a, **k: (0, 0), run=_run)
    assert rep.failures == 1


def test_collection_scan_error_is_a_failure_not_a_clean_prune():
    class _Boom:
        name = "Acme_CodeFunction"

    def _deleter(*_a, **_k):
        raise RuntimeError("scan failed")

    assert cdf.prune_file_rows([(_Boom(), "file_path")], ["x.py"], project="Acme",
                               project_source="/r", deleter=_deleter,
                               log_prefix="t") == (0, 1)


def _stub_analyzer(path: Path, *, exit_code: int, prune_failures: int, deleted: int) -> None:
    path.write_text(textwrap.dedent(f"""\
        import json, sys
        print(json.dumps({{"final": True, "files_analyzed": 1, "modules": 1,
                          "classes": 0, "functions": 1, "apis": 0, "insert_errors": 0,
                          "deleted_files": 1, "deleted_file_entities": {deleted},
                          "deleted_file_prune_failures": {prune_failures}}}))
        sys.exit({exit_code})
    """))


@pytest.mark.parametrize("exit_code,prune_failures", [(5, 1), (0, 1)])
def test_refresh_does_not_record_when_the_deleted_file_prune_failed(
    tmp_path, exit_code, prune_failures,
):
    clone = tmp_path / "clone"
    first = _repo(clone, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    _git(clone, "rm", "-q", "b.py")
    _git(clone, "commit", "-qm", "delete b")
    stub = tmp_path / "analyze.py"
    _stub_analyzer(stub, exit_code=exit_code, prune_failures=prune_failures, deleted=0)
    recorded: list = []
    lines: list = []
    cfg = types.SimpleNamespace(
        project_id="pid", code_graph_collection_prefix="Acme",
        code_graph_extra_paths=(types.SimpleNamespace(
            path=str(clone), enabled=True, last_indexed_commit=first),),
    )
    out = cer.refresh_extras(
        str(tmp_path), str(stub), sys.executable, tmp_path / "state", lines.append,
        deps=cer.Deps(resolve=lambda _r: cfg,
                      record=lambda *a: (recorded.append(a) or (True, "recorded"))),
    )
    assert out == {str(clone): "analyzer_failed"}, lines
    assert recorded == []
    assert any("last_indexed_commit unchanged" in ln for ln in lines), lines


def test_refresh_log_reports_the_deleted_file_counts(tmp_path):
    clone = tmp_path / "clone"
    first = _repo(clone, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    _git(clone, "rm", "-q", "b.py")
    _git(clone, "commit", "-qm", "delete b")
    stub = tmp_path / "analyze.py"
    _stub_analyzer(stub, exit_code=0, prune_failures=0, deleted=7)
    lines: list = []
    cfg = types.SimpleNamespace(
        project_id="pid", code_graph_collection_prefix="Acme",
        code_graph_extra_paths=(types.SimpleNamespace(
            path=str(clone), enabled=True, last_indexed_commit=first),),
    )
    out = cer.refresh_extras(
        str(tmp_path), str(stub), sys.executable, tmp_path / "state", lines.append,
        deps=cer.Deps(resolve=lambda _r: cfg, record=lambda *a: (True, "recorded")),
    )
    assert out == {str(clone): "recorded"}
    assert any("deleted_files=1 removed_entities=7" in ln for ln in lines), lines


# ── wiring: the incremental walk runs the prune per source root ──────────────


def test_incremental_walk_prunes_primary_and_extra_roots(analyzer_mod, tmp_path):
    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    # Only non-source files, so no finder dispatches anything: the walk is the
    # prune step alone (plus the soft-failing post-walk passes).
    p_base = _repo(primary, {"notes.txt": "x\n", "old.txt": "y\n"})
    e_base = _repo(extra, {"readme.txt": "x\n", "dead.txt": "y\n"})
    _git(primary, "rm", "-q", "old.txt")
    _git(primary, "commit", "-qm", "d")
    _git(extra, "rm", "-q", "dead.txt")
    _git(extra, "commit", "-qm", "d")
    assert p_base and e_base
    inst = _stub(analyzer_mod, {"functions_collection": [
        _row("p-old", "file_path", "old.txt", primary.resolve().as_posix()),
        _row("e-dead", "file_path", "dead.txt", extra.resolve().as_posix()),
        _row("e-old-wrong-root", "file_path", "old.txt", extra.resolve().as_posix()),
    ]})
    inst._get_stale_file_set = lambda: None
    inst._progress_emitter = None
    inst.index_dot_claude = False

    # since_commit=None → HEAD~1..HEAD per root (each root has exactly 2 commits).
    stats = inst.analyze_repository(primary.resolve(), incremental=True,
                                     extra_paths=[extra])

    assert sorted(inst.functions_collection.data.deleted) == ["e-dead", "p-old"]
    assert (stats["deleted_files"], stats["deleted_file_entities"],
            stats["deleted_file_prune_failures"]) == (2, 2, 0)


def test_non_incremental_walk_does_not_run_the_git_prune(analyzer_mod, tmp_path):
    primary = tmp_path / "primary"
    _repo(primary, {"notes.txt": "x\n", "old.txt": "y\n"})
    _git(primary, "rm", "-q", "old.txt")
    _git(primary, "commit", "-qm", "d")
    inst = _stub(analyzer_mod, {})
    inst._get_stale_file_set = lambda: None
    inst._progress_emitter = None
    inst.index_dot_claude = False
    calls: list = []
    inst._prune_git_deleted = lambda root, since: calls.append(root)

    stats = inst.analyze_repository(primary.resolve(), incremental=False)

    assert calls == []
    assert stats["deleted_files"] == 0



# ── analyzer exit status: --since-commit callers must not advance ────────────


def _run_main_with_stats(tmp_path: Path, stats: dict, extra_argv: list):
    from tests.common.child_env import child_env

    repo = tmp_path / "repo"
    _repo(repo, {"a.txt": "x\n"})
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(f"""\
        import sys, importlib.util
        spec = importlib.util.spec_from_file_location("acg", r"{_ANALYZER_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        C = mod.CodeGraphAnalyzer
        C.connect = lambda self: (setattr(self, "client", object()) or True)
        C.close = lambda self: None
        C.create_collections = lambda self, force=False, *, repo_path=None: None
        C.create_cross_references = lambda self, changed_files=None: {{"calls": 0, "extends": 0, "imports": 0}}
        C.analyze_repository = lambda self, *a, **kw: dict({stats!r})
        class _Svc:
            code_vector_slot = "codesage_embed"; code_model_id = "m"; code_dim = 8
            text_vector_slot = "qwen3_embed"; text_model_id = "t"; text_dim = 8
            def code_backend_ready(self): return True
            def text_backend_ready(self): return True
            def close(self): return None
        mod.EmbeddingService.for_project = classmethod(lambda cls, *a, **kw: _Svc())
        sys.argv = ["analyze_code_graph.py", r"{repo}", "--project", "Acme"] + {extra_argv!r}
        sys.exit(mod.main())
    """))
    return subprocess.run([sys.executable, str(runner)], capture_output=True, text=True,
                          timeout=60, env=child_env(VCT_JOERN_AVAILABLE="0"))


_BASE_STATS = {"modules": 1, "classes": 0, "functions": 1, "apis": 0, "files_analyzed": 1,
               "files_skipped": 0, "insert_errors": 0, "stale_pruned": 0,
               "prune_failures": 1, "chunk_plan_failures": 0, "deleted_files": 1,
               "deleted_file_entities": 0, "deleted_file_prune_failures": 1}


def test_main_exits_5_when_a_since_commit_run_failed_its_deleted_file_prune(tmp_path):
    proc = _run_main_with_stats(tmp_path, _BASE_STATS,
                                ["--incremental", "--since-commit", "HEAD", "--json-progress"])
    assert proc.returncode == 5, (proc.stdout[-2000:], proc.stderr[-2000:])
    final = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith('{"final"')]
    assert final and final[-1]["deleted_file_prune_failures"] == 1


def test_main_keeps_exit_0_without_since_commit_and_when_the_prune_succeeded(tmp_path):
    # No --since-commit: nobody advances a baseline → the PRUNE_FAILURES partial contract.
    a = _run_main_with_stats(tmp_path / "a", _BASE_STATS, ["--incremental"])
    assert a.returncode == 0, a.stderr[-2000:]
    assert "PRUNE_FAILURES=1" in a.stdout
    # --since-commit with a clean prune → exit 0 (the commit may be recorded).
    ok = dict(_BASE_STATS, prune_failures=0, deleted_file_prune_failures=0)
    b = _run_main_with_stats(tmp_path / "b", ok, ["--incremental", "--since-commit", "HEAD"])
    assert b.returncode == 0, b.stderr[-2000:]
    assert "Deleted files pruned: 1" in b.stdout


# ── --as-extra-path: never delete a row not provably the extra path's ────────


def _rev_rows(analyzer_mod, extra_src: str):
    cur = analyzer_mod.CODEGRAPH_EMBED_REVISION
    return [
        # Legacy PRIMARY rows (no project_source) whose files are not under the
        # extra repo: one current-revision (the deleted-file sweep's target),
        # one stale (the orphan-clear's target).
        ("legacy-current", {"file_path": "src/only_in_primary.py", "project": "Acme",
                            "project_source": "", "embed_revision": cur}),
        ("legacy-stale", {"file_path": "src/also_primary.py", "project": "Acme",
                          "project_source": "", "embed_revision": 0}),
        # Provably the extra path's: a deleted file, and a live one.
        ("extra-gone", {"file_path": "gone.py", "project": "Acme",
                        "project_source": extra_src, "embed_revision": cur}),
        ("extra-live", {"file_path": "readme.txt", "project": "Acme",
                        "project_source": extra_src, "embed_revision": cur}),
    ]


def _walk_extra(analyzer_mod, tmp_path, *, as_extra_path: bool):
    extra = (tmp_path / "extra_clone")
    _repo(extra, {"readme.txt": "x\n"})
    extra = extra.resolve()
    inst = _stub(analyzer_mod, {"functions_collection": _rev_rows(analyzer_mod, extra.as_posix())})
    for attr in _ANCHORS:  # the sweep reads embed_revision too
        coll = getattr(inst, attr)
        props = [types.SimpleNamespace(name=n) for n in (
            _ANCHORS[attr], "project", "project_source", "embed_revision")]
        coll.config = types.SimpleNamespace(get=lambda _p=props: types.SimpleNamespace(properties=_p))
    inst._progress_emitter = None
    inst.index_dot_claude = False
    inst.analyze_repository(extra, as_extra_path=as_extra_path)
    return set(inst.functions_collection.data.deleted)


def test_extra_path_sync_leaves_legacy_primary_rows_alone(analyzer_mod, tmp_path):
    deleted = _walk_extra(analyzer_mod, tmp_path, as_extra_path=True)
    assert "legacy-current" not in deleted and "legacy-stale" not in deleted  # leave-alone
    assert "extra-gone" in deleted                                           # act
    assert "extra-live" not in deleted


def test_without_the_signal_the_same_walk_would_judge_legacy_rows(analyzer_mod, tmp_path):
    """Pins WHY the signal is needed: a plain walk of the extra path treats an
    unstamped row as its own and deletes the primary's legacy rows."""
    deleted = _walk_extra(analyzer_mod, tmp_path, as_extra_path=False)
    assert {"legacy-current", "legacy-stale", "extra-gone"} <= deleted


def test_entity_reconcile_strict_skips_unstamped_rows():
    from vco_lib.codegraph_resync import delete_file_rows_exact, reconcile_walked_file_rows

    src = "/srv/extra"
    coll = _Coll("Acme_CodeFunction", "file_path", [
        _row("legacy", "file_path", "a.py", ""),
        _row("stamped-old", "file_path", "a.py", src),
        _row("stamped-kept", "file_path", "a.py", src),
    ])
    for strict, want in ((True, ["stamped-old"]), (False, ["legacy", "stamped-old"])):
        coll.data.deleted = []
        reconcile_walked_file_rows(
            [(coll, "file_path")],
            {(src, "a.py"): {"Acme_CodeFunction": {"stamped-kept"}}},
            project_name="Acme", primary_sources={src}, deleter=delete_file_rows_exact,
            strict_source=strict,
        )
        assert sorted(coll.data.deleted) == want, strict


def test_source_is_owned_rule():
    from vco_lib.codegraph_row_classify import source_is_owned

    assert source_is_owned("", {"/a"}) is True
    assert source_is_owned("", {"/a"}, strict=True) is False
    assert source_is_owned("/a", {"/a"}, strict=True) is True
    assert source_is_owned("/b", {"/a"}) is False
    assert source_is_owned("/a", None, strict=True) is False


def test_as_extra_path_flag_reaches_analyze_repository(tmp_path):
    """The CLI flag the Sync argv carries is accepted and threaded through."""
    from tests.common.child_env import child_env

    repo = tmp_path / "repo"
    _repo(repo, {"a.txt": "x\n"})
    runner = tmp_path / "runner.py"
    runner.write_text(textwrap.dedent(f"""\
        import sys, importlib.util
        spec = importlib.util.spec_from_file_location("acg", r"{_ANALYZER_PATH}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        C = mod.CodeGraphAnalyzer
        C.connect = lambda self: (setattr(self, "client", object()) or True)
        C.close = lambda self: None
        C.create_collections = lambda self, force=False, *, repo_path=None: None
        C.create_cross_references = lambda self, changed_files=None: {{"calls": 0, "extends": 0, "imports": 0}}
        def _ar(self, *a, **kw):
            print("AS_EXTRA_PATH=" + str(kw.get("as_extra_path")))
            return {{"files_analyzed": 1, "files_skipped": 0, "modules": 0, "classes": 0,
                    "functions": 0, "apis": 0, "insert_errors": 0}}
        C.analyze_repository = _ar
        class _Svc:
            code_vector_slot = "codesage_embed"; code_model_id = "m"; code_dim = 8
            text_vector_slot = "qwen3_embed"; text_model_id = "t"; text_dim = 8
            def code_backend_ready(self): return True
            def text_backend_ready(self): return True
            def close(self): return None
        mod.EmbeddingService.for_project = classmethod(lambda cls, *a, **kw: _Svc())
        sys.argv = ["analyze_code_graph.py"] + sys.argv[1:]
        sys.exit(mod.main())
    """))
    argv = cer.build_extra_sync_argv(str(repo), "Acme", False, None)
    proc = subprocess.run([sys.executable, str(runner), *argv], capture_output=True,
                          text=True, timeout=60, env=child_env(VCT_JOERN_AVAILABLE="0"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "AS_EXTRA_PATH=True" in proc.stdout


@pytest.mark.parametrize("flag", [True, False])
def test_walk_hands_the_signal_to_the_entity_reconcile(analyzer_mod, tmp_path, monkeypatch, flag):
    extra = tmp_path / "extra_clone"
    _repo(extra, {"readme.txt": "x\n"})
    seen: list = []

    def _spy(*a, **kw):
        seen.append(kw.get("strict_source"))
        return (0, 0)

    monkeypatch.setattr(analyzer_mod, "_reconcile_walked_file_rows", _spy)
    inst = _stub(analyzer_mod, {})
    inst._get_stale_file_set = lambda: None
    inst._progress_emitter = None
    inst.index_dot_claude = False
    inst.analyze_repository(extra.resolve(), as_extra_path=flag)
    assert seen == [flag]
