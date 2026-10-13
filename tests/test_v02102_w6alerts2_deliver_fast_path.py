# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-ALERTS2 (owner ruling on the Fable re-check pwsh cost note) —
the agent-alerts-deliver no-pending path on PowerShell.

``agent-alerts-deliver.ps1`` runs on EVERY main-chat tool call (PostToolUse
matcher ``*``). Its no-pending path cost ~88 ms on top of the bare pwsh start
(module autoload for Get-ChildItem / Test-Path / Join-Path, the env scrub,
the stdin read, and parsing the whole pending path). It now checks with one
.NET listing before any cmdlet, and the pending path lives in
``_lib/agent-alerts-deliver-pending.ps1`` (parsed only when something is
pending).

* The pending path still delivers through the split (both siblings), and
  still sees a claim (R5-1) through the single ``agent_alerts_*.jsonl*``
  pattern.
* A missing pending lib is a broken install: said to the model, not skipped.
* A coarse, relative cost guard: the no-pending path stays close to the bare
  interpreter start (min-of-N, generous bound, ``VCT_SKIP_LATENCY_GUARD=1``
  escape hatch). Before this lane it was ~85 ms over; the bound is 55 ms.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from tests.test_v02102_w6c2_subagent_hook_census import (
    FLAVOURS,
    HOOKS,
    _dead_pid,
    _env,
    _hooks_copy_without,
    _need,
    _project,
    _run_hook,
    _venv_env,
)

MAIN_CHAT = {"hook_event_name": "PostToolUse", "tool_name": "Grep", "session_id": "s1",
             "tool_input": {"pattern": "x"}}


def _aa():
    from vco_lib import agent_alerts

    return agent_alerts


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_pending_alert_is_delivered_through_the_split(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    project = _project(tmp_path)
    assert _aa().append_alert(str(project), "s1", "lane1", "coder", "credential", "split ok")
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT, project, env=_venv_env(project))
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PostToolUse" and "split ok" in out["additionalContext"]
    assert not list((project / ".claude" / "state").glob("agent_alerts_*"))


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_claim_alone_is_pending_under_the_single_pattern(tmp_path: Path, flavour: str) -> None:
    """R5-1 under the merged glob: a dead-pid claim with no alert file still
    takes the pending path (and is re-adopted and delivered)."""
    _need(flavour)
    project = _project(tmp_path)
    p = _aa().append_alert(str(project), "s1", "lane1", "coder", "credential", "claim only")
    assert p
    p.replace(p.with_name(f"{p.name}.delivering.{_dead_pid()}"))
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT, project, env=_venv_env(project))
    assert r.returncode == 0, r.stderr
    assert "claim only" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("decoy", ["agent_alerts.lock", "agent_alerts_x.json", "other.jsonl"])
def test_non_alert_files_are_not_pending(tmp_path: Path, flavour: str, decoy: str) -> None:
    """Leave-alone: the lock and look-alike names never start the pending path
    (a bogus venv would otherwise produce the broken-install notice)."""
    _need(flavour)
    project = _project(tmp_path)
    (project / ".claude" / "state" / decoy).write_text("x\n", encoding="utf-8")
    env = _env(project, VCT_VENV=str(tmp_path / "no-venv"),
               VCT_INSTALL_ROOT=str(tmp_path / "no-root"), VCO_FIND_PYTHON_PATH_ONLY="1")
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT, project, env=env)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_missing_state_dir_is_not_pending(tmp_path: Path, flavour: str) -> None:
    _need(flavour)
    project = tmp_path / "bare"
    project.mkdir()
    r = _run_hook("agent-alerts-deliver", flavour, MAIN_CHAT, project, env=_env(project))
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_ps1_missing_pending_lib_is_loud(tmp_path: Path) -> None:
    """A pending alert with the pending lib gone (a half-applied bundle) is
    said to the model and on stderr — never a silent exit."""
    _need("ps1")
    project = _project(tmp_path)
    assert _aa().append_alert(str(project), "s1", "lane1", "coder", "credential", "x")
    hooks = _hooks_copy_without(tmp_path, ["agent-alerts-deliver-pending.ps1"])
    for payload, event in ((MAIN_CHAT, "PostToolUse"),
                           ({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                             "prompt": "hi"}, "UserPromptSubmit")):
        r = _run_hook("agent-alerts-deliver", "ps1", payload, project,
                      env=_venv_env(project), hooks_dir=hooks)
        assert r.returncode == 0
        out = json.loads(r.stdout)["hookSpecificOutput"]
        assert out["hookEventName"] == event
        assert "agent-alerts-deliver-pending.ps1 is missing" in out["additionalContext"]
        assert "agent-alerts-deliver-pending.ps1 is missing" in r.stderr
    assert list((project / ".claude" / "state").glob("agent_alerts_*")), "nothing consumed"


def _min_ms(argv: list, env: dict, n: int) -> float:
    best = float("inf")
    for i in range(n + 1):
        t0 = time.perf_counter()
        r = subprocess.run(argv, input=json.dumps(MAIN_CHAT), capture_output=True, text=True,
                           env=env, timeout=60)
        elapsed = (time.perf_counter() - t0) * 1000
        assert r.returncode == 0 and r.stdout == "", (argv, r.stdout, r.stderr)
        if i:  # the first run is a warmup
            best = min(best, elapsed)
    return best


def ps1_no_pending_overhead_ms(hook: Path, project: Path, n: int = 7) -> tuple:
    """(hook min ms, bare pwsh min ms) for the no-pending path. Shared with
    the lane's red-proof, which points ``hook`` at the pre-change file."""
    empty = project / "empty.ps1"
    empty.write_text("", encoding="utf-8")
    env = _env(project)
    base = ["pwsh", *PWSH_BASE_FLAGS, "-ExecutionPolicy", "Bypass", "-File"]
    bare = _min_ms(base + [str(empty)], env, n)
    hook_ms = _min_ms(base + [str(hook)], env, n)
    return hook_ms, bare


def test_ps1_no_pending_path_stays_near_the_bare_interpreter_start(tmp_path: Path) -> None:
    _need("ps1")
    project = _project(tmp_path)
    hook_ms, bare = ps1_no_pending_overhead_ms(HOOKS / "agent-alerts-deliver.ps1", project)
    print(f"agent-alerts-deliver.ps1 no-pending: {hook_ms:.0f} ms (bare pwsh {bare:.0f} ms)")
    if os.environ.get("VCT_SKIP_LATENCY_GUARD") == "1":
        pytest.skip("VCT_SKIP_LATENCY_GUARD=1")
    bound = float(os.environ.get("VCT_ALERTS_PS1_OVERHEAD_BOUND_MS", "55"))
    assert hook_ms - bare < bound, (
        f"the no-pending path costs {hook_ms - bare:.0f} ms over the bare pwsh start "
        f"(bound {bound:.0f}): a cmdlet or a big parse came back before the pending check")
