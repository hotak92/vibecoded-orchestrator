# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 — an interim SubagentStop must not read as a finished agent.

A subagent that starts its own background work (a test suite, a build) stops
and resumes several times, and every interim stop fires Claude Code's
SubagentStop event. The harness payload carries no field that separates an
interim stop from a final one: SubagentStop input is ``session_id``,
``transcript_path``, ``cwd``, ``permission_mode``, ``hook_event_name``,
``stop_hook_active``, ``agent_id``, ``agent_type``, ``agent_transcript_path``,
``last_assistant_message``, ``background_tasks`` and ``session_crons``
(https://code.claude.com/docs/en/hooks#subagentstop-input) — and the last two
arrays are explicitly scoped to the PARENT session, not the subagent.

So VCO's answer is the honest one: the "worktree KEPT" notice is sent ONCE
per worktree path per session, and it says the agent may still be running and
will report after its final report. These tests pin that behaviour for both
siblings of ``subagent-stop-reconcile`` and the shipped coordinators' guidance
in the two CLAUDE.md templates.

POSIX-only for the hook tests (the hooks are bash); the ``.ps1`` arm runs
where pwsh exists, exactly as the shared ``FLAVOURS`` list decides.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_v02102_worktree_guard_no_head import (
    FLAVOURS,
    _LEAK_ENV_KEYS,
    _committed_repo,
    _launch,
    _record,
    _run,
    _stop,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _record_file(repo: Path, agent_id: str) -> Path:
    return repo / ".claude" / "state" / "worktree-guard" / f"wt-agent-{agent_id}.json"


def _as_if_resumed(repo: Path, agent_id: str) -> None:
    """Put the record back to ``created`` (what a resumed launch leaves), so
    the next stop re-runs the whole reap decision and would re-alert."""
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
    and that a lane keeps its own helpers in the foreground."""
    for name in ("CLAUDE.md.template", "ORCHESTRATOR-CLAUDE.md.template"):
        text = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "waiting on its own background work is INTERIM" in text, name
        assert "do not tell the user it finished or stalled" in text, name
        assert "runs its own helpers in the FOREGROUND" in text, name
        assert "reaches the root session" in text, name