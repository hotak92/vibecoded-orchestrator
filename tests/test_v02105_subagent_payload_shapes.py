# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.105 CL-2 — the subagent hooks against the payloads the harness really
sends, for background and fork-kind subagents.

Claude Code 2.1.302 made "fork" subagents a default kind and the VS Code
extension runs subagents in the BACKGROUND, so the question this file answers
is whether VCO's SubagentStart/SubagentStop hooks and the WorktreeCreate
guard still fire and attribute correctly (agent id, agent type, worktree).

Checked against live evidence on this machine (2026-10-29, Claude Code
2.1.312), which is what the fixtures below transcribe — with every private
path, session id and agent id replaced by a synthetic equivalent:

* ``WorktreeCreate`` — 294 logged payloads in a real project's
  ``.claude/logs/worktree-guard.jsonl`` carry EXACTLY six keys
  (``session_id``, ``transcript_path``, ``cwd``, ``prompt_id``,
  ``hook_event_name``, ``name``) with ``name`` = ``agent-<17 chars>``. No
  proposed-path field, no agent type, no isolation flag — the hook must
  derive the path itself, which it does.
* ``SubagentStop`` — 2271 rows in ``subagent-reconciliation.jsonl``; for the
  2025 of them whose harness sidecar still exists, the row's ``agent_type``
  equals the sidecar's ``agentType`` in 2025/2025 cases, 1236 of them
  ``requestShape: "background"``. Attribution therefore holds for background
  subagents; the hook's own ``agent_id`` is what the worktree records and the
  lane-reservation notice key on.
* 217 real rows carry an EMPTY ``agent_type`` (unnamed/fork-shaped launches):
  the row is still written, which is what the "no type" test below pins.
* 3361 ``agent-<id>`` ids on this machine are all ``[A-Za-z0-9-]``, so no id
  ever collapses into the shared ``default`` state bucket.
* ``model`` is NOT a hook-payload field: the harness records it in its own
  subagent sidecar (``"model": "opus"`` only when the dispatch named one), and
  no VCO hook reads or attributes a model. Nothing to pin there.

What this file adds is the SHAPE pin: the hooks keep working from the measured
payloads, tolerate fields a newer harness adds beside them, and stay graceful
when a launch carries no agent type at all.

POSIX-only (the hooks are bash); the ``.ps1`` siblings run where pwsh exists.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_v02102_worktree_guard_no_head import (
    _LEAK_ENV_KEYS,
    FLAVOURS,
    _agent_def,
    _committed_repo,
    _register_guard,
    _run,
)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


# ── the measured shapes, transcribed from live logs with values scrubbed ──

SESSION = "3f1c8a52-6d0e-4c1b-9a77-2b5d1e9c4f10"

WORKTREE_CREATE = {
    "session_id": SESSION,
    "transcript_path": f"/home/user/.claude/projects/-home-user-project/{SESSION}.jsonl",
    "cwd": "/home/user/project",
    "prompt_id": "8d2b6f40-1e37-4a95-b0c8-5f3ea71d24b9",
    "hook_event_name": "WorktreeCreate",
    "name": "agent-a9d2c8532ef719d0c",
}

SUBAGENT_STOP = {
    "session_id": SESSION,
    "transcript_path": f"/home/user/.claude/projects/-home-user-project/{SESSION}"
                       "/subagents/agent-a9d2c8532ef719d0c.jsonl",
    "cwd": "/home/user/project",
    "hook_event_name": "SubagentStop",
    "agent_id": "a9d2c8532ef719d0c",
    "agent_type": "deepseek-implementer",
}

SUBAGENT_START = {
    "session_id": SESSION,
    "transcript_path": f"/home/user/.claude/projects/-home-user-project/{SESSION}.jsonl",
    "cwd": "/home/user/project",
    "hook_event_name": "SubagentStart",
    "agent_id": "a9d2c8532ef719d0c",
    "agent_type": "deepseek-implementer",
}

#: Fields the harness keeps in its OWN sidecar for the same launch. A future
#: payload may grow some of them; none may change a hook's answer.
HARNESS_SIDECAR = {"requestShape": "background", "requestNonInteractive": True,
                   "spawnDepth": 1, "worktreePath": "/home/user/wt",
                   "spawnedWithWorktree": True}


def _journal(project: Path) -> list[dict]:
    log = project / ".claude" / "logs" / "subagent-reconciliation.jsonl"
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        # A torn line here is the failure this file exists to notice: the
        # hooks share one journal and subagents stop concurrently.
        rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# WorktreeCreate — the six measured keys, and fields a newer harness adds
# ---------------------------------------------------------------------------


