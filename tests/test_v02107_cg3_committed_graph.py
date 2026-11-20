# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 CG-3 — the code graph follows the COMMITTED state of the main checkout.

Owner rulings 2026-11-17 / 2026-11-19: the graph follows the main checkout's
current branch only; edits in linked worktrees are never indexed (not under the
project, not into a collection of their own); one analyzer run per HEAD move;
deleted code must not survive in the graph.

Covered here (no Weaviate: in-memory collections and fakes only):

* A-012 — two worktrees edit one file → nothing is written until the merge
  moves the main checkout's HEAD; a catch-up started from a worktree is skipped
  before it even reads HEAD; the merge is indexed by ONE analyzer run and the
  next catch-up runs none.
* A-013 — the daily path-existence reconciliation deletes the rows of a file
  that is gone (act), and deletes NOTHING when the root is missing or empty,
  when every listed path is missing, or when a class cannot be read
  (leave-alone); extra-folder rows are never judged.
* the shared per-checkout lock breaks only a provably dead holder's lock.
* the retired drain-queue state files are removed; the live lock is not.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from vco_lib import atomic
from vco_lib import codegraph_head_catchup as chc
from vco_lib import codegraph_path_reconcile as cpr
from vco_lib import codegraph_root_lock as root_lock
from vco_lib import weaviate_helpers

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTE_LIB_SH = REPO_ROOT / "templates" / "hooks" / "_lib" / "route-touched-path.sh"
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"


# ── in-memory collection the shared cursor can page ──────────────────────


@dataclass
class _Obj:
    uuid: str
    properties: dict


@dataclass
class _Coll:
    name: str
    rows: "dict[str, dict]" = field(default_factory=dict)
    fail_pages: bool = False
    deleted: list = field(default_factory=list)
    reads: list = field(default_factory=list)

    def __post_init__(self):
        coll = self

        class _Q:
            def fetch_objects(self, *, limit, after=None, return_properties=None, **kw):
                coll.reads.append({"after": after, "props": return_properties, **kw})
                if coll.fail_pages:
                    raise RuntimeError("gRPC page failed")
                keys = sorted(k for k in coll.rows if after is None or k > str(after))
                page = keys[:limit]
                objs = [_Obj(k, {p: coll.rows[k].get(p) for p in (return_properties or coll.rows[k])})
                        for k in page]
                return SimpleNamespace(objects=objs)

        class _D:
            def delete_by_id(self, *, uuid):
                coll.deleted.append(uuid)
                coll.rows.pop(uuid, None)

        self.query = _Q()
        self.data = _D()

    def seed(self, uid: str, **props) -> str:
        self.rows[uid] = props
        return uid


def _uid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _project(tmp_path: Path, files=("src/a.py",)) -> Path:
    root = tmp_path / "proj"
    for rel in files:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")
    return root


# ── A-013: the reconciliation decision ───────────────────────────────────


def test_rows_of_a_deleted_file_are_deleted_and_live_rows_kept(tmp_path: Path) -> None:
    """ACT. RED before CG-3: nothing on the ordinary path removed these rows."""
    root = _project(tmp_path)
    funcs = _Coll("P_CodeFunction")
    live = funcs.seed(_uid(1), file_path="src/a.py", project_source=root.as_posix())
    gone = funcs.seed(_uid(2), file_path="Game/old.py", project_source=root.as_posix())
    legacy_gone = funcs.seed(_uid(3), file_path="Game/older.py", project_source="")
    extra = funcs.seed(_uid(4), file_path="Game/old.py", project_source="/srv/extra")
    mods = _Coll("P_CodeModule")
    mod_gone = mods.seed(_uid(5), path="Game/old.py", project_source=root.as_posix())

    res = cpr.reconcile_collections([(mods, "path"), (funcs, "file_path")], root)

    assert res.outcome == "ok", res
    assert sorted(funcs.deleted) == [gone, legacy_gone] and mods.deleted == [mod_gone]
    assert live in funcs.rows and extra in funcs.rows, "live and extra-folder rows are kept"
    assert (res.distinct_paths, res.absent_paths, res.deleted) == (3, 2, 3)
    # "no read outside the three properties": only path + project_source asked.
    assert all(r["props"] == ["file_path", "project_source"] for r in funcs.reads)
    assert all(r["after"] is not None for r in funcs.reads), "always a cursor"


