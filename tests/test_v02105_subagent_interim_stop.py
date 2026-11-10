# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — an interim SubagentStop must not read as a finished agent.

A subagent that starts its own background work (a test suite, a build) stops
and resumes several times, and every interim stop fires Claude Code's
SubagentStop event. The harness payload carries no field that separates an
interim stop from a final one: SubagentStop input is ``session_id``,
``transcript_path``, ``cwd``, ``permission_mode``, ``hook_event_name``,
``stop_hook_active``, ``agent_id``, ``agent_type``, ``agent_transcript_path``,
``last_assistant_message``, ``effort``, ``background_tasks`` and
``session_crons`` (https://code.claude.com/docs/en/hooks#subagentstop-input) —
``background_tasks`` / ``session_crons`` are explicitly scoped to the PARENT
session, not the subagent, and ``effort`` does not separate the two either.

So VCO's answer is the honest one: the "worktree KEPT" notice is sent ONCE
per worktree path per session, and it says the agent may still be running and
will report after its final report. These tests pin that behaviour for both
siblings of ``subagent-stop-reconcile`` and the shipped coordinators' guidance
in the two CLAUDE.md templates.

Two gates decide "once" and they are NOT interchangeable. In the ordinary
sequence the second stop of one launch is already suppressed by the
PRE-EXISTING ``already_handled`` gate in ``wtrec_reap`` / ``Invoke-WtrecReap``
(the first stop's reap flips ``status`` off ``created``), before the marker
helper is called at all. The per-path marker is the belt to that suspender:
it covers a record re-made re-reapable that still carries ``kept_alert_*``
and the detached-job race (stop 2's job reaps before stop 1's job has written
its status). ``_as_if_resumed`` below constructs the first of those states
SYNTHETICALLY to isolate the marker's own contract.

POSIX-only for the hook tests (the hooks are bash); the ``.ps1`` arm runs
where pwsh exists, exactly as the shared ``FLAVOURS`` list decides.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from tests.test_v02102_worktree_guard_no_head import (
    FLAVOURS,
    _LEAK_ENV_KEYS,
    _PS,
    _committed_repo,
    _launch,
    _record,
    _run,
    _stop,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
LIB = TEMPLATES / "hooks" / "_lib"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _record_file(repo: Path, agent_id: str) -> Path:
    return repo / ".claude" / "state" / "worktree-guard" / f"wt-agent-{agent_id}.json"


def _as_if_resumed(repo: Path, agent_id: str) -> None:
    """Make the record re-reapable so the next stop runs the whole reap
    decision again — the state the marker alone has to suppress.

    SYNTHETIC, deliberately: no shipped writer puts ``status: created`` back
    on an existing record. ``wtrec_write`` / ``Write-WtrecRecord`` is a FULL
    rewrite that drops ``kept_alert_*`` (a real relaunch therefore re-alerts
    because the marker is GONE, not because the marker failed), and a
    WorktreeCreate re-fire on an existing worktree does not rewrite the
    record at all (``worktree-guard`` logs ``idempotent_existing_worktree``
    and exits without ``record_launch``). Left untouched, a second stop is
    already suppressed by the PRE-EXISTING ``already_handled`` gate in
    ``wtrec_reap`` — so this helper is what isolates the MARKER's contract:
    with the reap running again, the marker alone must suppress the notice.
    """
    f = _record_file(repo, agent_id)
    rec = json.loads(f.read_text(encoding="utf-8-sig"))
    rec["status"] = "created"
    f.write_text(json.dumps(rec), encoding="utf-8")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_kept_notice_is_interim_worded_and_sent_once_per_path_per_session(
        flavour: str, tmp_path: Path):
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "aaa111")
    (wt / "new.py").write_text("X = 1\n", encoding="utf-8")

    first = _stop(flavour, "aaa111", repo)
    assert first.returncode == 0, first.stderr
    assert "KEPT at " + str(wt) in first.stderr
    # The wording must not read as "the agent is done".
    assert "it will resume" in first.stderr
    assert "after its final report" in first.stderr

    # The agent resumes, does more work, and stops again: the parent already
    # has this worktree's notice for this session.
    _as_if_resumed(repo, "aaa111")
    (wt / "new.py").write_text("X = 2\n", encoding="utf-8")
    second = _stop(flavour, "aaa111", repo)
    assert second.returncode == 0, second.stderr
    assert "KEPT at " + str(wt) not in second.stderr

    rec = _record(repo, "aaa111")
    assert rec["kept_alert_path"] == str(wt)
    assert rec["kept_alert_session"] == "s1"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_new_session_is_told_again(flavour: str, tmp_path: Path):
    """LEAVE-ALONE: the dedupe is per SESSION. A later session that sees the
    same kept worktree must still be told (the record is not a one-shot)."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "bbb222")
    (wt / "new.py").write_text("X = 1\n", encoding="utf-8")
    first = _stop(flavour, "bbb222", repo)
    assert "KEPT at " + str(wt) in first.stderr

    _as_if_resumed(repo, "bbb222")
    payload = {"hook_event_name": "SubagentStop", "session_id": "s2",
               "agent_id": "bbb222", "agent_type": "expert-coder",
               "cwd": str(repo)}
    second = _run("subagent-stop-reconcile", flavour, payload, repo)
    assert second.returncode == 0, second.stderr
    assert "KEPT at " + str(wt) in second.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_parent_session_background_tasks_do_not_suppress_the_notice(
        flavour: str, tmp_path: Path):
    """The docs scope ``background_tasks`` / ``session_crons`` to the PARENT
    session, so they cannot prove THIS subagent is waiting on its own work.
    Acting on them would silently drop a real kept worktree; the hook reports
    once instead — never a guess in either direction."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "ccc333")
    (wt / "new.py").write_text("X = 1\n", encoding="utf-8")
    payload = {
        "hook_event_name": "SubagentStop", "session_id": "s1",
        "agent_id": "ccc333", "agent_type": "expert-coder", "cwd": str(repo),
        "background_tasks": [{"id": "task-1", "type": "shell",
                              "status": "running", "description": "pytest -q"}],
        "session_crons": [],
    }
    proc = _run("subagent-stop-reconcile", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    assert "KEPT at " + str(wt) in proc.stderr


def test_shipped_templates_carry_the_interim_notice():
    """Where coordinators and lanes read it: both shipped CLAUDE.md
    templates tell them an interim subagent notification is not a completion,
    and that a waiting lane ends its turn — since the harness backgrounds a
    subagent's long command itself and the completion notification wakes
    THAT subagent, the pre-2.1.318 "helpers in the FOREGROUND" rule is gone."""
    for name in ("CLAUDE.md.template", "ORCHESTRATOR-CLAUDE.md.template"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "waiting on its own background work is INTERIM" in text, name
        assert "do not tell the user it finished or stalled" in text, name
        assert "the completion notification wakes THAT subagent" in text, name
        assert "runs its own helpers in the FOREGROUND" not in text, name
        assert "reaches the root session" not in text, name


# ── the marker helper, called directly ─────────────────────────────────────
# The hook always hands the helper the SAME path string, so the parity
# divergences (case sensitivity, a non-object record, the atomic write) are
# only reachable by calling the helper itself — exactly what these do. Exit 0
# is "emit" (tell the parent), exit 1 is "already told".


def _helper(flavour: str, file: Path, sid: str, path: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    if flavour == "sh":
        env["PY"] = sys.executable
        argv = ["bash", "-c", '. "$1"; shift; wtrec_kept_alert_once "$@"', "_",
                str(LIB / "worktree-records.sh"), str(file), sid, path]
    else:
        assert _PS is not None
        env.update(WT_LIB=str(LIB / "worktree-records.ps1"), WT_FILE=str(file),
                   WT_SID=sid, WT_PATH=path)
        argv = [_PS, *PWSH_BASE_FLAGS, "-Command",
                ". $env:WT_LIB; if (Test-WtrecKeptAlertOnce $env:WT_FILE $env:WT_SID $env:WT_PATH) { exit 0 } else { exit 1 }"]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_marker_comparison_is_case_sensitive(flavour: str, tmp_path: Path):
    """Exact parity with the .sh's `==`: `-eq` is case-INSENSITIVE, so two
    paths differing only in case deduped on Windows and not on POSIX."""
    f = tmp_path / "wt-agent-aaaa.json"
    f.write_text(json.dumps({"name": "agent-aaaa", "status": "kept",
                             "kept_alert_path": "/x/WorkTree",
                             "kept_alert_session": "s1"}), encoding="utf-8")
    # Same path and session: already told.
    same = _helper(flavour, f, "s1", "/x/WorkTree")
    assert same.returncode == 1, same.stderr
    # Differs only in case: a DIFFERENT path, so the parent is told again.
    other = _helper(flavour, f, "s1", "/x/worktree")
    assert other.returncode == 0, other.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_non_object_record_is_emitted_for_and_never_rewritten(
        flavour: str, tmp_path: Path):
    """A valid-JSON non-object record (hand-corrupted) must fail OPEN without
    being rewritten: enumerating an array's own members would mangle it."""
    f = tmp_path / "wt-agent-bbbb.json"
    raw = "[1, 2, 3]"
    f.write_text(raw, encoding="utf-8")
    proc = _helper(flavour, f, "s1", "/x/wt")
    assert proc.returncode == 0, proc.stderr
    assert f.read_text(encoding="utf-8") == raw, "a non-object record must not be rewritten"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_marker_write_is_atomic_through_a_temp_file(flavour: str, tmp_path: Path):
    """Both siblings write the marker through `<file>.tmp` and rename it. The
    temp path is obstructed here, so a non-atomic writer would still rewrite
    the real record; the atomic one fails open and leaves it byte-identical."""
    f = tmp_path / "wt-agent-cccc.json"
    raw = json.dumps({"name": "agent-cccc", "status": "kept"})
    f.write_text(raw, encoding="utf-8")
    (tmp_path / "wt-agent-cccc.json.tmp").mkdir()
    proc = _helper(flavour, f, "s1", "/x/wt")
    assert proc.returncode == 0, proc.stderr          # fail-open: tell the parent
    assert f.read_text(encoding="utf-8") == raw, "a failed marker write must leave the record intact"


# ── LOW-1: the fail-open contract ──────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_missing_record_emits(flavour: str, tmp_path: Path):
    proc = _helper(flavour, tmp_path / "wt-agent-dddd.json", "s1", "/x/wt")
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_an_unreadable_record_emits_and_is_not_touched(flavour: str, tmp_path: Path):
    f = tmp_path / "wt-agent-eeee.json"
    raw = "{not json"
    f.write_text(raw, encoding="utf-8")
    proc = _helper(flavour, f, "s1", "/x/wt")
    assert proc.returncode == 0, proc.stderr
    assert f.read_text(encoding="utf-8") == raw


# ── LOW-2: the channel the coordinator actually reads ──────────────────────


def _alerts(project: Path) -> list[dict]:
    rows: list[dict] = []
    for path in sorted((project / ".claude" / "state").glob("agent_alerts_*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_interim_alert_is_filed_for_the_parent_once(
        flavour: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The detached job's stderr reaches no model; the parent-facing channel is
    the filed alert. It must carry the interim wording once — not twice."""
    # The alert writer needs a VCO-capable interpreter; point the resolver's
    # tier 1 at the one running this suite (it can import the checkout).
    monkeypatch.setenv("VCT_VENV", sys.executable)
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "ffff66")
    (wt / "new.py").write_text("X = 1\n", encoding="utf-8")

    first = _stop(flavour, "ffff66", repo)
    assert first.returncode == 0, first.stderr
    rows = _alerts(repo)
    assert [r["kind"] for r in rows] == ["worktree_kept"]
    assert "it will resume" in rows[0]["message"]
    assert "after its final report" in rows[0]["message"]
    assert str(wt) in rows[0]["message"]

    _as_if_resumed(repo, "ffff66")
    second = _stop(flavour, "ffff66", repo)
    assert second.returncode == 0, second.stderr
    assert len(_alerts(repo)) == 1, "the second stop must not file a second alert"


# ── LOW-3: a crashed marker write leaves no unbounded residue ───────────────


def _prune(flavour: str, project: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env["HOME"] = str(project.parent / "fakehome")
    if flavour == "sh":
        env["PY"] = sys.executable
        argv = ["bash", "-c", '. "$1"; wtrec_prune "$2"', "_",
                str(LIB / "worktree-records.sh"), str(project)]
    else:
        assert _PS is not None
        env.update(WT_LIB=str(LIB / "worktree-records.ps1"), WT_ROOT=str(project))
        argv = [_PS, *PWSH_BASE_FLAGS, "-Command", ". $env:WT_LIB; Remove-WtrecStale $env:WT_ROOT"]
    return subprocess.run(argv, capture_output=True, text=True, env=env,
                          cwd=str(project), timeout=120)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_crashed_marker_temp_file_is_pruned(flavour: str, tmp_path: Path):
    """A crash between the marker's tmp write and its rename leaves
    `wt-*.json.tmp`, which no exact-name lookup reads. It must age out on the
    same idle window as the record; a fresh one may still be mid-write."""
    project = tmp_path / "proj"
    d = project / ".claude" / "state" / "worktree-guard"
    d.mkdir(parents=True)
    stale = d / "wt-agent-old.json.tmp"
    stale_bak = d / "wt-agent-old.json.bak"
    fresh = d / "wt-agent-new.json.tmp"
    record = d / "wt-agent-old.json"
    for p in (stale, stale_bak, fresh, record):
        p.write_text("{}", encoding="utf-8")
    old = time.time() - 90 * 86400  # > max(30, 30) + 30 days
    for p in (stale, stale_bak):
        os.utime(p, (old, old))

    proc = _prune(flavour, project)
    assert proc.returncode == 0, proc.stderr
    assert not stale.exists(), "a stale marker tmp must not accumulate"
    assert not stale_bak.exists(), "a stale marker backup must not accumulate"
    assert fresh.exists(), "a fresh marker tmp may still be mid-write"
    assert record.exists(), "records keep their own idle window"


# ── SimRacing 2026-11-04: the KEPT notice names what to merge ───────────────
def _wt_git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], capture_output=True,
                          text=True, check=True, timeout=60).stdout.strip()


def _commit_in(wt: Path) -> None:
    (wt / "new.py").write_text("X = 1\n", encoding="utf-8")
    _wt_git(wt, "add", "new.py")
    _wt_git(wt, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
            "commit", "-q", "-m", "lane work")


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_kept_notice_names_the_sha_of_a_detached_worktree(
        flavour: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A harness-style lane worktree is on a DETACHED HEAD, so
    ``git log base..<branch>`` finds nothing: the notice must carry the SHA
    to merge by, on stderr and in the alert filed for the parent."""
    monkeypatch.setenv("VCT_VENV", sys.executable)
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "abc777")
    _commit_in(wt)
    sha = _wt_git(wt, "rev-parse", "--short=12", "HEAD")

    proc = _stop(flavour, "abc777", repo)
    assert proc.returncode == 0, proc.stderr
    assert f"detached HEAD {sha} - merge it by that SHA" in proc.stderr
    rows = _alerts(repo)
    assert [r["kind"] for r in rows] == ["worktree_kept"]
    assert f"detached HEAD {sha}" in rows[0]["message"]


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_kept_notice_names_the_branch_when_the_worktree_has_one(
        flavour: str, tmp_path: Path):
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "abc888")
    _wt_git(wt, "switch", "-q", "-c", "lane-x")
    _commit_in(wt)

    proc = _stop(flavour, "abc888", repo)
    assert proc.returncode == 0, proc.stderr
    assert "branch lane-x" in proc.stderr
    assert "detached HEAD" not in proc.stderr
