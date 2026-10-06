# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.100 WP-17 (F-W1-16) — project hooks registered by a RELATIVE path.

Claude Code runs a hook command in the session's CURRENT directory. A command
such as ``bash .claude/hooks/x.sh`` fails with exit 127 as soon as the session
(or a subagent) works from a subdirectory — on every call, each failure
written into the transcript (field evidence: 23 780 such lines in one
transcript; a project-own guard failing 4 096 times).

VCO's shipped hooks are anchored by the bundle merge. A project's OWN hooks
are the user's configuration, so the bundle update DETECTS them and OFFERS the
anchored rewrite through a ``project_hooks_relative_paths`` deferral entry; the
user applies it with ``python -m vco_lib.hook_relative_paths anchor``; the
registry probe clears the entry once nothing relative remains. Pinned here:
the detector, the bundle wiring (offer, never rewrite), the rewrite (exactly
the relative invocations, through the one settings writer), the probe, and the
shipped templates (every hook command anchored at ``${CLAUDE_PROJECT_DIR}``).
Every file lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.common.child_env import child_env
from tests.common.launcher_db_fixture import make_launcher_db
from vco_lib import deferral_probes, hook_relative_paths, project_init
from vco_lib.deferral_registry import condition
from vco_lib.deferral_report import DeferralReport
from vco_lib.hook_relative_paths import (
    CID,
    anchor_relative_hooks,
    find_relative_hook_commands,
    relative_hooks_still_present,
    relative_scripts,
)
from vco_lib.hooks_settings import invoked_script_tokens

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = {
    "linux": REPO / "templates" / "settings.json.linux.template",
    "windows": REPO / "templates" / "settings.json.windows.template",
}

SHIPPED = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/notify-stop.sh"'
OWN_RELATIVE = "bash .claude/hooks/my-guard.sh"
OWN_DOT_RELATIVE = "bash ./.claude/hooks/other.sh --flag"
OWN_PS1 = "powershell -NoProfile -File .claude/hooks/win-guard.ps1"
OWN_ARG_ONLY = "bash wrapper.sh --target .claude/hooks/my-guard.sh"
OWN_INLINE = "python3 -m py_compile \"$CLAUDE_TOOL_ARG_FILE_PATH\""

# v0.2.101 (NB-13): `.claude/scripts/` invocations are project-own commands too.
# Their identity is the PROJECT-RELATIVE path (basenames collide across the
# script subfolders; scripts may be extension-less).
OWN_SCRIPT = "bash .claude/scripts/kg-sync --all"
OWN_SCRIPT_DOT = "bash ./.claude/scripts/lib/vct_project_config.sh"
OWN_SCRIPT_PS1 = "powershell -NoProfile -File .claude/scripts/win-guard.ps1"
OWN_SCRIPT_ARG_ONLY = "bash wrapper.sh --target .claude/scripts/not-invoked.sh"
OWN_SCRIPT_ANCHORED = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/scripts/kg-sync"'


# ─── the detector ───────────────────────────────────────────────────────


@pytest.mark.parametrize("command,expected", [
    (OWN_RELATIVE, ["my-guard.sh"]),
    (OWN_DOT_RELATIVE, ["other.sh"]),
    (OWN_PS1, ["win-guard.ps1"]),
    (SHIPPED, []),
    ('bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh"', []),
    (OWN_ARG_ONLY, []),
    (OWN_INLINE, []),
    (None, []),
    # .claude/scripts/ — identity is the project-relative path (v0.2.101 NB-13).
    (OWN_SCRIPT, [".claude/scripts/kg-sync"]),
    (OWN_SCRIPT_DOT, [".claude/scripts/lib/vct_project_config.sh"]),
    (OWN_SCRIPT_PS1, [".claude/scripts/win-guard.ps1"]),
    ('bash .claude/scripts/kg-search "q"', [".claude/scripts/kg-search"]),
    (OWN_SCRIPT_ANCHORED, []),
    (OWN_SCRIPT_ARG_ONLY, []),
])
def test_relative_scripts(command, expected):
    assert relative_scripts(command) == expected


def test_find_reports_the_anchored_rewrite():
    block = {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": OWN_RELATIVE},
        {"type": "command", "command": SHIPPED},
    ]}]}
    found = find_relative_hook_commands(block)
    assert found == [{
        "event": "PreToolUse", "matcher": "Bash", "command": OWN_RELATIVE,
        "anchored": 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/my-guard.sh"',
    }]