@pytest.mark.parametrize("shape", ["missing", "empty"])
def test_a_missing_or_empty_root_deletes_nothing(tmp_path: Path, shape: str) -> None:
    """LEAVE-ALONE: an unmounted drive / missing folder / empty checkout makes
    every path ENOENT; that is not evidence of deletion."""
    root = tmp_path / "proj"
    if shape == "empty":
        root.mkdir()
    funcs = _Coll("P_CodeFunction")
    funcs.seed(_uid(1), file_path="src/a.py", project_source=root.as_posix())
    res = cpr.reconcile_collections([(funcs, "file_path")], root)
    assert res.outcome == "root_missing" and funcs.deleted == [] and funcs.reads == []


def test_every_path_missing_deletes_nothing(tmp_path: Path) -> None:
    """LEAVE-ALONE: the root holds files, but not ONE listed path exists — a
    replaced or moved tree, not deleted code."""
    root = _project(tmp_path, files=("unrelated.txt",))
    funcs = _Coll("P_CodeFunction")
    funcs.seed(_uid(1), file_path="src/a.py", project_source=root.as_posix())
    funcs.seed(_uid(2), file_path="src/b.py", project_source=root.as_posix())
    res = cpr.reconcile_collections([(funcs, "file_path")], root)
    assert res.outcome == "all_paths_missing" and funcs.deleted == []


def test_an_unreadable_class_authorises_no_delete_in_it(tmp_path: Path, monkeypatch) -> None:
    """LEAVE-ALONE for the class that failed (a page error, or 0 rows with an
    unreadable count); the readable class is still reconciled."""
    monkeypatch.setattr(weaviate_helpers, "http_count_objects", lambda *_a, **_k: None)
    root = _project(tmp_path)
    broken = _Coll("P_CodeClass", fail_pages=True)
    broken.seed(_uid(1), file_path="Game/old.py", project_source="")
    blind = _Coll("P_CodeModule")  # lists 0 rows; the count cannot be read
    funcs = _Coll("P_CodeFunction")
    funcs.seed(_uid(2), file_path="src/a.py", project_source="")
    gone = funcs.seed(_uid(3), file_path="Game/old.py", project_source="")
    res = cpr.reconcile_collections(
        [(broken, "file_path"), (blind, "path"), (funcs, "file_path")], root)
    assert res.outcome == "partial"
    assert sorted(res.unreadable) == ["P_CodeClass", "P_CodeModule"]
    assert broken.deleted == [] and funcs.deleted == [gone]


def test_an_unknowable_path_is_kept(tmp_path: Path) -> None:
    """LEAVE-ALONE: a probe that cannot tell (EACCES, …) reads as present."""
    root = _project(tmp_path)
    funcs = _Coll("P_CodeFunction")
    funcs.seed(_uid(1), file_path="src/a.py", project_source="")
    funcs.seed(_uid(2), file_path="locked/x.py", project_source="")
    res = cpr.reconcile_collections(
        [(funcs, "file_path")], root, reachable=lambda p: True)
    assert funcs.deleted == [] and res.absent_paths == 0


# ── A-013: the daily trigger ─────────────────────────────────────────────


class _Slot:
    def __init__(self, held: Optional[int]):
        self.held = held

    def __call__(self):
        held = self.held

        class _CM:
            def __enter__(self):
                return held

            def __exit__(self, *a):
                return False

        return _CM()


def _reconcile(tmp_path, root, *, slot=0, opener=None, now=1_000_000.0):
    state = root / ".claude" / "state"
    lines: list = []
    funcs = _Coll("P_CodeFunction")
    gone = funcs.seed(_uid(1), file_path="Game/old.py", project_source="")
    funcs.seed(_uid(2), file_path="src/a.py", project_source="")
    closed: list = []
    client = SimpleNamespace(close=lambda: closed.append(True))

    def _open(prefix):
        assert prefix == "P"
        return client, [(funcs, "file_path")]

    out = cpr.reconcile_if_due(
        str(root), "P", state, lines.append, now=lambda: now,
        open_collections=opener or _open, slot=_Slot(slot))
    return out, lines, state, funcs, gone, closed


def test_a_due_run_reconciles_stamps_and_logs(tmp_path: Path) -> None:
    root = _project(tmp_path)
    out, lines, state, funcs, gone, closed = _reconcile(tmp_path, root)
    assert out == "ok" and funcs.deleted == [gone] and closed == [True]
    assert (state / cpr.STAMP_NAME).read_text() == "1000000"
    assert any(ln.startswith("path-reconcile: outcome=ok") and "deleted=1" in ln
               for ln in lines), lines
    assert not list(state.glob("codegraph_drain_root_*.lock")), "the lock is released"


