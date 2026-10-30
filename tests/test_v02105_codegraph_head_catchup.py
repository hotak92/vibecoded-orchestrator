# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 #512A — HEAD-diff catch-up for the PRIMARY code-graph root.

Field defect: a project's code graph was six days / 1,070 commits stale. The
end-of-turn drain only ever indexes files the EDIT hook queued, so code that
arrives through a lane merge, a `git pull`, a shell commit or a GUI merge is
never indexed and nothing ever notices.

`vco_lib.codegraph_head_catchup` compares the binding's recorded
`last_analyzed_commit` (hub /config) with the main checkout's HEAD and indexes
exactly the `git diff --name-only <recorded>..HEAD` files. Covered here:

  * unchanged HEAD → NOTHING (the leave-alone case);
  * behind + recorded is an ancestor → the changed, existing code files are
    analyzed in ONE scoped batch and HEAD is recorded only after success;
  * a FAILED `git diff` (timeout / git error) → nothing runs, nothing is
    recorded — failure is distinct from an empty diff (fix round M2);
  * a non-ancestor / missing baseline → ONE full (non-incremental) walk,
    then HEAD is recorded — the converging fallback (fix round M3: the old
    `--incremental HEAD~1..HEAD` walk recorded nothing and re-ran forever);
  * a project with no ENABLED code-graph binding → skipped entirely; the
    hub's name-derived fallback prefix never authorizes a walk (M3);
  * a huge diff, or a deleted code file → the analyzer's incremental walk
    (which prunes deletions) instead of a per-turn batch;
  * the per-root lock keys on the drain's CANONICAL root, so a symlinked
    spelling of the checkout still serialises with a drain batch (LOW);
  * a linked-worktree session → skipped (FIX-A' territory);
  * a failed analysis → nothing recorded, so the next catch-up retries;
  * the drain hook spawns the module detached, once per interval — even when
    the turn queued no file (a merge queues none);
  * end-to-end against a REAL git repo with a merge commit.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from vco_lib import codegraph_head_catchup as chc
from tests.common.pwsh import PWSH_BASE_FLAGS

REPO_ROOT = Path(__file__).resolve().parent.parent
DRAIN_SH = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.sh"
DRAIN_PS1 = REPO_ROOT / "templates" / "hooks" / "stop-codegraph-drain.ps1"


@dataclass(frozen=True)
class _Cfg:
    project_id: str = "pid-1"
    code_graph_collection_prefix: str = "Acme"
    code_graph_last_analyzed_commit: Optional[str] = None
    code_graph_bound: bool = True


class _Recorder:
    """Fake git plumbing + analyzer + hub recorder.

    ``diff=None`` (the default) means the git diff FAILED — the tri-state
    the fix round introduced (failure ≠ empty). Pass a list for a working
    diff, ``[]`` for an empty one.
    """

    def __init__(
        self,
        head: Optional[str],
        diff: Optional[list] = None,
        *,
        ancestor: bool = True,
        recorded: Optional[str] = None,
        ok: bool = True,
        record_ok: bool = True,
        canonical: Optional[str] = None,
        bound: bool = True,
    ):
        self.head = head
        self.diff = diff
        self.ancestor = ancestor
        self.recorded = recorded
        self.ok = ok
        self.record_ok = record_ok
        self.canonical = canonical
        self.bound = bound
        self.runs: list = []
        self.records: list = []

    def resolve(self, _root):
        return _Cfg(
            code_graph_last_analyzed_commit=self.recorded,
            code_graph_bound=self.bound,
        )

    def git_head(self, _root):
        return self.head

    def is_ancestor(self, _root, _a, _d):
        return self.ancestor

    def diff_paths(self, _root, _old, _new):
        return None if self.diff is None else list(self.diff)

    def canonical_root(self, root):
        return self.canonical if self.canonical is not None else str(Path(root).resolve())

    def run_analyzer(self, python, analyzer, argv, timeout):
        # Capture the list file's contents while it still exists.
        listing: list = []
        if "--only-files-from" in argv:
            listing = Path(argv[argv.index("--only-files-from") + 1]).read_text().splitlines()
        self.runs.append({"argv": list(argv), "files": listing})
        if self.ok:
            return chc.AnalyzerResult(True, "ok", len(listing) or 1, 2, 3,
                                      deleted_files=1, deleted_entities=4)
        return chc.AnalyzerResult(False, "exit 1: boom")

    def record(self, project_id, commit):
        self.records.append((project_id, commit))
        return (self.record_ok, "recorded" if self.record_ok else "hub answered 500")


def _drive(tmp_path: Path, rec: _Recorder, now=None, **kw):
    lines: list = []
    deps = chc.Deps(
        resolve=rec.resolve,
        git_head=rec.git_head,
        is_ancestor=rec.is_ancestor,
        diff_paths=rec.diff_paths,
        run_analyzer=rec.run_analyzer,
        record=rec.record,
        canonical_root=rec.canonical_root,
        now=now or time.time,
    )
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    for rel in kw.pop("create_files", []):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")
    out = chc.catchup(
        str(root), "/x/analyze.py", "/x/python", root / ".claude" / "state",
        lines.append, deps=deps, **kw,
    )
    return out, lines, root


A = "a" * 40
B = "b" * 40


# ── the shared argv shapes (MUST MATCH the drain's spawn line) ─────────────


def test_scoped_argv_matches_the_drain_spawn(tmp_path: Path) -> None:
    got = chc.build_scoped_argv("/srv/proj", "Acme", "/s/l.txt", "--no-index-dot-claude")
    assert got == [
        "/srv/proj", "--project", "Acme", "--only-files-from", "/s/l.txt",
        "--canonical-source", "/srv/proj", "--no-index-dot-claude",
    ]


def test_incremental_argv_never_carries_canonical_source() -> None:
    # The analyzer REFUSES --canonical-source outside a scoped file mode
    # (exit 1), so the incremental shapes must not carry it.
    assert chc.build_incremental_argv("/p", "Acme", None, "--no-index-dot-claude") == [
        "/p", "--project", "Acme", "--incremental", "--no-index-dot-claude",
    ]
    argv = chc.build_incremental_argv("/p", "Acme", A, "--index-dot-claude")
    assert argv == ["/p", "--project", "Acme", "--incremental", "--since-commit", A,
                    "--index-dot-claude"]
    assert "--canonical-source" not in argv


def test_full_argv_is_one_plain_walk() -> None:
    # The converging fallback (M3): a FULL walk — no --incremental (which
    # would bound it to HEAD~1..HEAD), no --since-commit, no scoped batch.
    assert chc.build_full_argv("/p", "Acme", "--no-index-dot-claude") == [
        "/p", "--project", "Acme", "--no-index-dot-claude",
    ]
    argv = chc.build_full_argv("/p", "Acme", "--index-dot-claude")
    assert "--incremental" not in argv and "--only-files-from" not in argv
    assert "--canonical-source" not in argv


def test_dot_claude_flag_follows_the_shared_root_heuristic(tmp_path: Path) -> None:
    plain = tmp_path / "userproj"
    (plain / ".claude").mkdir(parents=True)
    assert chc.index_dot_claude_flag(str(plain)) == "--no-index-dot-claude"
    orch = tmp_path / "orch"
    (orch / ".claude").mkdir(parents=True)
    (orch / "vco_lib").mkdir(parents=True)
    assert chc.index_dot_claude_flag(str(orch)) == "--index-dot-claude"


def test_code_extension_gate_never_drifts_from_the_drain_siblings() -> None:
    """The module's extension gate is a MIRROR of the drain's (A>B>C rule C):

    ``stop-codegraph-drain.{sh,ps1}`` decide which queued path is a code file
    in shell, and this module applies the SAME rule to a git diff. A drift
    here would index a different set of files depending on how a change
    arrived, so both sibling literals are pinned to this module's pattern.
    """
    import re as _re

    sh = DRAIN_SH.read_text(encoding="utf-8")
    ps1 = DRAIN_PS1.read_text(encoding="utf-8")
    sh_pat = _re.search(r"^CODE_RE='([^']+)'$", sh, _re.M)
    ps1_pat = _re.search(r"^\$codeRe ='([^']+)'$", ps1, _re.M)
    assert sh_pat and ps1_pat, "the drain siblings no longer declare their gate"
    assert sh_pat.group(1) == ps1_pat.group(1), "the two siblings already drifted"
    assert chc.CODE_RE.pattern == sh_pat.group(1), (
        "vco_lib.codegraph_head_catchup.CODE_RE drifted from "
        f"templates/hooks/stop-codegraph-drain.sh ({chc.CODE_RE.pattern!r} != "
        f"{sh_pat.group(1)!r})"
    )


# ── the decision table ─────────────────────────────────────────────────────


def test_unchanged_head_is_a_leave_alone(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=B)
    out, lines, _ = _drive(tmp_path, rec)
    assert out == "up_to_date"
    assert rec.runs == [] and rec.records == []


def test_no_git_head_is_a_leave_alone(tmp_path: Path) -> None:
    rec = _Recorder(head=None, recorded=A)
    out, _, _ = _drive(tmp_path, rec)
    assert out == "no_git_head"
    assert rec.runs == [] and rec.records == []


def test_behind_queues_the_changed_files_and_records_head(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=A, diff=["src/a.py", "src/b.ts"])
    out, lines, root = _drive(tmp_path, rec, create_files=["src/a.py", "src/b.ts"])
    assert out == "recorded"
    assert len(rec.runs) == 1
    run = rec.runs[0]
    # ONE scoped batch over exactly the changed files, drain argv shape.
    assert run["argv"][0] == str(root)
    assert "--only-files-from" in run["argv"]
    assert run["files"] == [str(root / "src/a.py"), str(root / "src/b.ts")]
    assert rec.records == [("pid-1", B)]
    assert any("recorded project=pid-1" in ln for ln in lines), lines
    # Nothing left behind: the list file and the per-root lock are gone.
    state = root / ".claude" / "state"
    assert not list(state.glob("codegraph_head_catchup_files_*.txt"))
    assert not list(state.glob("codegraph_drain_root_*.lock"))


def test_filters_non_code_missing_and_scratch_files(tmp_path: Path) -> None:
    rec = _Recorder(
        head=B, recorded=A,
        diff=["README.md", "src/a.py", "src/gone.py", ".claude/state/x.py", "Makefile"],
    )
    out, lines, root = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "recorded"
    # Only the existing code file survived; the vanished code file routes the
    # diff to the incremental walk (which prunes it), so no scoped batch ran.
    assert all("--only-files-from" not in r["argv"] for r in rec.runs)
    assert "--since-commit" in rec.runs[0]["argv"]
    assert rec.records == [("pid-1", B)]
    assert any("1 deleted code file" in ln for ln in lines), lines


def test_only_non_indexable_changes_advance_the_baseline(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=A, diff=["README.md", "docs/x.md"])
    out, _, _ = _drive(tmp_path, rec)
    assert out == "recorded"
    assert rec.runs == []          # nothing to analyze
    assert rec.records == [("pid-1", B)]


def test_non_ancestor_converges_via_one_full_walk_then_records(
    tmp_path: Path,
) -> None:
    """M3: the old fallback ran an ``--incremental HEAD~1..HEAD`` walk and
    recorded NOTHING, so a project with a rewritten history re-walked one
    commit every 600 s forever while falling further behind. The converging
    shape: ONE full walk, then HEAD is recorded — after which the next
    catch-up is ``up_to_date``."""
    rec = _Recorder(head=B, recorded=A, ancestor=False, diff=["src/a.py"])
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "recorded"
    assert len(rec.runs) == 1
    argv = rec.runs[0]["argv"]
    assert "--incremental" not in argv and "--since-commit" not in argv
    assert "--only-files-from" not in argv
    assert rec.records == [("pid-1", B)]   # the baseline is re-proven, then advanced
    assert any("NOT an ancestor" in ln for ln in lines), lines
    assert any("full walk" in ln for ln in lines), lines
    # Convergence: with HEAD now recorded, the next catch-up is a no-op.
    rec.recorded = B
    out2, _lines2, _r = _drive(tmp_path, rec)
    assert out2 == "up_to_date"
    assert len(rec.runs) == 1


def test_missing_baseline_converges_via_one_full_walk_then_records(
    tmp_path: Path,
) -> None:
    """M3: same convergence for a project never analyzed (no recorded
    commit) — a full walk once, then the baseline exists."""
    rec = _Recorder(head=B, recorded=None, diff=["src/a.py"])
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "recorded"
    argv = rec.runs[0]["argv"]
    assert "--incremental" not in argv and "--only-files-from" not in argv
    assert rec.records == [("pid-1", B)]
    assert any("no recorded commit" in ln for ln in lines), lines


def test_full_walk_failure_records_nothing_and_retries_next_interval(
    tmp_path: Path,
) -> None:
    rec = _Recorder(head=B, recorded=None, ok=False)
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "analyzer_failed"
    assert rec.records == []
    assert any("no_recorded: FAILED" in ln for ln in lines), lines


def test_a_failed_diff_never_records_head(tmp_path: Path) -> None:
    """M2: a FAILED ``git diff`` (timeout, git error) used to read as an
    empty diff, so HEAD was recorded without indexing anything — the field
    scenario made silent. Failure is distinct from empty: nothing runs,
    nothing is recorded, and the log says why."""
    rec = _Recorder(head=B, recorded=A, diff=None)
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "diff_failed"
    assert rec.runs == [] and rec.records == []
    assert any("git diff failed" in ln for ln in lines), lines


def test_an_empty_diff_still_records_head(tmp_path: Path) -> None:
    """The other side of M2's branch: a diff that RAN and is empty (only
    non-code files changed) still advances the baseline."""
    rec = _Recorder(head=B, recorded=A, diff=[])
    out, lines, _ = _drive(tmp_path, rec)
    assert out == "recorded"
    assert rec.runs == []
    assert rec.records == [("pid-1", B)]


def test_unbound_or_disabled_project_is_skipped(tmp_path: Path) -> None:
    """M3: /config serves a NAME-derived fallback prefix even for a project
    with no binding row, so a prefix alone must not authorize a walk — the
    catch-up would create code-graph collections for an unbound project and
    loop a walk every 600 s. ``code_graph_bound`` False (no row, or the row
    is disabled) → nothing at all happens."""
    rec = _Recorder(head=B, recorded=A, diff=["src/a.py"], bound=False)
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "not_bound"
    assert rec.runs == [] and rec.records == []
    assert any("no enabled code-graph binding" in ln for ln in lines), lines
    # And with no recorded commit either (never analyzed + unbound): still
    # nothing — the full-walk fallback is behind the same gate.
    rec2 = _Recorder(head=B, recorded=None, bound=False)
    out2, _l2, _r2 = _drive(tmp_path, rec2)
    assert out2 == "not_bound"
    assert rec2.runs == [] and rec2.records == []


def test_the_root_lock_keys_on_the_drains_canonical_root(tmp_path: Path) -> None:
    """LOW: the drain keys its per-root lock on the git-resolved canonical
    root; the catch-up keyed on the session's spelling of the path, so a
    symlinked checkout let a catch-up walk and a drain batch run over the
    same repo at once. A lock held at the CANONICAL key must read as busy
    even when the project root was spelled through the symlink."""
    real = tmp_path / "real-proj"
    (real / ".claude" / "state").mkdir(parents=True)
    (real / "src").mkdir()
    (real / "src" / "a.py").write_text("x = 1\n")
    link = tmp_path / "link-proj"
    link.symlink_to(real, target_is_directory=True)
    state = real / ".claude" / "state"

    # The drain's own key: canonical (realpath) root.
    (state / f"codegraph_drain_root_{chc.path_key(str(real))}.lock").mkdir()

    rec = _Recorder(head=B, recorded=A, diff=["src/a.py"])
    lines: list = []
    out = chc.catchup(
        str(link), "/x/a.py", "/x/py", state, lines.append,
        deps=chc.Deps(resolve=rec.resolve, git_head=rec.git_head,
                      is_ancestor=rec.is_ancestor, diff_paths=rec.diff_paths,
                      run_analyzer=rec.run_analyzer, record=rec.record,
                      canonical_root=rec.canonical_root),
    )
    assert out == "busy"
    assert rec.runs == [] and rec.records == []
    assert any("holds the root lock" in ln for ln in lines), lines


def test_huge_diff_goes_to_the_background_incremental_walk(tmp_path: Path) -> None:
    files = [f"src/f{i}.py" for i in range(5)]
    rec = _Recorder(head=B, recorded=A, diff=files)
    out, lines, _ = _drive(tmp_path, rec, create_files=files, max_files=3)
    assert out == "recorded"
    argv = rec.runs[0]["argv"]
    assert "--incremental" in argv
    assert argv[argv.index("--since-commit") + 1] == A
    assert "--only-files-from" not in argv          # NOT the per-turn batch
    assert rec.records == [("pid-1", B)]
    assert any("huge_diff" in ln for ln in lines), lines


def test_worktree_session_is_skipped(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=A, diff=["src/a.py"],
                    canonical=str(tmp_path / "elsewhere"))
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "worktree_session"
    assert rec.runs == [] and rec.records == []
    assert any("linked worktree" in ln for ln in lines), lines


def test_analyzer_failure_records_nothing(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=A, diff=["src/a.py"], ok=False)
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "analyzer_failed"
    assert rec.records == []
    assert any("FAILED project=pid-1" in ln and "boom" in ln for ln in lines), lines
    # The lock is released so the next catch-up can retry.
    assert not list((tmp_path / "proj" / ".claude" / "state").glob("codegraph_drain_root_*.lock"))


def test_record_failure_is_logged(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=A, diff=["src/a.py"], record_ok=False)
    out, lines, _ = _drive(tmp_path, rec, create_files=["src/a.py"])
    assert out == "record_failed"
    assert any("NOT RECORDED" in ln for ln in lines), lines


def test_held_root_lock_is_busy_and_records_nothing(tmp_path: Path) -> None:
    rec = _Recorder(head=B, recorded=A, diff=["src/a.py"])
    root = tmp_path / "proj"
    state = root / ".claude" / "state"
    state.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("x = 1\n")
    (state / f"codegraph_drain_root_{chc.path_key(str(root))}.lock").mkdir()
    lines: list = []
    out = chc.catchup(
        str(root), "/x/a.py", "/x/py", state, lines.append,
        deps=chc.Deps(resolve=rec.resolve, git_head=rec.git_head,
                      is_ancestor=rec.is_ancestor, diff_paths=rec.diff_paths,
                      run_analyzer=rec.run_analyzer, record=rec.record,
                      canonical_root=rec.canonical_root),
    )
    assert out == "busy"
    assert rec.runs == [] and rec.records == []


def test_hub_unreachable_is_a_logged_noop(tmp_path: Path) -> None:
    lines: list = []

    def _boom(_root):
        raise RuntimeError("hub down")

    out = chc.catchup(
        str(tmp_path), "/x/a.py", "/x/py", tmp_path / "state", lines.append,
        deps=chc.Deps(resolve=_boom),
    )
    assert out == "no_config"
    assert any("config unavailable" in ln for ln in lines)


# ── the CLI: the huge-diff knob is a real control ─────────────────────────


def _cli_seen(tmp_path: Path, monkeypatch, argv: list) -> dict:
    seen: dict = {}
    monkeypatch.setattr(chc, "catchup", lambda *a, **kw: seen.update(kw) or "up_to_date")
    assert chc.main(["--project-root", str(tmp_path), "--analyzer", "/x/a.py", *argv]) == 0
    return seen


def test_max_files_env_knob_reaches_catchup(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCO_CODEGRAPH_HEAD_CATCHUP_MAX_FILES", "7")
    assert _cli_seen(tmp_path, monkeypatch, [])["max_files"] == 7


def test_explicit_max_files_flag_beats_the_env_knob(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCO_CODEGRAPH_HEAD_CATCHUP_MAX_FILES", "7")
    assert _cli_seen(tmp_path, monkeypatch, ["--max-files", "3"])["max_files"] == 3


def test_max_files_falls_back_to_the_bound_on_a_bad_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VCO_CODEGRAPH_HEAD_CATCHUP_MAX_FILES", "not-a-number")
    assert _cli_seen(tmp_path, monkeypatch, [])["max_files"] == chc.DEFAULT_MAX_DIFF_FILES


# ── real git: commits + a merge, fake recorded commit ──────────────────────


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True, env=env).stdout.strip()


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_real_git_merge_diff_is_queued_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "seed.py").write_text("def s():\n    return 0\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    recorded = _git(root, "rev-parse", "HEAD")

    # A lane branch lands a new module through a merge — no Edit hook ever
    # sees it.
    _git(root, "checkout", "-q", "-b", "lane")
    (root / "merged.py").write_text("def m():\n    return 1\n")
    (root / "notes.md").write_text("docs\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "lane work")
    base_branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(root, "checkout", "-q", base_branch)
    (root / "other.py").write_text("def o():\n    return 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "main work")
    _git(root, "merge", "--no-ff", "-m", "merge lane", "lane")
    head = _git(root, "rev-parse", "HEAD")

    state = root / ".claude" / "state"
    state.mkdir(parents=True)
    lines: list = []
    runs: list = []
    records: list = []

    def _run(python, analyzer, argv, timeout):
        listing = Path(argv[argv.index("--only-files-from") + 1]).read_text().splitlines()
        runs.append(listing)
        return chc.AnalyzerResult(True, "ok", len(listing), 2, 3)

    out = chc.catchup(
        str(root), "/x/analyze.py", sys.executable, state, lines.append,
        deps=chc.Deps(
            resolve=lambda _r: _Cfg(code_graph_last_analyzed_commit=recorded),
            run_analyzer=_run,
            record=lambda pid, commit: (records.append((pid, commit)) or (True, "recorded")),
        ),
    )
    assert out == "recorded", lines
    # Exactly the two code files the merge/commits changed — not notes.md.
    assert sorted(Path(p).name for p in runs[0]) == ["merged.py", "other.py"], runs
    assert records == [("pid-1", head)]
    assert chc.is_ancestor(str(root), recorded, head) is True


@pytest.mark.skipif(not shutil.which("git"), reason="git required")
def test_real_git_rewritten_history_is_not_an_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    ghost = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    head = _git(root, "rev-parse", "HEAD")
    assert chc.is_ancestor(str(root), ghost, head) is False
    assert chc.is_ancestor(str(root), head, head) is True
    assert chc.diff_paths(str(root), head, head) == []


# ── the hub recording route (loopback fake hub) ────────────────────────────


class _FakeHub(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", "0"))
        _FakeHub.seen.append((self.path, self.headers.get("Authorization"),
                              json.loads(self.rfile.read(n))))
        self.send_response(_FakeHub.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"recorded": true}')

    def log_message(self, format, *args):  # noqa: A002 — base API name; silence output
        pass


@pytest.fixture
def fake_hub(monkeypatch):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
    monkeypatch.setenv("VCT_HUB_PORT", str(srv.server_address[1]))
    monkeypatch.setenv("VCT_HUB_TOKEN", "tok-test")
    from vco_lib import project_config
    project_config._test_clear_cache()
    _FakeHub.seen = []
    _FakeHub.status = 200
    yield _FakeHub
    srv.shutdown()
    project_config._test_clear_cache()


def test_record_posts_to_the_hub_route(fake_hub) -> None:
    ok, why = chc.record_analyzed_commit("pid-1", "abc1234")
    assert ok, why
    path, auth, body = fake_hub.seen[0]
    assert path == "/api/v1/projects/pid-1/codegraph/analyzed"
    assert auth == "Bearer tok-test"
    assert body == {"commit": "abc1234"}


def test_record_reports_a_refusal(fake_hub) -> None:
    fake_hub.status = 404
    ok, why = chc.record_analyzed_commit("pid-1", "abc1234")
    assert not ok and "404" in why


def test_record_under_the_test_gate_is_unreachable() -> None:
    # conftest sets VCT_DISABLE_HUB_RESOLVER=1 — the POST must not leave.
    ok, why = chc.record_analyzed_commit("pid", "abc1234")
    assert not ok and "unreachable" in why


# ── the drain hook spawns the catch-up, once per interval ─────────────────


def _shim_python(path: Path, log: Path) -> None:
    path.write_text("#!/bin/sh\n" f"printf '%s\\n' \"$*\" >> '{log}'\n")
    path.chmod(0o755)


def _wait_lines(p: Path, want: int, tries: int = 100) -> list:
    for _ in range(tries):
        if p.exists():
            lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
            if len(lines) >= want:
                return lines
        time.sleep(0.05)
    return [ln for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def _catchup_lines(p: Path, tries: int = 100) -> list:
    """Lines recording a catch-up spawn (the extras block also spawns here)."""
    marker = "-m vco_lib.codegraph_head_catchup"
    for _ in range(tries):
        lines = _wait_lines(p, 1) if p.exists() else []
        hits = [ln for ln in lines if marker in ln]
        if hits:
            return hits
        time.sleep(0.05)
    return [ln for ln in _wait_lines(p, 1) if marker in ln]


def _hook_case(tmp_path: Path, runner: list) -> None:
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True)
    spawn_log = tmp_path / "spawned.log"
    shim = tmp_path / "py.sh"
    _shim_python(shim, spawn_log)
    stub = tmp_path / "analyze.py"
    stub.write_text("")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), "VCT_PYTHON": str(shim),
           "VCT_ANALYZER_SCRIPT": str(stub),
           "VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS": "600",
           "VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS": "600"}
    stdin = json.dumps({"session_id": "s1"})

    # No queue file at all — a merge produces none, yet the catch-up must run.
    r = subprocess.run(runner, input=stdin, capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    hits = _catchup_lines(spawn_log)
    assert len(hits) == 1, hits
    assert hits[0].startswith("-m vco_lib.codegraph_head_catchup --project-root "), hits
    assert f"--analyzer {stub}" in hits[0] and "--state-dir " in hits[0], hits
    assert (root / ".claude" / "state" / "codegraph_head_catchup.ts").is_file()

    # Inside the interval: no second spawn.
    subprocess.run(runner, input=stdin, capture_output=True, text=True,
                   timeout=60, env=env)
    time.sleep(1.0)
    assert len(_catchup_lines(spawn_log)) == 1


@pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("python3")), reason="bash required")
def test_drain_hook_spawns_the_catchup_bash(tmp_path: Path) -> None:
    _hook_case(tmp_path, ["bash", str(DRAIN_SH)])


@pytest.mark.skipif(not shutil.which("pwsh") or os.name == "nt", reason="pwsh (POSIX) required")
def test_drain_hook_spawns_the_catchup_pwsh(tmp_path: Path) -> None:
    _hook_case(tmp_path, ["pwsh", *PWSH_BASE_FLAGS, "-File", str(DRAIN_PS1)])


# ── v0.2.105 fix round (review MEDIUM): a soft-skipped run is not success ──
#
# The analyzer exits 0 when no embedding backend answers, printing
# "Code-graph analysis skipped: …" to stderr and indexing nothing. Reading
# only `ok` recorded HEAD over a graph that was never walked (the review's
# repro drove the REAL run_analyzer against such a stub). The check lives in
# ONE home (vco_lib.codegraph_analyzer_outcome) so this catch-up and the
# resync driver cannot drift — the driver had the fix, the catch-up did not.

_SOFT_SKIP_STUB = (
    "import sys\n"
    "print('\\u26a0\\ufe0f  Code-graph analysis skipped: active code backend "
    "(slot=codesage_embed) is not reachable.', file=sys.stderr)\n"
    "sys.exit(0)\n"
)

_ANALYZED_STUB = (
    "import json, sys\n"
    "print(json.dumps({'final': True, 'files_analyzed': 3, 'modules': 3}))\n"
    "sys.exit(0)\n"
)


def _real_runner_deps(rec: _Recorder) -> "chc.Deps":
    """The recorder's git plumbing + the REAL ``run_analyzer`` (the review's
    repro shape: the subprocess really runs and really soft-skips)."""
    return chc.Deps(
        resolve=rec.resolve, git_head=rec.git_head, is_ancestor=rec.is_ancestor,
        diff_paths=rec.diff_paths, run_analyzer=chc.run_analyzer,
        record=rec.record, canonical_root=rec.canonical_root,
    )


def test_a_soft_skipped_full_walk_records_nothing(tmp_path: Path) -> None:
    """ACT: the REAL ``run_analyzer`` against an analyzer that soft-skips
    (exit 0, nothing indexed) must fail the walk — nothing is recorded, so
    the next eligible catch-up retries instead of diffing from a HEAD the
    graph never reached. RED-PROOF: dropping the soft-skip check from
    ``run_analyzer`` turns this into ``recorded``."""
    stub = tmp_path / "stub_analyzer.py"
    stub.write_text(_SOFT_SKIP_STUB, encoding="utf-8")
    rec = _Recorder(head=B, recorded=None)   # no baseline → the full walk
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    logs: list = []
    out = chc.catchup(
        str(root), str(stub), sys.executable, root / ".claude" / "state",
        logs.append, deps=_real_runner_deps(rec),
    )
    assert out == "analyzer_failed", out
    assert rec.records == [], "a skipped walk must never advance the baseline"
    assert any("analysis skipped" in ln for ln in logs), logs


def test_the_same_walk_that_really_analyzed_records_head(tmp_path: Path) -> None:
    """LEAVE-ALONE: an analyzer that ran (exit 0, a final report, no skip
    line) still records HEAD — the check must not turn every run red."""
    stub = tmp_path / "ok_analyzer.py"
    stub.write_text(_ANALYZED_STUB, encoding="utf-8")
    rec = _Recorder(head=B, recorded=None)
    root = tmp_path / "proj"
    (root / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    out = chc.catchup(
        str(root), str(stub), sys.executable, root / ".claude" / "state",
        lambda _m: None, deps=_real_runner_deps(rec),
    )
    assert out == "recorded", out
    assert rec.records == [("pid-1", B)]


def test_the_soft_skip_marker_matches_the_analyzer_literal() -> None:
    """The one home's marker is the analyzer's own text (A>B>C rule C: a
    mirror locked by a parity test). If the analyzer reworded its skip line
    every consumer would silently stop noticing — this pins it."""
    from vco_lib.codegraph_analyzer_outcome import ANALYZER_SOFT_SKIP_MARKER

    src = (REPO_ROOT / "templates" / "scripts" / "analyze_code_graph.py").read_text(
        encoding="utf-8"
    )
    assert src.count(ANALYZER_SOFT_SKIP_MARKER) >= 2, (
        "the analyzer no longer prints ANALYZER_SOFT_SKIP_MARKER verbatim"
    )


# ── v0.2.105 fix round (review LOW): back off after repeated failures ──────


def _failing_runs(tmp_path: Path, rec: _Recorder, clock: dict, n: int) -> list:
    outs = []
    for _ in range(n):
        out, _lines, _root = _drive(tmp_path, rec, now=lambda: clock["t"])
        outs.append(out)
    return outs


def test_consecutive_failures_back_off_instead_of_retrying_every_interval(
    tmp_path: Path,
) -> None:
    """ACT: a full walk that keeps failing (a tree past the timeout, or a hub
    refusing the stamp) must not restart on every Stop after the interval.
    RED-PROOF: without the gate the fourth attempt runs the analyzer again."""
    clock = {"t": 1_000_000.0}
    rec = _Recorder(head=B, recorded=None, ok=False)
    outs = _failing_runs(tmp_path, rec, clock, chc.BACKOFF_AFTER_FAILURES)
    assert outs == ["analyzer_failed"] * chc.BACKOFF_AFTER_FAILURES
    runs_before = len(rec.runs)
    out, lines, _root = _drive(tmp_path, rec, now=lambda: clock["t"])
    assert out == "backoff"
    assert len(rec.runs) == runs_before, "no analyzer run while backing off"
    assert any("backoff" in ln for ln in lines), lines
    # The window elapses → one more attempt is allowed (and fails again).
    clock["t"] += chc.BACKOFF_BASE_SECONDS + 1
    out2, _l2, _r2 = _drive(tmp_path, rec, now=lambda: clock["t"])
    assert out2 == "analyzer_failed"
    assert len(rec.runs) == runs_before + 1


def test_a_recorded_run_clears_the_backoff(tmp_path: Path) -> None:
    """LEAVE-ALONE: a healthy project never sees the gate — one recorded run
    drops the counter."""
    clock = {"t": 1_000_000.0}
    rec = _Recorder(head=B, recorded=None, ok=False)
    _failing_runs(tmp_path, rec, clock, chc.BACKOFF_AFTER_FAILURES)
    state = tmp_path / "proj" / ".claude" / "state"
    assert chc.backoff_state_path(state, "pid-1").exists()
    rec.ok = True
    clock["t"] += chc.BACKOFF_BASE_SECONDS + 1
    out, _lines, _root = _drive(tmp_path, rec, now=lambda: clock["t"])
    assert out == "recorded"
    assert not chc.backoff_state_path(state, "pid-1").exists()


def test_an_up_to_date_tree_clears_the_backoff(tmp_path: Path) -> None:
    """LEAVE-ALONE: when the tree already sits at the recorded baseline there
    is nothing to converge, so a stale failure chain must not linger."""
    clock = {"t": 1_000_000.0}
    rec = _Recorder(head=B, recorded=None, ok=False)
    _failing_runs(tmp_path, rec, clock, chc.BACKOFF_AFTER_FAILURES)
    state = tmp_path / "proj" / ".claude" / "state"
    assert chc.backoff_state_path(state, "pid-1").exists()
    rec.recorded = B          # someone else got the graph current
    out, _lines, _root = _drive(tmp_path, rec, now=lambda: clock["t"])
    assert out == "up_to_date"
    assert not chc.backoff_state_path(state, "pid-1").exists()


def test_the_backoff_window_doubles_and_is_capped() -> None:
    assert chc.backoff_window_seconds(0) == 0
    assert chc.backoff_window_seconds(chc.BACKOFF_AFTER_FAILURES - 1) == 0
    assert (
        chc.backoff_window_seconds(chc.BACKOFF_AFTER_FAILURES)
        == chc.BACKOFF_BASE_SECONDS
    )
    assert (
        chc.backoff_window_seconds(chc.BACKOFF_AFTER_FAILURES + 1)
        == 2 * chc.BACKOFF_BASE_SECONDS
    )
    assert chc.backoff_window_seconds(500) == chc.BACKOFF_MAX_SECONDS


# ── v0.2.105 fix round (review LOW): the record route is project-scoped ────


@pytest.fixture
def on_disk_tokens(monkeypatch, tmp_path):
    """Discovery WITHOUT the ``VCT_HUB_TOKEN`` pin: the on-disk tokens decide
    (the state dir is isolated so the real ``~/.vct`` is never read)."""
    monkeypatch.delenv("VCT_HUB_TOKEN", raising=False)
    root = tmp_path / "vct"
    root.mkdir()
    monkeypatch.setenv("VCT_STATE_DIR", str(root))
    from vco_lib import project_config
    project_config._test_clear_cache()
    yield root
    project_config._test_clear_cache()


def test_record_presents_the_project_scoped_token(fake_hub, on_disk_tokens) -> None:
    """ACT: ``/codegraph/analyzed`` requires ``hub.token.<project_id>`` (the
    same gate as /env and /config), so the caller must present it — the
    global ``hub.token`` is refused there."""
    (on_disk_tokens / "hub.token").write_text("global-tok", encoding="utf-8")
    (on_disk_tokens / "hub.token.pid-1").write_text("scoped-tok", encoding="utf-8")
    from vco_lib import project_config
    project_config._test_clear_cache()
    ok, why = chc.record_analyzed_commit("pid-1", "abc1234")
    assert ok, why
    path, auth, body = fake_hub.seen[0]
    assert path == "/api/v1/projects/pid-1/codegraph/analyzed"
    assert auth == "Bearer scoped-tok"
    assert body == {"commit": "abc1234"}


def test_a_refusing_hub_is_retried_once_with_the_scoped_token(
    monkeypatch, tmp_path
) -> None:
    """LEAVE-ALONE (the stale-pin shape the resolvers already handle): an
    exported ``VCT_HUB_TOKEN`` wins over the per-project file, so the POST
    would 403 forever. ONE bounded retry with the on-disk scoped token."""
    accepted = "scoped-tok"
    seen: list = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — base API name
            n = int(self.headers.get("Content-Length", "0"))
            auth = self.headers.get("Authorization")
            seen.append(auth)
            self.rfile.read(n)
            code = 200 if auth == f"Bearer {accepted}" else 403
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"recorded": true}')

        def log_message(self, format, *args):  # noqa: A002 — base API name
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.delenv("VCT_DISABLE_HUB_RESOLVER", raising=False)
        monkeypatch.delenv("VCT_HUB_TOKEN_STRICT", raising=False)
        monkeypatch.setenv("VCT_HUB_PORT", str(srv.server_address[1]))
        monkeypatch.setenv("VCT_HUB_TOKEN", "stale-env-pin")
        root = tmp_path / "vct"
        root.mkdir()
        (root / "hub.token").write_text("global-tok", encoding="utf-8")
        (root / "hub.token.pid-1").write_text(accepted, encoding="utf-8")
        monkeypatch.setenv("VCT_STATE_DIR", str(root))
        from vco_lib import project_config
        project_config._test_clear_cache()
        ok, why = chc.record_analyzed_commit("pid-1", "abc1234")
        assert ok, why
        assert seen == ["Bearer stale-env-pin", f"Bearer {accepted}"], seen
    finally:
        srv.shutdown()
        from vco_lib import project_config
        project_config._test_clear_cache()