def test_the_measured_worktree_create_shape_is_what_the_log_carries():
    """The pin itself: six keys, `name` = agent-<id>, no proposed path.

    RED if the harness (or this fixture) drifts into the shape the hook would
    have to validate instead of create."""
    assert set(WORKTREE_CREATE) == {
        "session_id", "transcript_path", "cwd", "prompt_id", "hook_event_name",
        "name"}
    assert re.fullmatch(r"agent-[A-Za-z0-9]+", WORKTREE_CREATE["name"])
    for absent in ("worktree_path", "path", "proposed_path", "agent_type",
                   "isolation"):
        assert absent not in WORKTREE_CREATE


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_guard_creates_the_worktree_from_the_measured_payload(
        flavour: str, tmp_path: Path):
    repo = _committed_repo(tmp_path / "proj")
    payload = {**WORKTREE_CREATE, "cwd": str(repo)}
    proc = _run("worktree-guard", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    created = Path(proc.stdout.strip().splitlines()[-1])
    assert created.is_dir(), proc.stdout
    assert created.name.startswith(WORKTREE_CREATE["name"])


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_extra_harness_fields_do_not_change_the_decision(
        flavour: str, tmp_path: Path):
    """A newer build adding sidecar-shaped fields beside the six must not
    change the create decision (2.1.302 added payload fields before)."""
    repo = _committed_repo(tmp_path / "proj")
    payload = {**WORKTREE_CREATE, **HARNESS_SIDECAR, "cwd": str(repo)}
    proc = _run("worktree-guard", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip().splitlines()[-1]).is_dir(), proc.stdout


# ---------------------------------------------------------------------------
# SubagentStop — attribution from the measured payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_stop_row_carries_the_measured_attribution(
        flavour: str, tmp_path: Path):
    repo = _committed_repo(tmp_path / "proj")
    proc = _run("subagent-stop-reconcile", flavour,
                {**SUBAGENT_STOP, "cwd": str(repo)}, repo)
    assert proc.returncode == 0, proc.stderr
    row, = _journal(repo)
    assert row["session_id"] == SESSION
    assert row["agent_id"] == SUBAGENT_STOP["agent_id"]
    assert row["agent_type"] == "deepseek-implementer"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_launch_with_no_agent_type_still_records(
        flavour: str, tmp_path: Path):
    """217 live rows carry an empty `agent_type` (unnamed/forks): the row is
    still written, with the empty value — never dropped, never a crash."""
    repo = _committed_repo(tmp_path / "proj")
    payload = {**SUBAGENT_STOP, "agent_type": "", "cwd": str(repo)}
    proc = _run("subagent-stop-reconcile", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    row, = _journal(repo)
    assert row["agent_id"] == SUBAGENT_STOP["agent_id"]
    assert row["agent_type"] == ""


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_stop_hook_ignores_sidecar_only_fields(
        flavour: str, tmp_path: Path):
    repo = _committed_repo(tmp_path / "proj")
    payload = {**SUBAGENT_STOP, **HARNESS_SIDECAR, "cwd": str(repo)}
    proc = _run("subagent-stop-reconcile", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    row, = _journal(repo)
    assert row["agent_type"] == "deepseek-implementer"


# ---------------------------------------------------------------------------
# SubagentStart — the two fields the hooks attribute by
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_the_start_hook_attributes_a_skipped_isolation_by_id_and_type(
        flavour: str, tmp_path: Path):
    """The measured payload carries `agent_id` + `agent_type`; those two are
    exactly what decides a skipped isolation (no worktree record + a
    definition declaring `isolation: worktree`)."""
    repo = _committed_repo(tmp_path / "proj")
    _agent_def(repo, "deepseek-implementer", "worktree")
    _register_guard(repo)
    proc = _run("subagent-start-isolation-check", flavour,
                {**SUBAGENT_START, "cwd": str(repo)}, repo)
    assert proc.returncode == 0, proc.stderr
    assert "ISOLATION SKIPPED" in proc.stdout, proc.stdout
    assert "deepseek-implementer" in proc.stdout


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("agent_type", ["", "fork", "some-plugin:agent"])
def test_the_start_hook_stays_silent_when_it_cannot_decide(
        flavour: str, agent_type: str, tmp_path: Path):
    """LEAVE-ALONE: a launch whose type is empty, a fork-kind name with no
    definition, or a plugin-scoped type is not guessable — the hook exits 0
    and says nothing (never warns an agent into a violation)."""
    repo = _committed_repo(tmp_path / "proj")
    _register_guard(repo)
    payload = {**SUBAGENT_START, "agent_type": agent_type, "cwd": str(repo)}
    proc = _run("subagent-start-isolation-check", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_a_launched_agent_with_a_worktree_record_is_not_warned(
        flavour: str, tmp_path: Path):
    """The background-isolated case measured live: WorktreeCreate ran first,
    wrote `wt-agent-<id>.json`, so the start hook finds the record and stays
    silent whatever the payload's type says."""
    repo = _committed_repo(tmp_path / "proj")
    _agent_def(repo, "deepseek-implementer", "worktree")
    _register_guard(repo)
    created = _run("worktree-guard", flavour,
                   {**WORKTREE_CREATE, "cwd": str(repo)}, repo)
    assert created.returncode == 0, created.stderr
    payload = {**SUBAGENT_START, "cwd": str(repo)}
    proc = _run("subagent-start-isolation-check", flavour, payload, repo)
    assert proc.returncode == 0, proc.stderr
    assert "ISOLATION SKIPPED" not in proc.stdout, proc.stdout