def test_a_run_inside_the_day_does_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)
    state = root / ".claude" / "state"
    state.mkdir(parents=True)
    (state / cpr.STAMP_NAME).write_text(str(1_000_000 - 3600))
    out, _l, _s, funcs, _g, _c = _reconcile(tmp_path, root)
    assert out == "not_due" and funcs.deleted == [] and funcs.reads == []


def test_a_future_stamp_is_no_stamp(tmp_path: Path) -> None:
    root = _project(tmp_path)
    state = root / ".claude" / "state"
    state.mkdir(parents=True)
    (state / cpr.STAMP_NAME).write_text(str(1_000_000 + 7200))
    out, *_ = _reconcile(tmp_path, root)
    assert out == "ok"


def test_busy_slots_skip_without_a_stamp(tmp_path: Path) -> None:
    root = _project(tmp_path)
    out, lines, state, funcs, _g, _c = _reconcile(tmp_path, root, slot=None)
    assert out == "slot_busy" and funcs.reads == []
    assert not (state / cpr.STAMP_NAME).exists(), "retried at the next catch-up"


def test_unreachable_weaviate_stamps_nothing(tmp_path: Path) -> None:
    root = _project(tmp_path)

    def _boom(_prefix):
        raise ConnectionError("refused")

    out, lines, state, *_ = _reconcile(tmp_path, root, opener=_boom)
    assert out == "unreachable" and not (state / cpr.STAMP_NAME).exists()


def test_a_held_checkout_lock_skips_without_a_stamp(tmp_path: Path) -> None:
    root = _project(tmp_path)
    state = root / ".claude" / "state"
    lock = root_lock.lock_path(state, root_lock.path_key(str(root.resolve())))
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    out, _l, _s, funcs, _g, _c = _reconcile(tmp_path, root)
    assert out == "busy" and funcs.reads == [] and not (state / cpr.STAMP_NAME).exists()


# ── the shared per-checkout lock ─────────────────────────────────────────


def _held(state: Path, pid: Optional[str], age_s: float = 0.0) -> Path:
    lock = root_lock.lock_path(state, "k")
    lock.mkdir(parents=True)
    if pid is not None:
        (lock / "pid").write_text(pid)
    if age_s:
        old = time.time() - age_s
        os.utime(lock, (old, old))
    return lock


@pytest.mark.parametrize("alive,breaks", [(False, True), (True, False), (None, False)])
def test_a_lock_is_broken_only_when_its_holder_is_provably_dead(
        tmp_path: Path, alive, breaks) -> None:
    state = tmp_path / "state"
    _held(state, "4242")
    got = root_lock.acquire(state, "k", alive=lambda _pid: alive)
    assert (got is not None) is breaks
    if got is not None:
        assert (got / "pid").read_text() == str(os.getpid())
        root_lock.release(got)


@pytest.mark.parametrize("age_s,breaks", [(31 * 60, True), (60, False)])
def test_a_pidless_lock_is_broken_only_after_the_bound(tmp_path: Path, age_s, breaks) -> None:
    state = tmp_path / "state"
    _held(state, None, age_s=age_s)
    got = root_lock.acquire(state, "k")
    assert (got is not None) is breaks


def test_try_machine_slot_never_waits(tmp_path: Path) -> None:
    with atomic.try_machine_slot(tmp_path, slots=1) as first:
        assert first == 0
        with atomic.try_machine_slot(tmp_path, slots=1) as second:
            assert second is None, "a busy pool yields None instead of queueing"
    with atomic.try_machine_slot(tmp_path, slots=1) as again:
        assert again == 0, "released on exit"


# ── the retired drain-queue state files ──────────────────────────────────


