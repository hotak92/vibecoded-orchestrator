# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.71 Track T-WT — SubagentStop Layer-3b worktree-violation alert.

The reconciler (`subagent-stop-reconcile.sh`) gains a post-hoc detector: when
a subagent that requested `isolation: worktree` modified files that the
snapshot diff found under the PARENT checkout (and the reported cwd was the
parent toplevel, or absent), the isolation silently fell back to the shared
tree — the 2026-06-30 incident. The reconciler writes a `worktree_violation`
row to `.claude/logs/worktree-guard.jsonl` (the same JSONL the WorktreeCreate
guard uses) so the integrator has one diagnosis trail.

Decision matrix:
  * isolation + parent-root writes + cwd==parent  → worktree_violation row
  * isolation + parent-root writes + cwd absent   → worktree_violation row
  * isolation + parent-root writes + cwd is a
    SEPARATE worktree                              → NO violation (leave-alone)
  * NO isolation flag + parent-root writes         → NO violation (leave-alone)
  * isolation + NO file changes                    → NO violation (leave-alone)

This mirrors the existing reconciler-test snapshot mechanism (SubagentStart
takes a SHA snapshot; SubagentStop diffs it).

POSIX-only; the .ps1 sibling carries the same logic (hook-OS-parity gate).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "templates" / "hooks"
SUBAGENT_START = HOOKS_DIR / "subagent-start-kg-inject.sh"
SUBAGENT_STOP = HOOKS_DIR / "subagent-stop-reconcile.sh"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash hooks are POSIX-only; .ps1 sibling covered by hook-OS-parity gate.",
)


def _setup_project(tmp_path: Path) -> Path:
    (tmp_path / ".claude" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude" / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "main.py").write_text(
        "# seed\ndef foo():\n    return 1\n", encoding="utf-8"
    )
    return tmp_path


def _run_hook(
    hook_path: Path,
    payload: dict,
    project_root: Path,
    home_override: Path,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(project_root)
    env.pop("VCT_DISABLE_HOOKS", None)
    # Pin nudge-counter writes into the test sandbox.
    env["HOME"] = str(home_override)
    return subprocess.run(
        ["bash", str(hook_path)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _violation_rows(project_root: Path) -> list[dict]:
    log = project_root / ".claude" / "logs" / "worktree-guard.jsonl"
    if not log.exists():
        return []
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("decision") == "worktree_violation":
            rows.append(row)
    return rows


def _snapshot_then_modify_then_stop(
    tmp_path: Path,
    stop_payload: dict,
    agent_id: str = "wtv-agent",
    session_id: str = "wtv-session",
) -> subprocess.CompletedProcess:
    """Take a snapshot via SubagentStart, modify a parent-root file, then run
    SubagentStop with the given payload. Returns the stop CompletedProcess."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    start_payload = {
        "prompt": "do work",
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": "@agent-coder",
    }
    start = _run_hook(SUBAGENT_START, start_payload, tmp_path, home)
    assert start.returncode == 0, start.stderr
    # Modify a file under the parent root → the diff will report it.
    (tmp_path / "src" / "main.py").write_text(
        "# modified by agent\ndef foo():\n    return 2\n", encoding="utf-8"
    )
    payload = {"session_id": session_id, "agent_id": agent_id, **stop_payload}
    return _run_hook(SUBAGENT_STOP, payload, tmp_path, home)


def test_violation_when_isolation_and_cwd_is_parent(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    stop = _snapshot_then_modify_then_stop(
        tmp_path,
        {"isolation": "worktree", "cwd": str(tmp_path)},
    )
    assert stop.returncode == 0, stop.stderr
    rows = _violation_rows(tmp_path)
    assert len(rows) == 1, f"expected 1 violation row, got {rows}"
    assert rows[0]["agent_id"] == "wtv-agent"
    assert rows[0]["file_count"] >= 1
    assert "src/main.py" in rows[0]["changed_files"]


def test_violation_when_isolation_and_cwd_absent(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    stop = _snapshot_then_modify_then_stop(
        tmp_path,
        {"isolation": "worktree"},  # no cwd field
    )
    assert stop.returncode == 0, stop.stderr
    # cwd absent + isolation + parent-root writes → still a violation.
    assert len(_violation_rows(tmp_path)) == 1


def test_no_violation_when_cwd_is_separate_worktree(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    separate = str(tmp_path / "worktrees" / "agent-x")
    stop = _snapshot_then_modify_then_stop(
        tmp_path,
        {"isolation": "worktree", "cwd": separate},
    )
    assert stop.returncode == 0, stop.stderr
    # A real separate worktree cwd → the parent-root writes came from
    # elsewhere; do NOT false-alarm.
    assert _violation_rows(tmp_path) == []


def test_no_violation_when_isolation_not_requested(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    stop = _snapshot_then_modify_then_stop(
        tmp_path,
        {"cwd": str(tmp_path)},  # no isolation flag
    )
    assert stop.returncode == 0, stop.stderr
    # Non-isolation agents are never flagged (we don't guess violations).
    assert _violation_rows(tmp_path) == []


def test_no_violation_when_no_file_changes(tmp_path: Path) -> None:
    _setup_project(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    agent_id = "wtv-nochange"
    start = _run_hook(
        SUBAGENT_START,
        {"prompt": "x", "session_id": "s", "agent_id": agent_id, "agent_type": "@agent-coder"},
        tmp_path,
        home,
    )
    assert start.returncode == 0
    # No modification between snapshot and stop.
    stop = _run_hook(
        SUBAGENT_STOP,
        {"session_id": "s", "agent_id": agent_id, "isolation": "worktree", "cwd": str(tmp_path)},
        tmp_path,
        home,
    )
    assert stop.returncode == 0, stop.stderr
    assert _violation_rows(tmp_path) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
