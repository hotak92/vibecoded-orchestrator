# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 fix round of CG-3 (WAVE2-CHECK M-2, L-1, L-2 and the A-011 inbound-edge check).

* M-2 — a project folder NESTED inside a larger git repository is the main
  checkout, not a linked worktree: its catch-up indexes the project's own
  files, and the daily path reconciliation judges its rows against the
  project folder, never the repository toplevel. A real linked worktree is
  still skipped.
* L-1 — the reconciliation keeps the rows of a file that HEAD still tracks
  and that is missing only from the working tree; a committed deletion and a
  never-committed file are removed; a work tree whose committed tree cannot
  be read deletes nothing.
* L-2 — the per-checkout lock records its holder's start token, and a lock
  whose pid was reused by another process is broken.
* A-011 — a scoped run that rewrites a callee's file keeps the callee's
  inbound count from unchanged callers and their stored ``calls`` edges, and
  a rewritten caller's old edges are subtracted (not counted twice).

No Weaviate: in-memory collections and real scratch git repositories only.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from vco_lib import codegraph_guards as guards
from vco_lib import codegraph_head_catchup as chc
from vco_lib import codegraph_path_reconcile as cpr
from vco_lib import codegraph_root_lock as root_lock
from vco_lib import git_meta

REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(not _GIT, reason="git required")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _write(path: Path, text: str = "x = 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _mono(tmp_path: Path) -> "tuple[Path, Path]":
    """A repository ``mono`` holding a project folder ``mono/svc`` (no .git of
    its own) and an unrelated sibling folder."""
    mono = tmp_path / "mono"
    _write(mono / "svc" / "pkg" / "a.py")
    _write(mono / "svc" / "pkg" / "keep.py")
    _write(mono / "other" / "b.py")
    _git(mono, "init", "-q", "-b", "main")
    _git(mono, "add", ".")
    _git(mono, "commit", "-q", "-m", "base")
    return mono, mono / "svc"


# ── an in-memory collection (cursor pages, filtered reads, replace) ──────


@dataclass
class _Obj:
    uuid: str
    properties: dict
    references: Optional[dict] = None
    vector: Optional[dict] = None


class _Coll:
    """Rows keyed by uuid. ``data.replace`` overwrites a row's properties AND
    drops its own references (Weaviate's replace semantics); ``query`` serves
    cursor pages, ``Filter.equal`` reads and point reads."""

    def __init__(self, name: str, rows: Optional[dict] = None,
                 refs: Optional[dict] = None, declared=()):
        self.name = name
        self.rows = {k: dict(v) for k, v in (rows or {}).items()}
        self.refs = {k: list(v) for k, v in (refs or {}).items()}
        self.deleted: list = []
        self.updates: list = []
        self.replaced: list = []
        self.ref_adds: list = []
        coll = self

        class _Q:
            def fetch_objects(self, *, limit=None, after=None, offset=0, filters=None,
                              return_properties=None, return_references=None, **kw):
                keys = sorted(coll.rows)
                if filters is not None:
                    keys = [k for k in keys if coll.rows[k].get(filters.target) == filters.value]
                    keys = keys[offset or 0:]
                elif after is not None:
                    keys = [k for k in keys if k > str(after)]
                keys = keys[:limit or len(keys)]
                return types.SimpleNamespace(objects=[coll._obj(k, return_properties, return_references)
                                                      for k in keys])

            def fetch_object_by_id(self, uuid, return_properties=None, **kw):
                return coll._obj(uuid, return_properties, None) if uuid in coll.rows else None

        class _D:
            def delete_by_id(self, *, uuid):
                coll.deleted.append(uuid)
                coll.rows.pop(uuid, None)

            def update(self, *, uuid, properties):
                coll.updates.append({"uuid": uuid, "properties": dict(properties)})
                coll.rows.setdefault(uuid, {}).update(properties)

            def replace(self, *, uuid, properties=None, vector=None, references=None, **kw):
                coll.replaced.append(uuid)
                coll.rows[uuid] = dict(properties or {})
                coll.refs.pop(uuid, None)

            def insert(self, *, uuid, properties=None, **kw):
                coll.rows[uuid] = dict(properties or {})

            def reference_add(self, *, from_uuid, from_property, to):
                coll.ref_adds.append((from_uuid, from_property, to))
                coll.refs.setdefault(from_uuid, []).append(str(to))

        self.query = _Q()
        self.data = _D()
        self.config = types.SimpleNamespace(get=lambda: types.SimpleNamespace(
            properties=[types.SimpleNamespace(name=n) for n in declared]))

    def _obj(self, uid, props, references):
        p = self.rows[uid]
        out = {k: p.get(k) for k in props} if props else dict(p)
        refs = None
        if references is not None:
            refs = {"calls": types.SimpleNamespace(objects=[
                types.SimpleNamespace(uuid=t, properties={}) for t in self.refs.get(uid, [])])}
        return _Obj(uid, out, refs)


# ── M-2: nested project vs real linked worktree ─────────────────────────


@needs_git
def test_the_worktree_rule_is_git_dir_versus_common_dir(tmp_path: Path) -> None:
    mono, svc = _mono(tmp_path)
    assert git_meta.linked_worktree_main(mono) is None, "main checkout"
    assert git_meta.linked_worktree_main(svc) is None, "a nested project folder is NOT a worktree"
    wt = tmp_path / "lane"
    _git(mono, "worktree", "add", "-q", "-b", "lane", str(wt))
    assert git_meta.linked_worktree_main(wt) == mono.resolve()
    assert git_meta.linked_worktree_main(wt / "svc") == mono.resolve(), \
        "a folder inside a linked worktree is still that worktree"
    assert git_meta.linked_worktree_main(tmp_path / "nowhere") is None


class _Hub:
    def __init__(self, recorded: str):
        self.recorded = recorded
        self.runs: list = []
        self.reconciles: list = []

    def resolve(self, _root):
        return types.SimpleNamespace(project_id="pid-1", code_graph_collection_prefix="P",
                                     code_graph_last_analyzed_commit=self.recorded,
                                     code_graph_bound=True)

    def record(self, _pid, commit):
        self.recorded = commit
        return True, "recorded"

    def run_analyzer(self, _py, _an, argv, _timeout):
        files = []
        if "--only-files-from" in argv:
            files = Path(argv[argv.index("--only-files-from") + 1]).read_text().split()
        self.runs.append({"argv": list(argv), "files": files})
        return chc.AnalyzerResult(True, "ok", len(files), 1, 1)


def _catchup(root: Path, hub: _Hub, reconcile) -> "tuple[str, list]":
    lines: list = []
    state = root / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    deps = chc.Deps(resolve=hub.resolve, run_analyzer=hub.run_analyzer, record=hub.record,
                    reconcile=reconcile)
    return chc.catchup(str(root), "/x/analyze.py", "/x/python", state, lines.append,
                       deps=deps), lines


@needs_git
def test_a_nested_project_is_indexed_with_its_own_files(tmp_path: Path) -> None:
    """ACT. RED before the fix: the toplevel (``mono``) differed from the
    project folder, the catch-up returned ``worktree_session`` and the graph
    froze. The diff is also scoped to the project folder and relative to it
    (a change in the sibling folder is not handed to the analyzer)."""
    mono, svc = _mono(tmp_path)
    base = _git(mono, "rev-parse", "HEAD")
    _write(svc / "pkg" / "a.py", "x = 2\n")
    _write(mono / "other" / "b.py", "y = 2\n")
    _git(mono, "commit", "-q", "-am", "edit both")
    hub = _Hub(recorded=base)
    seen: list = []
    out, lines = _catchup(svc, hub, lambda root, *_a: seen.append(root) or "not_due")
    assert out == "recorded", lines
    assert len(hub.runs) == 1 and hub.runs[0]["files"] == [str(svc / "pkg" / "a.py")]
    assert hub.recorded == _git(mono, "rev-parse", "HEAD")
    assert seen == [str(svc)], "the reconciliation gets the PROJECT folder, not the toplevel"


@needs_git
def test_a_real_linked_worktree_is_still_skipped(tmp_path: Path) -> None:
    """LEAVE-ALONE: a session in a linked worktree indexes nothing and runs no
    reconciliation (also a project folder inside that worktree)."""
    mono, _svc = _mono(tmp_path)
    wt = tmp_path / "lane"
    _git(mono, "worktree", "add", "-q", "-b", "lane", str(wt))
    _write(wt / "svc" / "pkg" / "a.py", "x = 3\n")
    _git(wt, "commit", "-q", "-am", "lane edit")
    for root in (wt, wt / "svc"):
        hub = _Hub(recorded=_git(mono, "rev-parse", "HEAD"))
        seen: list = []
        out, lines = _catchup(root, hub, lambda r, *_a: seen.append(r) or "not_due")
        assert out == "worktree_session", lines
        assert hub.runs == [] and seen == []


def _fn_rows(paths) -> dict:
    return {f"00000000-0000-0000-0000-{i:012d}": {"file_path": p, "project_source": ""}
            for i, p in enumerate(paths, start=1)}


@needs_git
def test_the_reconciliation_judges_a_nested_project_against_its_own_folder(tmp_path: Path) -> None:
    """ACT + LEAVE-ALONE on the real catch-up → reconcile wiring: the rows of
    the nested project (paths relative to ``svc``) keep the live file and
    lose the one that was never committed and is gone. RED before the fix
    (the reconcile was handed the toplevel): every row's path was missing
    under ``mono`` and the run deleted nothing (``all_paths_missing``)."""
    mono, svc = _mono(tmp_path)
    funcs = _Coll("P_CodeFunction", _fn_rows(["pkg/a.py", "pkg/scratch.py"]))
    hub = _Hub(recorded=_git(mono, "rev-parse", "HEAD"))
    outcomes: list = []

    def _reconcile(root, prefix, state_dir, log):
        res = cpr.reconcile_collections([(funcs, "file_path")], Path(root))
        outcomes.append(res.outcome)
        return res.outcome

    out, lines = _catchup(svc, hub, _reconcile)
    assert out == "up_to_date", lines
    assert outcomes == ["ok"]
    assert [r["file_path"] for r in funcs.rows.values()] == ["pkg/a.py"]


# ── L-1: committed but missing from the working tree ────────────────────


@needs_git
def test_a_tracked_file_missing_only_from_the_working_tree_keeps_its_rows(tmp_path: Path) -> None:
    """LEAVE-ALONE (RED before the fix: deleted on disk → rows deleted, and
    nothing restored them after ``git restore``) plus ACT for a committed
    deletion and a never-committed file."""
    root = tmp_path / "proj"
    _write(root / "src" / "live.py")
    _write(root / "src" / "uncommitted_delete.py")
    _write(root / "src" / "committed_delete.py")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    (root / "src" / "uncommitted_delete.py").unlink()            # not committed
    _git(root, "rm", "-q", "src/committed_delete.py")
    _git(root, "commit", "-q", "-m", "rm")                       # committed
    funcs = _Coll("P_CodeFunction", _fn_rows([
        "src/live.py", "src/uncommitted_delete.py", "src/committed_delete.py",
        "src/never_committed.py"]))
    res = cpr.reconcile_collections([(funcs, "file_path")], root)
    assert res.outcome == "ok" and res.kept_committed == 1
    assert sorted(r["file_path"] for r in funcs.rows.values()) == [
        "src/live.py", "src/uncommitted_delete.py"]
    assert "kept_committed=1" in res.line()


def test_a_work_tree_whose_commits_cannot_be_read_deletes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root / "src" / "live.py")
    funcs = _Coll("P_CodeFunction", _fn_rows(["src/live.py", "src/gone.py"]))
    res = cpr.reconcile_collections([(funcs, "file_path")], root,
                                    committed=lambda _r: (cpr.GIT_UNKNOWN, None))
    assert res.outcome == cpr.GIT_UNKNOWN and funcs.deleted == []