def test_retired_queue_files_are_removed_and_the_lock_is_kept(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for name in ("codegraph_drain_s1.txt", "codegraph_drain_shared.txt",
                 "codegraph_drain_extra_0123456789abcdef0123456789abcdef_s1.txt",
                 "codegraph_drain_list_ab.txt", "codegraph_drain_s1.draining.77",
                 "codegraph_drain_last_sync.ts"):
        (state / name).write_text("x\n")
    lock = state / "codegraph_drain_root_ab.lock"
    lock.mkdir()
    keep = state / "codegraph_head_catchup.ts"
    keep.write_text("1")
    lines: list = []
    assert chc.retire_drain_queue_files(state, lines.append) == 6
    assert sorted(p.name for p in state.iterdir()) == [
        "codegraph_drain_root_ab.lock", "codegraph_head_catchup.ts"]
    assert lines and "retired 6" in lines[0]


# ── A-012: worktrees are never indexed; one run per HEAD move ────────────

_GIT = shutil.which("git")
_BASH = shutil.which("bash")


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


@dataclass(frozen=True)
class _Cfg:
    project_id: str = "pid-1"
    code_graph_collection_prefix: str = "P"
    code_graph_last_analyzed_commit: Optional[str] = None
    code_graph_bound: bool = True


class _Hub:
    """The binding's recorded commit, kept between catch-ups."""

    def __init__(self, recorded: str):
        self.recorded = recorded
        self.runs: list = []
        self.reconciles: list = []

    def resolve(self, _root):
        return _Cfg(code_graph_last_analyzed_commit=self.recorded)

    def record(self, _pid, commit):
        self.recorded = commit
        return True, "recorded"

    def run_analyzer(self, _py, _an, argv, _timeout):
        files = []
        if "--only-files-from" in argv:
            files = Path(argv[argv.index("--only-files-from") + 1]).read_text().split()
        self.runs.append({"argv": list(argv), "files": files})
        return chc.AnalyzerResult(True, "ok", len(files), 1, 1)

    def reconcile(self, root, prefix, state_dir, log):
        self.reconciles.append(root)
        return "not_due"

    def deps(self, **kw) -> chc.Deps:
        return chc.Deps(resolve=self.resolve, run_analyzer=self.run_analyzer,
                        record=self.record, reconcile=self.reconcile, **kw)


def _catchup(root: Path, hub: _Hub, **kw) -> "tuple[str, list]":
    lines: list = []
    state = root / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    out = chc.catchup(str(root), "/x/analyze.py", "/x/python", state, lines.append,
                      deps=hub.deps(**kw))
    return out, lines


def _route_sh(project_root: Path, touched: Path, tmp_path: Path) -> None:
    script = (f'. "{ROUTE_LIB_SH}"\n'
              f'vco_route_init "{HOOKS_DIR}" "{project_root}" "{shutil.which("python3")}"\n'
              f'vco_route_touched_path "{touched}" "sess1"\n')
    env = {**os.environ, "VCT_STATE_DIR": str(tmp_path / "vct-state"), "VCT_VENV": "",
           "VCT_INSTALL_ROOT": "", "VCO_VENV_PYTHON": ""}
    subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True,
                   timeout=60, env=env)


@pytest.mark.skipif(not (_GIT and _BASH and shutil.which("python3")), reason="git + bash required")
def test_two_worktrees_editing_one_file_write_nothing_until_the_merge(tmp_path: Path) -> None:
    """RED before CG-3: each worktree edit was queued and indexed under the
    main checkout's identity (lanes overwrote each other's rows)."""
    main = tmp_path / "main"
    (main / "src").mkdir(parents=True)
    (main / "src" / "shared.py").write_text("def f():\n    return 0\n")
    _git(main, "init", "-q", "-b", "main")
    _git(main, "add", ".")
    _git(main, "commit", "-q", "-m", "base")
    base = _git(main, "rev-parse", "HEAD")
    hub = _Hub(recorded=base)

    worktrees = []
    for name, body in (("lane-a", "return 1"), ("lane-b", "return 2")):
        wt = tmp_path / name
        _git(main, "worktree", "add", "-q", "-b", name, str(wt))
        edited = wt / "src" / "shared.py"
        edited.write_text(f"def f():\n    {body}\n")
        _route_sh(main, edited, tmp_path)           # the parent's routing hook
        _git(wt, "commit", "-q", "-am", name)
        out, lines = _catchup(wt, hub)              # a session in the worktree
        assert out == "worktree_session", lines
        worktrees.append(wt)

    state = main / ".claude" / "state"
    assert not list(state.glob("codegraph_drain_*")), "an edit queues nothing"
    assert hub.runs == [] and hub.recorded == base, "nothing written before the merge"
    assert hub.reconciles == [], "a worktree session runs no reconciliation either"

    _git(main, "merge", "-q", "--no-edit", "lane-a")
    out, lines = _catchup(main, hub)
    assert out == "recorded", lines
    assert len(hub.runs) == 1, "ONE analyzer run for the HEAD move"
    assert hub.runs[0]["files"] == [str(main / "src" / "shared.py")]
    assert (main / "src" / "shared.py").read_text().endswith("return 1\n"), "the merged state"
    assert hub.recorded == _git(main, "rev-parse", "HEAD")

    out, _ = _catchup(main, hub)
    assert out == "up_to_date" and len(hub.runs) == 1, "the same change is never processed twice"
    assert len(hub.reconciles) == 2, "the daily reconciliation is offered after each catch-up"


