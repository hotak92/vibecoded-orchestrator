# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.102 W6-T (ship-gate finding #2) — worktree-guard's per-agent records
survive as long as their agent can still be resumed.

``_lib/worktree-records`` says "a record outlives its worktree on purpose: a
resumed subagent's SubagentStart must still find it", but the prune deleted
EVERY ``wt-*.json`` older than 30 days by mtime. An agent resumed after that
had no record, so ``wtrec_isolation_skipped`` / ``Test-WtrecIsolationSkipped``
read it as a SKIPPED isolation and subagent-stop-reconcile's Layer 3b raised a
false ``worktree_violation`` for an agent the guard DID isolate.

The rule now, pinned for BOTH the .sh and the .ps1 library:

  * a lookup (``wtrec_find`` / ``Find-WtrecRecord``) bumps the record's mtime,
    so the idle clock starts at the agent's last activity, not its launch;
  * a record of ANY status is pruned only once idle longer than
    ``max(30, cleanupPeriodDays) + 30`` days — ``cleanupPeriodDays`` being
    Claude Code's transcript retention, read (that key only) from the user
    settings and the project's ``settings.json`` + ``settings.local.json``,
    the largest valid value winning, invalid values ignored;
  * notices keep their 30-day prune;
  * with no python (.sh) no record is pruned at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.common.pwsh import PWSH_BASE_FLAGS
from tests.test_v02102_w6r_isolation_alert import _start_snapshot_modify_stop, _violations
from tests.test_v02102_worktree_guard_no_head import (
    _LEAK_ENV_KEYS,
    _PS,
    FLAVOURS,
    _agent_def,
    _committed_repo,
    _launch,
    _register_guard,
    _run,
)

_HUGE_INT = object()  # sentinel: a JSON integer too large for a double
LIB = Path(__file__).resolve().parents[1] / "templates" / "hooks" / "_lib"
DAY = 86400.0


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in _LEAK_ENV_KEYS:  # CLAUDE_CONFIG_DIR included
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _fakehome(project: Path) -> Path:
    # The same per-test HOME that _run (tests/test_v02102_worktree_guard_no_head)
    # hands every hook.
    return project.parent / "fakehome"


def _rec_dir(project: Path) -> Path:
    d = project / ".claude" / "state" / "worktree-guard"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _age(path: Path, days: float) -> None:
    t = time.time() - days * DAY
    os.utime(path, (t, t))


def _put_record(project: Path, name: str, idle_days: float, *, status: str = "created",
                mode: str = "head", created_days: float | None = None) -> Path:
    created = time.gmtime(time.time() - (created_days if created_days is not None else idle_days) * DAY)
    rec = {"name": name, "mode": mode, "path": str(project / ".claude" / "worktrees" / name),
           "base": "0" * 40, "toplevel": str(project), "status": status,
           "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", created)}
    p = _rec_dir(project) / f"wt-{name}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    _age(p, idle_days)
    return p


def _put_notice(project: Path, name: str, idle_days: float) -> Path:
    p = _rec_dir(project) / f"notice-{name}.txt"
    p.write_text("notice\n", encoding="utf-8")
    _age(p, idle_days)
    return p


def _settings(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A neighbouring key that must never matter (nor be printed).
    path.write_text(json.dumps({"cleanupPeriodDays": value, "env": {"OTHER": "x"}}), encoding="utf-8")


def _prune(flavour: str, project: Path, *, no_python: bool = False,
           config_dir: Path | None = None) -> subprocess.CompletedProcess:
    """Call wtrec_prune / Remove-WtrecStale directly, the way worktree-guard does."""
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env["HOME"] = str(_fakehome(project))
    env["WT_LIB"] = str(LIB / f"worktree-records.{flavour}")
    env["WT_ROOT"] = str(project)
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    if flavour == "sh":
        resolve = 'PY=""' if no_python else 'PY="$(command -v python3 || command -v python)"'
        argv = ["bash", "-c", f'. "$WT_LIB"; {resolve}; wtrec_prune "$WT_ROOT"']
    else:
        assert _PS is not None
        argv = [_PS, *PWSH_BASE_FLAGS, "-Command", ". $env:WT_LIB; Remove-WtrecStale $env:WT_ROOT"]
    proc = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=str(project), timeout=90)
    assert proc.returncode == 0, proc.stderr
    assert "OTHER" not in proc.stdout + proc.stderr
    return proc


