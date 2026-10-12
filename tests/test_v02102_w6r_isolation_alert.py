# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-R item 1 — subagent-stop-reconcile's Layer 3b alert can fire.

Layer 3b (the post-hoc worktree-violation alert) took its isolation flag
only from payload fields (``isolation``, ``worktree``, ...) that the live
SubagentStop payload never carries, so the alert was dead code and its
header comment promised a guard that did not exist. The flag is now derived
the way subagent-start-isolation-check does it, through ONE shared function
(``_lib/worktree-records`` :: ``wtrec_isolation_skipped`` /
``Test-WtrecIsolationSkipped``):

  * worktree-guard record with mode head|snapshot → isolated → silent;
  * no record, guard registered, definition declares ``isolation: worktree``
    → skipped → ``worktree_violation`` row + stderr line;
  * guard parked / definition not isolated / plugin type → silent.

The mode is read before Step 1b's reap rewrites the record. Pinned for both
the .sh and the .ps1 hook.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_v02102_worktree_guard_no_head import (
    _LEAK_ENV_KEYS,
    FLAVOURS,
    _agent_def,
    _committed_repo,
    _launch,
    _register_guard,
    _run,
)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _violations(project: Path) -> list[dict]:
    log = project / ".claude" / "logs" / "worktree-guard.jsonl"
    if not log.exists():
        return []
    rows = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("decision") == "worktree_violation"]


def _start_snapshot_modify_stop(flavour: str, repo: Path, agent_id: str, agent_type: str,
                                extra_stop: dict | None = None):
    # SubagentStart takes the SHA snapshot the stop hook diffs against; it
    # walks fixed source dirs (src/, docs/, ...), not the repo root.
    src = repo / "src" / "main.py"
    src.parent.mkdir(exist_ok=True)
    src.write_text("def foo():\n    return 1\n", encoding="utf-8")
    start = _run("subagent-start-kg-inject", flavour,
                 {"hook_event_name": "SubagentStart", "prompt": "do work", "session_id": "s1",
                  "agent_id": agent_id, "agent_type": agent_type, "cwd": str(repo)}, repo)
    assert start.returncode == 0, start.stderr
    src.write_text("def foo():\n    return 2  # edited in the parent checkout\n", encoding="utf-8")
    payload = {"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": agent_id,
               "agent_type": agent_type, "cwd": str(repo), **(extra_stop or {})}
    return _run("subagent-stop-reconcile", flavour, payload, repo)


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_subagent_start_snapshot_records_files(flavour: str, tmp_path: Path) -> None:
    # The .ps1 snapshot probed `[System.IO.Path]::GetType()` (a static call
    # that throws), so every file was skipped and the .ps1 reconciler —
    # Layer 3b included — never saw a change.
    repo = _committed_repo(tmp_path / "proj")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("X = 1\n", encoding="utf-8")
    proc = _run("subagent-start-kg-inject", flavour,
                {"hook_event_name": "SubagentStart", "session_id": "s1", "agent_id": "snap1",
                 "agent_type": "x", "cwd": str(repo)}, repo)
    assert proc.returncode == 0, proc.stderr
    snap = repo / ".claude" / "state" / "subagent-snapshot-snap1.json"
    files = json.loads(snap.read_text(encoding="utf-8-sig"))["files"]
    assert list(files) == ["src/main.py"]


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_skipped_isolation_is_alerted_at_stop(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    _register_guard(repo)
    _agent_def(repo, "my-coder", "worktree")
    # A live SubagentStop payload: no isolation field at all.
    proc = _start_snapshot_modify_stop(flavour, repo, "abc123", "my-coder")
    assert proc.returncode == 0, proc.stderr
    rows = _violations(repo)
    assert len(rows) == 1, rows
    assert rows[0]["agent_id"] == "abc123" and rows[0]["agent_type"] == "my-coder"
    assert "src/main.py" in rows[0]["changed_files"]
    assert "WORKTREE VIOLATION" in proc.stderr


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("case", ["guard-made-worktree", "guard-made-worktree-payload-flag",
                                  "guard-not-registered", "definition-not-isolated",
                                  "plugin-type"])
def test_stop_alert_leave_alone(flavour: str, case: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    agent_type = "my-coder"
    extra: dict = {}
    if case != "guard-not-registered":
        _register_guard(repo)
    _agent_def(repo, "my-coder", None if case == "definition-not-isolated" else "worktree")
    if case.startswith("guard-made-worktree"):
        # The guard gave the agent its own worktree (record mode=head); the
        # parent-checkout edit came from elsewhere. The record is reaped
        # (status rewritten) in Step 1b — the mode must have been read first.
        _launch(flavour, repo, "abc123")
    if case == "guard-made-worktree-payload-flag":
        extra = {"isolation": "worktree"}
    if case == "plugin-type":
        agent_type = "some-plugin:my-coder"
    proc = _start_snapshot_modify_stop(flavour, repo, "abc123", agent_type, extra)
    assert proc.returncode == 0, proc.stderr
    assert _violations(repo) == []
    assert "WORKTREE VIOLATION" not in proc.stderr