def test_a_folder_outside_git_keeps_the_disk_rule(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root / "src" / "live.py")
    funcs = _Coll("P_CodeFunction", _fn_rows(["src/live.py", "src/gone.py"]))
    res = cpr.reconcile_collections([(funcs, "file_path")], root,
                                    committed=lambda _r: (cpr.NOT_GIT, None))
    assert res.outcome == "ok" and len(funcs.deleted) == 1


@needs_git
def test_committed_tree_reads_a_nested_subtree_relative_to_it(tmp_path: Path) -> None:
    mono, svc = _mono(tmp_path)
    verdict, paths = cpr.committed_tree(svc)
    assert verdict == "git" and paths == {"pkg/a.py", "pkg/keep.py"}
    assert cpr.committed_tree(tmp_path / "not-a-repo-at-all")[0] in (cpr.NOT_GIT, cpr.GIT_UNKNOWN)


# ── L-2: the lock does not trust a bare pid ──────────────────────────────


def _held(state: Path, pid: str, token: Optional[str]) -> Path:
    lock = root_lock.lock_path(state, "k")
    lock.mkdir(parents=True)
    (lock / "pid").write_text(pid)
    if token is not None:
        (lock / "token").write_text(token)
    return lock


@pytest.mark.parametrize("token,reused,breaks", [
    ("proc:111", True, True),      # the pid now names another process → broken
    ("proc:111", False, False),    # same instance → kept
    (None, True, False),           # no token recorded → cannot tell → kept
])
def test_a_live_pid_is_broken_only_when_its_start_token_proves_reuse(
        tmp_path: Path, token, reused, breaks) -> None:
    state = tmp_path / "state"
    _held(state, "4242", token)
    got = root_lock.acquire(state, "k", alive=lambda _p: True,
                            reused=lambda _p, _t: reused, token=lambda _p: "proc:999")
    assert (got is not None) is breaks
    if got is not None:
        assert (got / "token").read_text() == "proc:999"
        root_lock.release(got)
        assert not got.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="proc: start tokens are Linux-only")