def test_a_worktree_session_is_skipped_before_head_is_read(tmp_path: Path) -> None:
    hub = _Hub(recorded="a" * 40)
    read_head: list = []

    def _head(root):
        read_head.append(root)
        return "b" * 40

    root = tmp_path / "wt"
    out, lines = _catchup(root, hub, git_head=_head,
                          linked_worktree=lambda _r: str(tmp_path / "main"))
    assert out == "worktree_session" and read_head == [] and hub.reconciles == []
    assert any("worktrees are never indexed" in ln for ln in lines), lines


def test_a_busy_checkout_skips_the_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    state = root / ".claude" / "state"
    lock = root_lock.lock_path(state, chc.path_key(str(root.resolve())))
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    hub = _Hub(recorded="a" * 40)
    out, _ = _catchup(root, hub, git_head=lambda _r: "b" * 40,
                      is_ancestor=lambda *_a: True, diff_paths=lambda *_a: ["src/a.py"],
                      linked_worktree=lambda _r: None)
    assert out == "busy" and hub.reconciles == []


def test_a_project_without_git_still_gets_the_reconciliation(tmp_path: Path) -> None:
    """A non-git project is updated by a manual re-analysis only, but rows of
    its deleted files are still removed by the daily reconciliation."""
    root = tmp_path / "proj"
    hub = _Hub(recorded="a" * 40)
    out, _ = _catchup(root, hub, git_head=lambda _r: None, linked_worktree=lambda _r: None)
    assert out == "no_git_head" and hub.runs == [] and hub.reconciles == [str(root)]


# ── CGWRITE-11: the walk tally ───────────────────────────────────────────


def test_walk_tally_counts_and_times() -> None:
    from vco_lib import codegraph_guards as g

    g.walk_tally_reset()
    g.walk_count("embeds", 3)
    g.walk_count("stamped")
    g.walk_count("skipped", 5)
    g.walk_count("rows_written", 4)
    g.walk_count("xref_reads", 2)
    g.walk_count("not-a-counter", 9)  # ignored: a counter never fails a write
    g.walk_tally_mark_xref()
    line = g.walk_tally_line()
    assert "embeds=3 stamped=1 skipped=5 rows_written=4 xref_reads=2" in line
    assert line.startswith("started=20") and " ended=20" in line
    assert " walk_s=" in line and " xref_s=" in line
    g.walk_tally_reset()
    assert g.walk_tally() == dict.fromkeys(g.WALK_TALLY_KEYS, 0)


def test_the_final_report_tally_reaches_the_catchup_log(tmp_path: Path) -> None:
    """The analyzer's ``{"final": true, "tally": …}`` line is carried by
    ``run_analyzer`` (RED before: the catch-up asked for no final report and
    logged no counts or times)."""
    stub = tmp_path / "stub.py"
    stub.write_text(
        "import json\n"
        "print(json.dumps({'final': True, 'files_analyzed': 2, 'modules': 2,"
        " 'tally': 'started=2026-01-01T00:00:00Z ended=2026-01-01T00:00:09Z walk_s=8.0"
        " xref_s=1.0 embeds=1 stamped=0 skipped=3 rows_written=1 xref_reads=6'}))\n")
    import sys

    res = chc.run_analyzer(sys.executable, str(stub), [], 60)
    assert res.ok and "embeds=1" in res.tally and "started=2026-01-01" in res.tally


# ── the SessionStart check: a pull made outside a session ────────────────


@pytest.mark.parametrize("flavour,script", [("linux", "stop-codegraph-drain.sh"),
                                            ("windows", "stop-codegraph-drain.ps1")])
def test_the_catchup_hook_also_runs_at_session_start(flavour: str, script: str) -> None:
    """RED before CG-3: only Stop ran the catch-up, so a pull or merge made
    outside a session waited for the first turn's end."""
    import json

    doc = json.loads((REPO_ROOT / "templates" / f"settings.json.{flavour}.template")
                     .read_text(encoding="utf-8"))
    groups = doc["hooks"]["SessionStart"]
    matchers = sorted(g["matcher"] for g in groups
                      if any(script in h.get("command", "") for h in g["hooks"]))
    assert matchers == ["resume", "startup"], matchers
    for g in groups:
        for h in g["hooks"]:
            if script in h.get("command", ""):
                assert h.get("async") is True, "a SessionStart hook never waits on an analyzer"
    stop = [h for g in doc["hooks"]["Stop"] for h in g["hooks"] if script in h.get("command", "")]
    assert len(stop) == 1