# ─── the shipped templates ──────────────────────────────────────────────


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_every_shipped_hook_command_is_anchored(flavour):
    """Every `.claude/hooks/` script a shipped template INVOKES starts at
    ${CLAUDE_PROJECT_DIR} — and the detector agrees (nothing relative)."""
    data = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    assert find_relative_hook_commands(data["hooks"]) == []
    invoked = 0
    for groups in data["hooks"].values():
        for group in groups:
            for item in group["hooks"]:
                for token in invoked_script_tokens(item["command"]):
                    if ".claude/hooks/" in token:
                        invoked += 1
                        assert token.startswith("${CLAUDE_PROJECT_DIR"), (flavour, item["command"])
    assert invoked >= 40


# ─── the bundle update: offer, never rewrite ────────────────────────────


def _template() -> dict:
    return {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": SHIPPED}]}]}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    orch = tmp_path / "orch"
    (orch / "templates").mkdir(parents=True)
    (orch / "vct-module.json").write_text("{}\n", encoding="utf-8")
    for os_name in ("linux", "windows"):
        (orch / "templates" / f"settings.json.{os_name}.template").write_text(
            json.dumps(_template(), indent=2), encoding="utf-8")
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    db = make_launcher_db(tmp_path / "state", projects=[
        {"project_id": "p1", "name": "Proj", "folder_path": project}])
    monkeypatch.setenv("VCT_LAUNCHER_DB_PATH", str(db))
    return orch, project


def _write_settings(project: Path, commands: list, name: str = "settings.json") -> Path:
    data = _template()
    data["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": c} for c in commands]}]
    path = project / ".claude" / name
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _update(project: Path, orch: Path) -> dict:
    return project_init.install_project_bundle(project, orchestrator_root=orch, update_mode=True)


def test_update_offers_the_rewrite_and_leaves_own_hooks_alone(world):
    orch, project = world
    settings = _write_settings(project, [OWN_RELATIVE, OWN_INLINE])
    _update(project, orch)
    data = json.loads(settings.read_text(encoding="utf-8"))
    own = [h["command"] for h in data["hooks"]["PreToolUse"][0]["hooks"]]
    assert own == [OWN_RELATIVE, OWN_INLINE], "the user's own hook was rewritten without asking"
    entry = DeferralReport.read(project).entry_for(CID)
    assert entry is not None
    assert OWN_RELATIVE in entry.detected
    assert '"${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/my-guard.sh"' in entry.detected
    assert "python -m vco_lib.hook_relative_paths anchor --project-folder" in entry.command_to_apply
    assert OWN_INLINE not in entry.detected


def test_update_with_only_anchored_hooks_records_nothing(world):
    orch, project = world
    _write_settings(project, [OWN_INLINE, 'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/mine.sh"'])
    _update(project, orch)
    assert DeferralReport.read(project).entry_for(CID) is None


def test_settings_local_json_is_scanned_too(world):
    orch, project = world
    _write_settings(project, [OWN_INLINE])
    _write_settings(project, [OWN_PS1], name="settings.local.json")
    _update(project, orch)
    entry = DeferralReport.read(project).entry_for(CID)
    assert entry is not None and "settings.local.json" in entry.detected


# ─── the rewrite and the probe ──────────────────────────────────────────


def test_anchor_rewrites_exactly_the_relative_invocations(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    settings = _write_settings(project, [OWN_RELATIVE, OWN_DOT_RELATIVE, OWN_ARG_ONLY, OWN_INLINE, SHIPPED])
    assert relative_hooks_still_present(project) is True
    outcome = anchor_relative_hooks(project)
    assert outcome["refused"] == {}
    data = json.loads(settings.read_text(encoding="utf-8"))
    got = [h["command"] for h in data["hooks"]["PreToolUse"][0]["hooks"]]
    assert got == [
        'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/my-guard.sh"',
        'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/other.sh" --flag',
        OWN_ARG_ONLY, OWN_INLINE, SHIPPED,
    ]
    assert relative_hooks_still_present(project) is False
    # Idempotent: a second run changes nothing.
    assert anchor_relative_hooks(project)["changed"] == {}


def test_find_reports_the_anchored_script_rewrite():
    """A relative `.claude/scripts/` invocation is detected and offered the
    anchored form — identity is the project-relative path, argument-position
    paths and already-anchored forms are left alone (v0.2.101 NB-13)."""
    block = {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": OWN_SCRIPT},
        {"type": "command", "command": OWN_SCRIPT_ARG_ONLY},
        {"type": "command", "command": OWN_SCRIPT_ANCHORED},
    ]}]}
    assert find_relative_hook_commands(block) == [{
        "event": "PreToolUse", "matcher": "Bash", "command": OWN_SCRIPT,
        "anchored": 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/scripts/kg-sync" --all',
    }]


