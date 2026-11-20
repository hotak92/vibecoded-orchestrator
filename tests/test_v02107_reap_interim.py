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

  * SubagentStop marks an unchanged worktree ``status: reap_candidate`` and
    leaves it on disk;
  * SessionEnd records the session's end; the next SessionStart
    (``session-worktree-reap``) removes the candidates of ENDED sessions
    (and of long-idle ones that never recorded an end), never the current
    session's, and only after re-checking that each is still unchanged.

Both siblings, act + leave-alone for every destructive branch.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

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


# ── the sweep: act ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_ended_session_candidate_is_removed_at_the_next_session_start(
        flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp001")
    _stop(flavour, "swp001", repo, sid="s1")
    _session(flavour, "SessionEnd", repo, "s1")
    assert wt.is_dir(), "SessionEnd only records the end"
    _session(flavour, "SessionStart", repo, "s2")
    assert not wt.exists() and not _registered(repo, wt)
    rec = _record(repo, "swp001")
    assert (rec["status"], rec["status_reason"]) == ("removed", "clean")
    row = _rows(repo, "session-worktree-reap")[-1]
    assert (row["decision"], row["agent_id"], row["resolved_path"]) == ("reap_removed", "swp001", str(wt))
    assert (repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_abandoned_candidate_without_an_end_marker_is_removed(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp002")
    transcript = tmp_path / "agent.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    _stop(flavour, "swp002", repo, sid="s1", transcript=str(transcript))
    old = time.time() - 8 * 86400
    rec_file = repo / ".claude" / "state" / "worktree-guard" / "wt-agent-swp002.json"
    os.utime(rec_file, (old, old))
    os.utime(transcript, (old, old))
    _session(flavour, "SessionStart", repo, "s2")
    assert not wt.exists()
    assert _record(repo, "swp002")["status"] == "removed"


# ── the sweep: leave alone ──────────────────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_candidate_changed_since_its_stop_is_kept_by_the_sweep(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp003")
    _stop(flavour, "swp003", repo, sid="s1")
    (wt / "late.txt").write_text("written after the stop\n", encoding="utf-8")
    _session(flavour, "SessionEnd", repo, "s1")
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
    # Even with an end marker on disk and an idle record, the session that is
    # starting (a resume, compact or clear of s1) never sweeps its own agents.
    _session(flavour, "SessionEnd", repo, "s1")
    old = time.time() - 30 * 86400
    os.utime(repo / ".claude" / "state" / "worktree-guard" / "wt-agent-swp004.json", (old, old))
    _session(flavour, "SessionStart", repo, "s1", source="compact")
    assert wt.is_dir()
    assert _record(repo, "swp004")["status"] == "reap_candidate"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_live_other_session_candidate_is_not_swept(flavour: str, tmp_path: Path) -> None:
    """A second chat starting in the same project must not reap the first
    chat's paused agent: s1 never ended."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp005")
    _stop(flavour, "swp005", repo, sid="s1")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir()
    assert _record(repo, "swp005")["status"] == "reap_candidate"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_resumed_session_withdraws_its_end(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp006")
    _stop(flavour, "swp006", repo, sid="s1")
    _session(flavour, "SessionEnd", repo, "s1")
    marker = repo / ".claude" / "state" / "worktree-guard" / "session-ended-s1"
    assert marker.is_file()
    _session(flavour, "SessionStart", repo, "s1", source="resume")
    assert not marker.exists()
    # s1 is live again: a concurrent chat's start leaves its agent alone.
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _record(repo, "swp006")["status"] == "reap_candidate"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_end_marker_older_than_the_candidate_does_not_count(flavour: str, tmp_path: Path) -> None:
    """A stale marker (the session was resumed, and its end hook never
    withdrew it) predates the candidate: not evidence the agent is final."""
    repo = _committed_repo(tmp_path / "proj")
    wt = _launch(flavour, repo, "swp007")
    d = repo / ".claude" / "state" / "worktree-guard"
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "session-ended-s1"
    marker.write_text("", encoding="utf-8")
    old = time.time() - 3600
    os.utime(marker, (old, old))
    _stop(flavour, "swp007", repo, sid="s1")
    _session(flavour, "SessionStart", repo, "s2")
    assert wt.is_dir() and _record(repo, "swp007")["status"] == "reap_candidate"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_sweep_never_touches_a_kept_or_created_record(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    kept = _launch(flavour, repo, "swp008")
    (kept / "work.py").write_text("W = 1\n", encoding="utf-8")
    _stop(flavour, "swp008", repo, sid="s1")
    running = _launch(flavour, repo, "swp009")  # never stopped: status created
    _session(flavour, "SessionEnd", repo, "s1")
    _session(flavour, "SessionStart", repo, "s2")
    assert kept.is_dir() and running.is_dir()
    assert _record(repo, "swp008")["status"] == "kept"
    assert _record(repo, "swp009")["status"] == "created"
    assert _rows(repo, "session-worktree-reap") == []


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
def test_template_registers_session_end_and_every_session_start(os_name: str) -> None:
    doc = json.loads(_TEMPLATES[os_name].read_text(encoding="utf-8"))
    # One SessionStart group with no matcher (fires on every source), one SessionEnd.
    assert _reap_events(doc) == {"SessionStart": [None], "SessionEnd": [None]}


@pytest.mark.parametrize("os_name", sorted(_TEMPLATES))
def test_bundle_update_adds_the_registration_to_an_older_install(os_name: str) -> None:
    from vco_lib import settings_merge
    old = json.loads((_REPO / "tests" / "fixtures" / "settings_templates_v0.2.100"
                      / f"{os_name}.json").read_text(encoding="utf-8"))
    assert _reap_events(old) == {}
    template = json.loads(_TEMPLATES[os_name].read_text(encoding="utf-8"))
    merged = settings_merge.smart_merge_settings(old, template)
    assert _reap_events(merged) == {"SessionStart": [None], "SessionEnd": [None]}
