# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.107 REAP-INTERIM — an isolated agent's worktree is never removed at
SubagentStop.

SubagentStop also fires on an INTERIM stop (the agent ends its turn waiting on
its own background work or a lock, and resumes later), and its payload cannot
tell interim from final (https://code.claude.com/docs/en/hooks
#subagentstop-input). Removing an unchanged worktree there removed it under a
paused agent, and SendMessage could no longer resume it ("its worktree no
longer exists"). Now:

  * SubagentStop marks an unchanged worktree ``status: reap_candidate`` (a
    changed one ``kept``) and leaves it on disk;
  * every SessionStart (``session-worktree-reap``) removes an unchanged
    candidate idle for 7 days, and a kept lane whose commits are merged into
    the main checkout's branch and that is clean (owner rulings 2026-11-20),
    never one of the starting session, re-checking every condition first;
    any doubt (lock, a process working in it, git failure, detached main,
    record mismatch) keeps it.

Both siblings, act + leave-alone for every destructive branch.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.test_v02102_worktree_guard_no_head import (
    FLAVOURS,
    _LEAK_ENV_KEYS,
    _committed_repo,
    _git,
    _launch,
    _record,
    _reap_rows,
    _run,
)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _stop(flavour: str, agent_id: str, project: Path, sid: str = "s1", transcript: str = ""):
    payload = {"hook_event_name": "SubagentStop", "session_id": sid, "agent_id": agent_id,
               "agent_type": "expert-coder", "cwd": str(project)}
    if transcript:
        payload["agent_transcript_path"] = transcript
    proc = _run("subagent-stop-reconcile", flavour, payload, project)
    assert proc.returncode == 0, proc.stderr
    return proc


def _session(flavour: str, event: str, project: Path, sid: str, source: str = "startup"):
    payload = {"hook_event_name": event, "session_id": sid, "cwd": str(project)}
    if event == "SessionStart":
        payload["source"] = source
    else:
        payload["reason"] = "prompt_input_exit"
    proc = _run("session-worktree-reap", flavour, payload, project)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", "SessionStart stdout becomes model context: must stay empty"
    return proc


def _rows(project: Path, hook: str) -> list[dict]:
    log = project / ".claude" / "logs" / "worktree-guard.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("hook") == hook]


def _registered(repo: Path, wt: Path) -> bool:
    return f"worktree {wt}" in _git(repo, "worktree", "list", "--porcelain").stdout


# ── SubagentStop: mark, never remove ────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_interim_stop_keeps_the_worktree_as_a_candidate(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "int001")
    proc = _stop(flavour, "int001", repo, transcript=str(tmp_path / "agent.jsonl"))
    assert wt.is_dir() and _registered(repo, wt), "a paused agent's worktree must survive its stop"
    rec = _record(repo, "int001")
    assert rec["status"] == "reap_candidate"
    assert rec["reap_session"] == "s1"
    assert rec["reap_transcript"] == str(tmp_path / "agent.jsonl")
    assert isinstance(rec["reap_marked_epoch"], int)
    assert (_reap_rows(repo)[-1]["decision"], _reap_rows(repo)[-1]["reason"]) == (
        "reap_candidate", "unchanged_reap_deferred")
    assert "KEPT" not in proc.stderr
    # The agent resumes and stops again: still a candidate, still there.
    _stop(flavour, "int001", repo)
    assert wt.is_dir() and _record(repo, "int001")["status"] == "reap_candidate"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_writes_after_an_interim_stop_turn_the_candidate_into_kept_once(
        flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "int002")
    _stop(flavour, "int002", repo)
    (wt / "new.py").write_text("X = 1\n", encoding="utf-8")
    final = _stop(flavour, "int002", repo)
    assert wt.is_dir()
    assert _record(repo, "int002")["status"] == "kept"
    assert final.stderr.count("subagent-stop-reconcile: worktree of agent int002 KEPT at " + str(wt)) == 1
    again = _stop(flavour, "int002", repo)
    assert "KEPT" not in again.stderr, "one notice per worktree"


# ── the sweep, rule (a): an unchanged candidate goes after 7 idle days ──────

_WG = (".claude", "state", "worktree-guard")


def _rec_file(project: Path, agent_id: str) -> Path:
    return project.joinpath(*_WG) / f"wt-agent-{agent_id}.json"


def _age(project: Path, agent_id: str, days: float, transcript: Path | None = None) -> None:
    old = time.time() - days * 86400
    os.utime(_rec_file(project, agent_id), (old, old))
    if transcript is not None:
        os.utime(transcript, (old, old))


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_ended_chat_alone_does_not_remove_an_unchanged_candidate(
        flavour: str, tmp_path: Path) -> None:
    """Owner 2026-11-20 "keep longer": the chat that ran the agent ending is
    not enough — it can still be resumed. Under 7 idle days it stays."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp001")
    _stop(flavour, "swp001", repo, sid="s1")
    _age(repo, "swp001", 6)
    _session(flavour, "SessionEnd", repo, "s1")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _registered(repo, wt)
    assert _record(repo, "swp001")["status"] == "reap_candidate"
    assert not repo.joinpath(*_WG, "session-ended-s1").exists(), "no end marker any more"
    assert _rows(repo, "session-worktree-reap") == []


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_candidate_idle_seven_days_is_removed(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp002")
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _stop(flavour, "swp002", repo, sid="s1", transcript=str(transcript))
    _age(repo, "swp002", 8, transcript)
    _session(flavour, "SessionStart", repo, "s2")
    assert not wt.exists() and not _registered(repo, wt)
    rec = _record(repo, "swp002")
    assert (rec["status"], rec["status_reason"]) == ("removed", "clean")
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["agent_id"], row["resolved_path"]) == ("reap_removed", "swp002", str(wt))
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_recent_transcript_keeps_an_idle_record(flavour: str, tmp_path: Path) -> None:
    """The idle clock is the LATER of record and transcript: an agent whose
    transcript still moves is not abandoned."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp010")
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _stop(flavour, "swp010", repo, sid="s1", transcript=str(transcript))
    _age(repo, "swp010", 8)
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _record(repo, "swp010")["status"] == "reap_candidate"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_candidate_changed_since_its_stop_is_kept_by_the_sweep(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp003")
    _stop(flavour, "swp003", repo, sid="s1")
    (wt / "late.txt").write_text("written after the stop\n", encoding="utf-8")
    _age(repo, "swp003", 8)
    _session(flavour, "SessionStart", repo, "s2")
    assert (wt / "late.txt").is_file()
    rec = _record(repo, "swp003")
    assert (rec["status"], rec["status_reason"]) == ("kept", "has_changes")
    assert _rows(repo, "session-worktree-reap")[-1]["decision"] == "reap_kept"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_current_session_candidate_is_never_swept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp004")
    _stop(flavour, "swp004", repo, sid="s1")
    # Even idle for a month, the session that is starting (a resume, compact
    # or clear of s1) never sweeps its own agents.
    _age(repo, "swp004", 30)
    _session(flavour, "SessionStart", repo, "s1", source="compact")
    assert wt.is_dir()
    assert _record(repo, "swp004")["status"] == "reap_candidate"


# The in-use check reads /proc/<pid>/cwd on Linux and asks lsof on macOS;
# elsewhere (Windows) it cannot tell, and "cannot tell" keeps the worktree too.
_CWD_SCAN = sys.platform.startswith("linux") or sys.platform == "darwin"
_IN_USE_REASON = "in_use" if _CWD_SCAN else "in_use_unknown"


def _idle_process(cwd: Path) -> subprocess.Popen:
    """A process whose cwd is inside the worktree (portable: no `sleep`)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            cwd=str(cwd), env=child_env())


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_locked_idle_candidate_is_deferred(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp005")
    _stop(flavour, "swp005", repo, sid="s1")
    _git(repo, "worktree", "lock", str(wt))
    _age(repo, "swp005", 8)
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _registered(repo, wt)
    assert _record(repo, "swp005")["status"] == "reap_candidate", "a deferral is retried, not final"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"]) == ("reap_deferred", "locked")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_idle_candidate_a_process_works_in_is_deferred(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp006")
    _stop(flavour, "swp006", repo, sid="s1")
    _age(repo, "swp006", 8)
    (wt / "sub").mkdir()  # an empty directory is invisible to git status
    proc = _idle_process(wt / "sub")
    try:
        _session(flavour, "SessionStart", repo, "s2")
    finally:
        proc.kill()
        proc.wait()
    assert wt.is_dir()
    assert _record(repo, "swp006")["status"] == "reap_candidate"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"]) == ("reap_deferred", _IN_USE_REASON)
    if _CWD_SCAN:
        # The process gone, the next sweep removes it.
        _session(flavour, "SessionStart", repo, "s3")
        assert not wt.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_session_end_payload_does_nothing(flavour: str, tmp_path: Path) -> None:
    """The SessionEnd marker is retired with the "chat ended" trigger: a
    SessionEnd payload reaching the hook is a no-op."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp007")
    _stop(flavour, "swp007", repo, sid="s1")
    _age(repo, "swp007", 8)
    _session(flavour, "SessionEnd", repo, "s1")
    assert wt.is_dir()
    assert not repo.joinpath(*_WG, "session-ended-s1").exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_sweep_never_touches_a_created_record(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    running = _launch(flavour, repo, "swp009")  # never stopped: status created
    rec = _record(repo, "swp009")
    rec["reap_session"] = "s1"
    _rec_file(repo, "swp009").write_text(json.dumps(rec), encoding="utf-8")
    _age(repo, "swp009", 30)
    _session(flavour, "SessionStart", repo, "s2")
    assert running.is_dir()
    assert _record(repo, "swp009")["status"] == "created"
    assert _rows(repo, "session-worktree-reap") == []


# ── the sweep, rule (b): a kept lane is removed once merged ─────────────────


def _kept_with_commit(flavour: str, repo: Path, agent_id: str, sid: str = "s1") -> Path:
    wt = _launch(flavour, repo, agent_id)
    (wt / "feature.py").write_text("F = 1\n", encoding="utf-8")
    _git(wt, "add", "feature.py")
    _git(wt, "commit", "-qm", "lane work")
    _stop(flavour, agent_id, repo, sid=sid)
    rec = _record(repo, agent_id)
    assert (rec["status"], rec["reap_session"]) == ("kept", sid)
    return wt


def _merge(repo: Path, wt: Path) -> None:
    _git(repo, "merge", "-q", "--ff-only", _git(wt, "rev-parse", "HEAD").stdout.strip())


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_clean_lane_is_removed(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg001")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s2")
    assert not wt.exists() and not _registered(repo, wt)
    rec = _record(repo, "mrg001")
    assert (rec["status"], rec["status_reason"]) == ("removed", "merged")
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"], row["agent_id"]) == ("reap_removed", "merged", "mrg001")
    assert (repo / "feature.py").read_text(encoding="utf-8") == "F = 1\n"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_with_only_ignored_files_is_removed(flavour: str, tmp_path: Path) -> None:
    """The merged rule reads `git status --porcelain` WITHOUT ignored files
    (build output, caches): owner rule 2026-11-20."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg002")
    (wt / "build").mkdir()
    (wt / "build" / "out.bin").write_text("x\n", encoding="utf-8")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s2")
    assert not wt.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("change", ["untracked", "modified"])
def test_merged_dirty_lane_is_kept(flavour: str, change: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg003")
    _merge(repo, wt)
    if change == "untracked":
        (wt / "notes.md").write_text("unmerged thought\n", encoding="utf-8")
    else:
        (wt / "feature.py").write_text("F = 2\n", encoding="utf-8")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _registered(repo, wt)
    assert _record(repo, "mrg003")["status"] == "kept"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"]) == ("reap_deferred", "has_changes")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_unmerged_lane_is_kept_silently(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg004")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _record(repo, "mrg004")["status"] == "kept"
    assert _rows(repo, "session-worktree-reap") == [], "a lane awaiting merge logs nothing"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_of_the_current_session_is_kept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg005")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s1", source="compact")
    assert wt.is_dir() and _record(repo, "mrg005")["status"] == "kept"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_without_a_recorded_session_is_kept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg006")
    rec = _record(repo, "mrg006")
    del rec["reap_session"]
    _rec_file(repo, "mrg006").write_text(json.dumps(rec), encoding="utf-8")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_locked_lane_is_kept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg007")
    _merge(repo, wt)
    _git(repo, "worktree", "lock", str(wt))
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir()
    assert _rows(repo, "session-worktree-reap")[-1]["reason"] == "locked"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_a_process_works_in_is_kept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg008")
    _merge(repo, wt)
    proc = _idle_process(wt)
    try:
        _session(flavour, "SessionStart", repo, "s2")
    finally:
        proc.kill()
        proc.wait()
    assert wt.is_dir() and _record(repo, "mrg008")["status"] == "kept"
    assert _rows(repo, "session-worktree-reap")[-1]["reason"] == _IN_USE_REASON


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_is_kept_while_the_main_checkout_is_detached(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg009")
    _merge(repo, wt)
    _git(repo, "checkout", "-q", "--detach")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir()
    assert _rows(repo, "session-worktree-reap")[-1]["reason"] == "main_branch_unknown"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_that_does_not_match_its_record_is_kept(flavour: str, tmp_path: Path) -> None:
    """worktree-guard makes head worktrees DETACHED: one now on a branch is
    not the worktree the record describes."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg010")
    _git(wt, "switch", "-q", "-c", "someone-elses")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir()
    assert _rows(repo, "session-worktree-reap")[-1]["reason"] == "record_mismatch"
    assert _git(repo, "show-ref", "--verify", "-q", "refs/heads/someone-elses",
                check=False).returncode == 0


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_lane_whose_git_fails_is_kept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _kept_with_commit(flavour, repo, "mrg011")
    _merge(repo, wt)
    gitfile = wt / ".git"
    saved = gitfile.read_text(encoding="utf-8")
    gitfile.write_text("gitdir: /nonexistent/broken\n", encoding="utf-8")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and (wt / "feature.py").is_file()
    gitfile.write_text(saved, encoding="utf-8")


def _handmade(repo: Path, name: str, mode: str, wt: Path, base: str, **extra: str) -> None:
    d = repo.joinpath(*_WG)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"name": f"agent-{name}", "mode": mode, "path": str(wt), "base": base,
           "toplevel": str(repo), "status": "kept", "reap_session": "s1", **extra}
    (d / f"wt-agent-{name}.json").write_text(json.dumps(rec), encoding="utf-8")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_merged_reservation_lane_is_removed_with_its_branch(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    wt = (tmp_path / "proj.vco-lanes" / "lane-x-r1").resolve()
    _git(repo, "worktree", "add", "-q", "-b", "lane-x", str(wt), base)
    (wt / "feature.py").write_text("F = 1\n", encoding="utf-8")
    _git(wt, "add", "feature.py")
    _git(wt, "commit", "-qm", "lane work")
    _handmade(repo, "res001", "reservation", wt, base, branch="lane-x", branch_created="yes")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s2")
    assert not wt.exists() and not (tmp_path / "proj.vco-lanes").exists()
    assert _git(repo, "show-ref", "--verify", "-q", "refs/heads/lane-x", check=False).returncode != 0
    assert _record(repo, "res001")["status_reason"] == "merged"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_adopted_reservation_worktree_is_never_removed(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    wt = (tmp_path / "proj.vco-lanes" / "adopted").resolve()
    _git(repo, "worktree", "add", "-q", "-b", "adopted", str(wt), base)
    (wt / "feature.py").write_text("F = 1\n", encoding="utf-8")
    _git(wt, "add", "feature.py")
    _git(wt, "commit", "-qm", "lane work")
    _handmade(repo, "res002", "reservation_existing", wt, base, branch="adopted")
    _merge(repo, wt)
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir()
    assert _git(repo, "show-ref", "--verify", "-q", "refs/heads/adopted", check=False).returncode == 0
    assert _record(repo, "res002")["status"] == "kept"


# ── Windows: rely on Windows' lock (owner ruling 2026-11-20) ────────────────
# No process's cwd can be read on Windows, so the in-use check is not a doubt
# there: Windows' own lock is asked first by renaming the folder aside and
# back (Windows refuses that while a process works in it), then the plain
# `git worktree remove` runs; a refusal at either step keeps the worktree and
# its record and logs the reason. Every other doubt still keeps. The Windows
# branch is forced here by running a COPY of the hooks whose lib reports the
# OS as windows (Get-WtrecOsKind / _wtrec_os_kind, the one OS seam); a folder
# Windows would refuse to rename is simulated by a read-only parent folder.
# The probe was added because `git worktree remove` is no lock test: it
# deletes every file it can before it meets the lock and unregisters the
# worktree even then (seen with a read-only parent: an empty folder is left).

_OS_OVERRIDE = {"ps1": '\nfunction Get-WtrecOsKind { return "windows" }\n',
                "sh": "\n_wtrec_os_kind() { echo windows; }\n"}
_PROBE_FREE = {"ps1": '\nfunction Test-WtrecFolderFree([string]$path) { return "free" }\n',
               "sh": "\n_wtrec_folder_free() { echo free; }\n"}


def _windows_hooks(flavour: str, tmp_path: Path, probe_free: bool = False) -> Path:
    from tests.test_v02102_worktree_guard_no_head import HOOKS
    dst = tmp_path / "hooks-win"
    shutil.copytree(HOOKS, dst)
    lib = dst / "_lib" / f"worktree-records.{flavour}"
    with lib.open("a", encoding="utf-8") as fh:
        fh.write(_OS_OVERRIDE[flavour])
        if probe_free:
            fh.write(_PROBE_FREE[flavour])
    return dst


def _win_session(flavour: str, hooks: Path, project: Path, sid: str) -> None:
    from tests.common.pwsh import PWSH_BASE_FLAGS
    from tests.test_v02102_worktree_guard_no_head import _PS
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    env["HOME"] = str(project.parent / "fakehome")
    path = hooks / f"session-worktree-reap.{flavour}"
    argv = ["bash", str(path)] if flavour == "sh" else [_PS, *PWSH_BASE_FLAGS, "-File", str(path)]
    payload = {"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(project),
               "source": "startup"}
    proc = subprocess.run(argv, input=json.dumps(payload), capture_output=True, text=True,
                          env=env, cwd=str(project), timeout=90)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""


def _readonly(folder: Path):
    folder.chmod(0o555)
    return folder


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_lib_reports_in_use_as_unsupported(flavour: str, tmp_path: Path) -> None:
    """The seam itself: with the OS reported as windows the in-use check says
    `unsupported`, not `unknown` (which still means "keep")."""
    hooks = _windows_hooks(flavour, tmp_path)
    lib = hooks / "_lib" / f"worktree-records.{flavour}"
    if flavour == "sh":
        argv = ["bash", "-c", f'. "{lib}"; _wtrec_in_use "{tmp_path}"']
    else:
        from tests.common.pwsh import PWSH_BASE_FLAGS
        from tests.test_v02102_worktree_guard_no_head import _PS
        argv = [_PS, *PWSH_BASE_FLAGS, "-Command", f". '{lib}'; Test-WtrecInUse '{tmp_path}'"]
    out = subprocess.run(argv, capture_output=True, text=True, env=child_env(), timeout=60)
    assert out.stdout.strip() == "unsupported", out.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_idle_candidate_is_removed_once_windows_lets_it_go(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "win001")
    _stop(flavour, "win001", repo, sid="s1")
    _age(repo, "win001", 8)
    _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    assert not wt.exists() and not _registered(repo, wt)
    assert _record(repo, "win001")["status"] == "removed"
    assert _rows(repo, "session-worktree-reap")[-1]["decision"] == "reap_removed"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_idle_candidate_in_use_is_kept_whole(flavour: str, tmp_path: Path) -> None:
    """Windows refuses the rename probe (a process works in the folder):
    nothing is deleted, the worktree stays registered, the record stays a
    candidate."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "win002")
    _stop(flavour, "win002", repo, sid="s1")
    _age(repo, "win002", 8)
    parent = _readonly(wt.parent)
    try:
        _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    finally:
        parent.chmod(0o755)
    assert (wt / "app.py").is_file() and _registered(repo, wt), "nothing deleted, still a worktree"
    rec = _record(repo, "win002")
    assert rec["status"] == "reap_candidate", "kept as a candidate: the next sweep asks again"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"]) == ("reap_deferred", "in_use"), row
    assert not list(wt.parent.glob("*.vco-reap-probe-*"))


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_idle_candidate_whose_git_removal_fails_is_kept(flavour: str, tmp_path: Path) -> None:
    """The probe passes but `git worktree remove` still fails: the record
    stays a candidate and git's reason is logged."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "win004")
    _stop(flavour, "win004", repo, sid="s1")
    _age(repo, "win004", 8)
    parent = _readonly(wt.parent)
    try:
        _win_session(flavour, _windows_hooks(flavour, tmp_path, probe_free=True), repo, "s2")
    finally:
        parent.chmod(0o755)
    assert wt.is_dir()
    assert _record(repo, "win004")["status"] == "reap_candidate"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert row["decision"] == "reap_deferred" and row["reason"].startswith("git_refused:"), row


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_probe_that_could_not_put_the_folder_back_is_not_taken_for_gone(
        flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "win005")
    _stop(flavour, "win005", repo, sid="s1")
    _age(repo, "win005", 8)
    aside = wt.parent / f"{wt.name}.vco-reap-probe-4242"
    wt.rename(aside)
    _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    assert aside.is_dir() and (aside / "app.py").is_file()
    assert _record(repo, "win005")["status"] == "reap_candidate", "never marked removed"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"]) == ("reap_deferred", "probe_not_restored"), row


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_probe_puts_a_free_folder_back(flavour: str, tmp_path: Path) -> None:
    hooks = _windows_hooks(flavour, tmp_path)
    lib = hooks / "_lib" / f"worktree-records.{flavour}"
    folder = tmp_path / "lane"
    folder.mkdir()
    (folder / "f.txt").write_text("x\n", encoding="utf-8")
    if flavour == "sh":
        argv = ["bash", "-c", f'. "{lib}"; _wtrec_folder_free "{folder}"']
    else:
        from tests.common.pwsh import PWSH_BASE_FLAGS
        from tests.test_v02102_worktree_guard_no_head import _PS
        argv = [_PS, *PWSH_BASE_FLAGS, "-Command", f". '{lib}'; Test-WtrecFolderFree '{folder}'"]
    out = subprocess.run(argv, capture_output=True, text=True, env=child_env(), timeout=60)
    assert out.stdout.strip() == "free", out.stderr
    assert (folder / "f.txt").read_text(encoding="utf-8") == "x\n"
    assert not list(tmp_path.glob("lane.vco-reap-probe-*"))


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_locked_candidate_is_still_deferred(flavour: str, tmp_path: Path) -> None:
    """Only the in-use check is waived on Windows: a `git worktree lock`
    still keeps the worktree."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "win003")
    _stop(flavour, "win003", repo, sid="s1")
    _git(repo, "worktree", "lock", str(wt))
    _age(repo, "win003", 8)
    _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    assert wt.is_dir() and _registered(repo, wt)
    assert _rows(repo, "session-worktree-reap")[-1]["reason"] == "locked"


def _merged_reservation(repo: Path, tmp_path: Path, name: str) -> Path:
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    wt = (tmp_path / "proj.vco-lanes" / f"{name}-r1").resolve()
    _git(repo, "worktree", "add", "-q", "-b", name, str(wt), base)
    (wt / "feature.py").write_text("F = 1\n", encoding="utf-8")
    _git(wt, "add", "feature.py")
    _git(wt, "commit", "-qm", "lane work")
    _handmade(repo, name, "reservation", wt, base, branch=name, branch_created="yes")
    _merge(repo, wt)
    return wt


def _has_branch(repo: Path, name: str) -> bool:
    return _git(repo, "show-ref", "--verify", "-q", f"refs/heads/{name}", check=False).returncode == 0


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_merged_lane_is_removed_with_its_branch(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _merged_reservation(repo, tmp_path, "winres1")
    _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    assert not wt.exists() and not _has_branch(repo, "winres1")
    assert _record(repo, "winres1")["status_reason"] == "merged"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_merged_lane_in_use_keeps_worktree_and_branch(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _merged_reservation(repo, tmp_path, "winres2")
    lanes = _readonly(wt.parent)
    try:
        _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    finally:
        lanes.chmod(0o755)
    assert (wt / "feature.py").is_file() and _registered(repo, wt), "nothing deleted"
    assert _has_branch(repo, "winres2")
    assert _record(repo, "winres2")["status"] == "kept", "record unchanged: retried next sweep"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["reason"]) == ("reap_deferred", "in_use"), row


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_merged_lane_whose_git_removal_fails_keeps_its_branch(
        flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _merged_reservation(repo, tmp_path, "winres4")
    lanes = _readonly(wt.parent)
    try:
        _win_session(flavour, _windows_hooks(flavour, tmp_path, probe_free=True), repo, "s2")
    finally:
        lanes.chmod(0o755)
    assert wt.is_dir() and _has_branch(repo, "winres4"), "branch -d runs only after a removal"
    assert _record(repo, "winres4")["status"] == "kept"
    row = _rows(repo, "session-worktree-reap")[-1]
    assert row["decision"] == "reap_deferred" and row["reason"].startswith("git_refused:"), row


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_windows_merged_dirty_lane_is_still_kept(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _merged_reservation(repo, tmp_path, "winres3")
    (wt / "notes.md").write_text("unmerged thought\n", encoding="utf-8")
    _win_session(flavour, _windows_hooks(flavour, tmp_path), repo, "s2")
    assert (wt / "notes.md").is_file() and _has_branch(repo, "winres3")
    assert _rows(repo, "session-worktree-reap")[-1]["reason"] == "has_changes"


# ── delivery: the bundle update registers the hook in existing installs ─────

_REPO = Path(__file__).resolve().parents[1]
_TEMPLATES = {"linux": _REPO / "templates" / "settings.json.linux.template",
              "windows": _REPO / "templates" / "settings.json.windows.template"}


def _reap_events(doc: dict) -> dict[str, list]:
    out: dict[str, list] = {}
    for event, groups in doc.get("hooks", {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if "session-worktree-reap." in h.get("command", ""):
                    out.setdefault(event, []).append(g.get("matcher"))
    return out


@pytest.mark.parametrize("os_name", sorted(_TEMPLATES))
def test_template_registers_every_session_start_only(os_name: str) -> None:
    doc = json.loads(_TEMPLATES[os_name].read_text(encoding="utf-8"))
    # One SessionStart group with no matcher (fires on every source); no
    # SessionEnd since the "chat ended" trigger was retired (2026-11-20).
    assert _reap_events(doc) == {"SessionStart": [None]}


@pytest.mark.parametrize("os_name", sorted(_TEMPLATES))
def test_bundle_update_adds_the_registration_to_an_older_install(os_name: str) -> None:
    from vco_lib import settings_merge
    old = json.loads((_REPO / "tests" / "fixtures" / "settings_templates_v0.2.100"
                      / f"{os_name}.json").read_text(encoding="utf-8"))
    assert _reap_events(old) == {}
    template = json.loads(_TEMPLATES[os_name].read_text(encoding="utf-8"))
    merged = settings_merge.smart_merge_settings(old, template)
    assert _reap_events(merged) == {"SessionStart": [None]}