def _find(flavour: str, project: Path, agent_id: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _LEAK_ENV_KEYS}
    env.update(WT_LIB=str(LIB / f"worktree-records.{flavour}"), WT_ROOT=str(project), WT_ID=agent_id)
    if flavour == "sh":
        argv = ["bash", "-c", '. "$WT_LIB"; wtrec_find "$WT_ROOT" "$WT_ID"']
    else:
        assert _PS is not None
        argv = [_PS, *PWSH_BASE_FLAGS, "-Command", ". $env:WT_LIB; Find-WtrecRecord $env:WT_ROOT $env:WT_ID"]
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=90).stdout.strip()


def _project(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


# ── the idle window: max(30, cleanupPeriodDays) + 30 = 60 days by default ───


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("status,mode", [("created", "head"), ("removed", "head"), ("kept", "snapshot"),
                                         ("created", "in_place"), ("reaped", "head")])
def test_record_idle_past_window_is_pruned_whatever_status(flavour: str, status: str, mode: str,
                                                            tmp_path: Path) -> None:
    proj = _project(tmp_path)
    stale = _put_record(proj, "agent-old", 61, status=status, mode=mode)
    fresh = _put_record(proj, "agent-new", 0, status=status, mode=mode)
    _prune(flavour, proj)
    assert not stale.exists(), f"a {status}/{mode} record idle 61 days is past the 60-day window"
    assert fresh.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_record_inside_window_is_kept(flavour: str, tmp_path: Path) -> None:
    """45 idle days: the old 30-day prune deleted it; a resume is still possible."""
    proj = _project(tmp_path)
    rec = _put_record(proj, "agent-mid", 45)
    _prune(flavour, proj)
    assert rec.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_notices_keep_the_30_day_prune(flavour: str, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    old = _put_notice(proj, "agent-a", 31)
    new = _put_notice(proj, "agent-b", 29)
    _prune(flavour, proj)
    assert not old.exists() and new.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_old_created_at_with_recent_mtime_is_kept(flavour: str, tmp_path: Path) -> None:
    """The window counts from the last activity (mtime), not from the launch."""
    proj = _project(tmp_path)
    rec = _put_record(proj, "agent-busy", 0, created_days=45)
    _settings(proj / ".claude" / "settings.local.json", 0)  # min window (60) still keeps it
    _prune(flavour, proj)
    assert rec.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_lookup_restarts_the_idle_clock(flavour: str, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    rec = _put_record(proj, "agent-abc123", 59)
    assert _find(flavour, proj, "abc123") == str(rec)
    assert time.time() - rec.stat().st_mtime < 120, "wtrec_find must bump the record's mtime"
    _age(rec, 59)
    # A missing record is not created by the lookup.
    assert _find(flavour, proj, "nope99") == ""
    assert not (_rec_dir(proj) / "wt-agent-nope99.json").exists()


# ── cleanupPeriodDays raises the window ─────────────────────────────────────


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("where", ["user-home", "user-config-dir", "project", "project-local"])
def test_raised_cleanup_period_is_honoured(flavour: str, where: str, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    config_dir = None
    if where == "user-home":
        _settings(_fakehome(proj) / ".claude" / "settings.json", 100)
    elif where == "user-config-dir":
        config_dir = tmp_path / "cfgdir"
        _settings(config_dir / "settings.json", 100)
        # CLAUDE_CONFIG_DIR replaces ~/.claude: a ~/.claude value is not read.
        _settings(_fakehome(proj) / ".claude" / "settings.json", 1000)
    elif where == "project":
        _settings(proj / ".claude" / "settings.json", 100)
    else:
        _settings(proj / ".claude" / "settings.local.json", 100)
    kept = _put_record(proj, "agent-k", 120)    # window = 100 + 30 = 130
    gone = _put_record(proj, "agent-g", 140)
    _prune(flavour, proj, config_dir=config_dir)
    assert kept.exists(), f"cleanupPeriodDays=100 in {where} must keep a 120-day idle record"
    assert not gone.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_largest_valid_cleanup_period_wins(flavour: str, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    _settings(_fakehome(proj) / ".claude" / "settings.json", 200)
    _settings(proj / ".claude" / "settings.local.json", 10)
    rec = _put_record(proj, "agent-x", 200)
    _prune(flavour, proj)
    assert rec.exists()


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("value", [True, -500, "500", None, [500], {"days": 500}, _HUGE_INT],
                         ids=["bool", "negative", "string", "null", "list", "object", "beyond-double"])
def test_invalid_cleanup_period_is_ignored(flavour: str, value: object, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    if value is _HUGE_INT:
        path = proj / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"cleanupPeriodDays": ' + "9" * 400 + "}", encoding="utf-8")
    else:
        _settings(proj / ".claude" / "settings.json", value)
    rec = _put_record(proj, "agent-y", 70)
    _prune(flavour, proj)
    # A number beyond a double is invalid too (PowerShell cannot even parse it).
    assert not rec.exists(), "an invalid cleanupPeriodDays must be ignored (window stays 60)"


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_unreadable_settings_fall_back_to_the_default(flavour: str, tmp_path: Path) -> None:
    proj = _project(tmp_path)
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    (proj / ".claude" / "settings.local.json").write_text("[1, 2]", encoding="utf-8")
    keep = _put_record(proj, "agent-k", 50)
    gone = _put_record(proj, "agent-g", 70)
    _prune(flavour, proj)
    assert keep.exists() and not gone.exists()


def test_no_python_prunes_no_record(tmp_path: Path) -> None:
    """.sh only: without python the window cannot be computed → keep records."""
    proj = _project(tmp_path)
    rec = _put_record(proj, "agent-z", 400)
    notice = _put_notice(proj, "agent-z", 31)
    _prune("sh", proj, no_python=True)
    assert rec.exists()
    assert not notice.exists(), "notices keep their 30-day find-based prune"


# ── end to end: a resumed agent is not alerted after another agent's launch ─


@pytest.mark.parametrize("flavour", FLAVOURS)
def test_resumed_agent_keeps_its_record_across_another_launch(flavour: str, tmp_path: Path) -> None:
    repo = _committed_repo(tmp_path / "proj")
    _register_guard(repo)
    _agent_def(repo, "my-coder", "worktree")
    _launch(flavour, repo, "res123")
    rec = repo / ".claude" / "state" / "worktree-guard" / "wt-agent-res123.json"
    assert rec.is_file()
    # The agent finished 45 days ago (worktree reaped, record kept) ...
    data = json.loads(rec.read_text(encoding="utf-8-sig"))
    data["status"] = "removed"
    data["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 45 * DAY))
    rec.write_text(json.dumps(data), encoding="utf-8")
    _age(rec, 45)

    # ... and is resumed now: its SubagentStart finds the record (no warning),
    start = _run("subagent-start-isolation-check", flavour,
                 {"hook_event_name": "SubagentStart", "cwd": str(repo),
                  "agent_id": "res123", "agent_type": "my-coder"}, repo)
    assert start.returncode == 0, start.stderr
    assert "ISOLATION SKIPPED" not in start.stdout
    # another agent launches meanwhile (worktree-guard prunes),
    _launch(flavour, repo, "other1")
    assert rec.is_file(), "the resumed agent's record must survive another launch's prune"
    # and the resumed agent's stop raises no false worktree violation.
    stop = _start_snapshot_modify_stop(flavour, repo, "res123", "my-coder")
    assert stop.returncode == 0, stop.stderr
    assert _violations(repo) == []
    assert "WORKTREE VIOLATION" not in stop.stderr