def test_anchor_rewrites_relative_script_invocations(tmp_path):
    """The rewrite applies to `.claude/scripts/` invocations too, in the
    per-OS form (.sh / extension-less → `:-.` fallback; .ps1 → exact
    placeholder), through the one settings writer."""
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    settings = _write_settings(
        project,
        [OWN_SCRIPT, OWN_SCRIPT_DOT, OWN_SCRIPT_PS1, OWN_SCRIPT_ARG_ONLY, OWN_RELATIVE],
    )
    assert relative_hooks_still_present(project) is True
    outcome = anchor_relative_hooks(project)
    assert outcome["refused"] == {}
    data = json.loads(settings.read_text(encoding="utf-8"))
    got = [h["command"] for h in data["hooks"]["PreToolUse"][0]["hooks"]]
    assert got == [
        'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/scripts/kg-sync" --all',
        'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/scripts/lib/vct_project_config.sh"',
        'powershell -NoProfile -File "${CLAUDE_PROJECT_DIR}/.claude/scripts/win-guard.ps1"',
        OWN_SCRIPT_ARG_ONLY,
        'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/my-guard.sh"',
    ]
    assert relative_hooks_still_present(project) is False
    # Idempotent: a second run changes nothing.
    assert anchor_relative_hooks(project)["changed"] == {}


def test_the_anchored_command_runs_from_a_subdirectory(tmp_path):
    """The point of the rewrite: the relative form fails after a `cd`, the
    anchored one does not."""
    project = tmp_path / "proj"
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "my-guard.sh").write_text("echo guard-ran\n", encoding="utf-8")
    sub = project / "deep" / "sub"
    sub.mkdir(parents=True)
    env = dict(child_env(), CLAUDE_PROJECT_DIR=str(project))
    rel = subprocess.run(["sh", "-c", OWN_RELATIVE], cwd=sub, env=env, capture_output=True, text=True)
    assert rel.returncode != 0
    anchored = find_relative_hook_commands(
        {"Stop": [{"hooks": [{"command": OWN_RELATIVE}]}]})[0]["anchored"]
    ok = subprocess.run(["sh", "-c", anchored], cwd=sub, env=env, capture_output=True, text=True)
    assert ok.returncode == 0 and "guard-ran" in ok.stdout


def test_dry_run_writes_nothing(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    settings = _write_settings(project, [OWN_RELATIVE])
    before = settings.read_bytes()
    outcome = anchor_relative_hooks(project, dry_run=True)
    assert list(outcome["changed"]) == [".claude/settings.json"]
    assert settings.read_bytes() == before


def test_unreadable_settings_keeps_the_entry(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    assert relative_hooks_still_present(project) is None


def test_cli_anchor(tmp_path):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    _write_settings(project, [OWN_RELATIVE])
    res = subprocess.run([sys.executable, "-m", "vco_lib.hook_relative_paths", "anchor",
                          "--project-folder", str(project)],
                         capture_output=True, text=True, timeout=60,
                         env=dict(child_env(), PYTHONPATH=str(REPO)), cwd=str(REPO))
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["ok"] and ".claude/settings.json" in out["changed"]
    assert relative_hooks_still_present(project) is False


def test_registry_row_and_probe_are_wired():
    spec = condition(CID)
    assert spec is not None and spec.condition_class == "action_required"
    probe = spec.clear_probe.split(":")[-1]
    assert deferral_probes.PROBES[probe] is deferral_probes.project_hooks_relative_paths_still_present
    assert hook_relative_paths.CID == CID


def test_hook_command_key_treats_script_spellings_as_one_registration():
    """The shared ``only=None`` anchoring path — ``hook_command_key``, the key
    the launcher's Hooks tab keys rows by and :func:`hooks_settings._locate`
    matches older spellings with — recognises a relative and an anchored
    ``.claude/scripts/`` invocation as ONE registration, so a hook this feature
    rewrote is still locatable from its older DB row. Two DIFFERENT commands
    running one script stay distinct (v0.2.101 NB-13)."""
    from vco_lib.hook_retirements import hook_command_key

    relative = "bash .claude/scripts/kg-sync --all"
    anchored = 'bash "${CLAUDE_PROJECT_DIR:-.}/.claude/scripts/kg-sync" --all'
    assert hook_command_key(relative) == hook_command_key(anchored)
    assert hook_command_key(relative) != hook_command_key("bash .claude/scripts/kg-sync")
