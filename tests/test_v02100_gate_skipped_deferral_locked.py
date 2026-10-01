# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-17 — the hooks' ``gate_skipped_no_project_id`` entry goes
through the ONE locked deferral writer.

Before v0.2.100 ``templates/hooks/_lib/route-touched-path.{sh,ps1}`` wrote the
entry with a bare append (``>>`` / ``Add-Content``): no lock, a
grep-then-append race, a raw markdown block. Every other writer of
``UPDATE_DEFERRED.md`` reads, merges and rewrites it under
``.claude/context/.update-deferred.lock``, so the hook's append could be lost
to, or clobber, a concurrent ``install.py`` finalize. The hook now calls
``python -m vco_lib.gate_skipped_deferral``, which writes through
:func:`vco_lib.deferral_emit.emit`. Pinned here, driving the real shell
functions: the write WAITS for a held lock; a foreign entry survives; a
missing VCO venv is said on stderr and nothing is written unlocked.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from vco_lib.atomic import exclusive_file_lock
from vco_lib.deferral_emit import LOCK_REL, emit
from vco_lib.deferral_report import DeferralEntry, DeferralReport
from vco_lib.gate_skipped_deferral import CONDITION_ID, build_entry

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "templates" / "hooks"
PWSH = shutil.which("pwsh")
_SHELLS = ["sh", pytest.param("ps1", marks=pytest.mark.skipif(PWSH is None, reason="pwsh not installed"))]


def _world(tmp_path: Path) -> tuple[Path, Path]:
    hooks = tmp_path / "hooks"
    shutil.copytree(HOOKS, hooks)  # outside any clone: no tier-4 venv fallback
    proj = tmp_path / "proj"
    (proj / ".claude" / "context").mkdir(parents=True)
    return hooks, proj


def _env(tmp_path: Path, *, venv: str | None) -> dict:
    env = dict(child_env())
    for key in ("VCT_VENV", "VCT_INSTALL_ROOT", "VCT_DISABLE_HOOKS", "CLAUDE_SESSION_ID"):
        env.pop(key, None)
    env.update(HOME=str(tmp_path / "home"), VCT_STATE_DIR=str(tmp_path / "state"),
               VCT_SESSION_ID="sess-1", PYTHONPATH=str(REPO))
    env["VCT_VENV"] = venv if venv else str(tmp_path / "no-such-venv")
    return env


def _argv(impl: str, hooks: Path, proj: Path) -> list:
    if impl == "sh":
        script = (
            '. "$1/_lib/route-touched-path.sh"; '
            '_VCO_ROUTE_HOOKS_DIR="$1"; _VCO_ROUTE_PROJECT_ROOT="$2"; '
            '_kg_emit_gate_skipped_deferral Demo_KnowledgeGraph'
        )
        return ["bash", "-c", script, "_", str(hooks), str(proj)]
    driver = hooks.parent / "drive.ps1"
    driver.write_text(
        "param([string]$Hooks, [string]$Proj)\n"
        ". (Join-Path $Hooks '_lib/route-touched-path.ps1')\n"
        "$script:VcoRouteHooksDir = $Hooks\n"
        "$script:VcoRouteProjectRoot = $Proj\n"
        "Emit-KgGateSkippedDeferral -Collection 'Demo_KnowledgeGraph'\n",
        encoding="utf-8")
    return [PWSH, "-NoProfile", "-NonInteractive", "-File", str(driver),
            "-Hooks", str(hooks), "-Proj", str(proj)]


def _entry(proj: Path):
    return DeferralReport.read(proj).entry_for(CONDITION_ID)


@pytest.mark.parametrize("impl", _SHELLS)
def test_hook_entry_lands_through_the_locked_writer(impl, tmp_path):
    hooks, proj = _world(tmp_path)
    # A foreign entry another writer already recorded must survive the hook.
    emit(proj, DeferralEntry(condition_id="some_other_condition", title="t",
                             detected="d", why_deferred="w", command_to_apply="c"))
    res = subprocess.run(_argv(impl, hooks, proj), env=_env(tmp_path, venv=sys.executable),
                         capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, res.stderr
    entry = _entry(proj)
    assert entry is not None, res.stderr
    assert "hook env" in entry.title
    assert "Demo_KnowledgeGraph" in entry.detected
    assert DeferralReport.read(proj).entry_for("some_other_condition") is not None
    # Exactly one section for the condition — an upsert, not an append.
    text = (proj / ".claude" / "context" / "UPDATE_DEFERRED.md").read_text(encoding="utf-8")
    assert text.count(f"## {CONDITION_ID}") == 1


@pytest.mark.parametrize("impl", _SHELLS)
def test_hook_write_waits_for_a_held_lock(impl, tmp_path):
    hooks, proj = _world(tmp_path)
    ledger = proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
    with exclusive_file_lock(proj / LOCK_REL):
        proc = subprocess.Popen(_argv(impl, hooks, proj), env=_env(tmp_path, venv=sys.executable),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(4)
        # An unlocked append would already be on disk.
        assert not ledger.exists() or CONDITION_ID not in ledger.read_text(encoding="utf-8")
        assert proc.poll() is None, "the writer finished while the lock was held"
    _out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err
    assert _entry(proj) is not None


@pytest.mark.parametrize("impl", _SHELLS)
def test_no_venv_is_loud_and_writes_nothing(impl, tmp_path):
    hooks, proj = _world(tmp_path)
    res = subprocess.run(_argv(impl, hooks, proj), env=_env(tmp_path, venv=None),
                         capture_output=True, text=True, timeout=180)
    assert res.returncode == 0
    assert "NOT recorded" in res.stderr
    assert not (proj / ".claude" / "context" / "UPDATE_DEFERRED.md").exists()
    # The sentinel is released, so the next write retries.
    assert not (proj / ".claude" / "state" / "gate_skipped_deferral_sess-1").exists()


@pytest.mark.parametrize("impl", _SHELLS)
def test_second_call_in_a_session_is_a_noop(impl, tmp_path):
    """Leave-alone: the per-session sentinel still stops a burst."""
    hooks, proj = _world(tmp_path)
    env = _env(tmp_path, venv=sys.executable)
    subprocess.run(_argv(impl, hooks, proj), env=env, capture_output=True, timeout=180, check=True)
    ledger = proj / ".claude" / "context" / "UPDATE_DEFERRED.md"
    ledger.unlink()
    subprocess.run(_argv(impl, hooks, proj), env=env, capture_output=True, timeout=180, check=True)
    assert not ledger.exists()


def test_mcp_and_hook_surfaces_share_one_entry_shape():
    hook, mcp = build_entry("X", "hook"), build_entry("X", "mcp")
    assert hook.condition_id == mcp.condition_id == CONDITION_ID
    assert hook.command_to_apply == mcp.command_to_apply
    assert "MCP env" in mcp.title and "hook env" in hook.title


def test_cli_refuses_a_missing_folder(tmp_path):
    res = subprocess.run([sys.executable, "-m", "vco_lib.gate_skipped_deferral",
                          "--folder", str(tmp_path / "nope"), "--collection", "X"],
                         capture_output=True, text=True, timeout=60,
                         env=dict(child_env(), PYTHONPATH=str(REPO)))
    assert res.returncode == 1 and "not a directory" in res.stderr