def test_the_real_identity_check_breaks_a_reused_pid_and_keeps_a_live_holder(tmp_path: Path) -> None:
    """The default seams (deferral_probes): our own live pid recorded with a
    WRONG start token reads as reused; with its real token, as the holder."""
    from vco_lib.deferral_probes import process_start_token

    state = tmp_path / "state"
    lock = _held(state, str(os.getpid()), "proc:1")
    assert root_lock.holder_is_stale(lock) is True
    (lock / "token").write_text(process_start_token(os.getpid()) or "")
    assert root_lock.holder_is_stale(lock) is False
    got = root_lock.acquire(tmp_path / "other", "k")
    assert got is not None and (got / "token").read_text() == process_start_token(os.getpid())
    root_lock.release(got)


# ── A-011: inbound edges from unchanged files survive a scoped run ──────

_ANALYZER_PATH = REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py"
_MCP_ROOT = REPO_ROOT / "claude_mcp_servers"


@pytest.fixture(scope="module")
def analyzer_mod() -> types.ModuleType:
    if str(_MCP_ROOT) not in sys.path:
        sys.path.insert(0, str(_MCP_ROOT))
    spec = importlib.util.spec_from_file_location("_fixcg_analyzer", str(_ANALYZER_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_DECLARED = ("full_name", "name", "file_path", "function_body", "call_names", "n_callers",
             "content_hash", "embed_revision", "total_chunks", "chunk_num", "language",
             "project_source")


def _fn(full: str, file_path: str, body: str, *, calls=None, n_callers=None) -> dict:
    return {"full_name": full, "name": full.rsplit(".", 1)[-1], "file_path": file_path,
            "function_body": body, "total_chunks": 1, "chunk_num": 0, "language": "python",
            "project_source": "", "call_names": calls, "n_callers": n_callers,
            "content_hash": "old-hash", "embed_revision": 0}


def _analyzer(analyzer_mod, funcs: _Coll):
    a = analyzer_mod.CodeGraphAnalyzer("P")
    a.client = object()
    a.functions_collection = funcs
    a.classes_collection = _Coll("P_CodeClass")
    a.modules_collection = _Coll("P_CodeModule")
    a.function_cache, a.class_cache, a.module_cache, a.module_imports = {}, {}, {}, {}
    a._populate_caches_from_weaviate = lambda: (_ for _ in ()).throw(
        AssertionError("a scoped run must not load the whole graph"))
    return a


def _rewrite(a, funcs: _Coll, uid: str, body: str) -> None:
    """What the walk does to a CHANGED function: the real write path, which
    finds a different content hash and replaces the row."""
    row = funcs.rows[uid]
    params = {"properties": {k: row[k] for k in ("full_name", "name", "file_path", "language",
                                                 "chunk_num", "total_chunks", "project_source")}
              | {"function_body": body}, "vector": [0.1]}
    a._write_one_object(funcs, uid, params, row["full_name"])
    assert uid in funcs.replaced, "the changed row is really rewritten"


def test_a_rewritten_callee_keeps_its_count_and_its_inbound_edges(analyzer_mod) -> None:
    """The review's targeted check. An UNCHANGED caller (a.py) calls
    ``target`` (t.py); t.py changes and is rewritten by a scoped run. RED
    before the fix: the replace nulled the callee's ``n_callers`` and the
    scoped delta (which only knows the changed files' edges) never restored it."""
    funcs = _Coll("P_CodeFunction", {
        "u-a": _fn("a.caller", "a.py", "def caller():\n    target()\n", calls=["target"]),
        "u-t": _fn("mod.target", "t.py", "def target():\n    return 1\n", calls=[], n_callers=1),
    }, refs={"u-a": ["u-t"]}, declared=_DECLARED)
    a = _analyzer(analyzer_mod, funcs)
    _rewrite(a, funcs, "u-t", "def target():\n    return 2\n")
    a.create_cross_references(changed_files={"t.py"})
    assert funcs.rows["u-t"].get("n_callers") == 1, "the inbound count survives"
    assert funcs.refs.get("u-a") == ["u-t"], "the unchanged caller's edge survives"
    assert "u-a" not in funcs.replaced and not [u for u in funcs.updates if u["uuid"] == "u-a"]


def test_a_rewritten_caller_moves_its_edges_without_double_counting(analyzer_mod) -> None:
    """The other half: the CHANGED file is the caller. Its old edge to
    ``target`` is subtracted from the stored count and its new edge added —
    RED before the fix: the replace nulled its ``call_names``, so the old edge
    was not subtracted and ``target`` read 2."""
    funcs = _Coll("P_CodeFunction", {
        "u-c": _fn("c.caller", "c.py", "def caller():\n    target()\n", calls=["target"]),
        "u-t": _fn("mod.target", "t.py", "def target():\n    return 1\n", calls=[], n_callers=1),
    }, refs={"u-c": ["u-t"]}, declared=_DECLARED)
    a = _analyzer(analyzer_mod, funcs)
    _rewrite(a, funcs, "u-c", "def caller():\n    x = 1\n    target()\n")
    a.create_cross_references(changed_files={"c.py"})
    assert funcs.rows["u-t"].get("n_callers") == 1
    assert funcs.refs.get("u-c") == ["u-t"], "the rewritten caller's edge is re-added once"


def test_carry_never_overrides_what_the_write_sets_and_never_writes_null() -> None:
    props = {"call_names": ["fresh"]}
    assert guards.carry_xref_props(props, {"call_names": ["old"], "n_callers": None}) == {}
    assert props == {"call_names": ["fresh"]}
    assert guards.carry_xref_props(props, None) == {}
    assert guards.xref_carry_props_for(_Coll("P_CodeClass"), {}) == ()
    assert guards.xref_carry_props_for(_Coll("P_CodeFunction", declared=("n_callers",)), {}) == (
        "n_callers",), "only declared props are read (an undeclared one fails the read)"
