# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Every VCO hook command is anchored at the project root (v0.2.97).

Claude Code runs a hook command in the session's CURRENT directory, and that
directory follows ``cd`` and worktrees. Every hook VCO shipped was relative —
``bash .claude/hooks/x.sh`` / ``powershell … -File .claude/hooks/x.ps1`` — so
once a session's cwd moved, every VCO hook failed with "No such file or
directory" (seen live: a WorktreeCreate hook after a Bash ``cd`` persisted).

The shipped form is now the double-quoted shell form
``bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.sh"``: Claude Code substitutes the
placeholder where it supports that, and exports ``CLAUDE_PROJECT_DIR`` where it
does not, which ``sh`` / Git Bash expand inside the quotes.

Pinned here: the templates (and what a fresh project gets) hold no relative
hook path; an existing project's relative VCO entries are rewritten by the
ordinary bundle update while the user's own hooks stay byte-for-byte; a
launcher-parked relative entry is restored anchored, and still keeps its
anchored template twin out of the merge; the hooks editor matches the two
spellings as one registration. Every settings file lives under ``tmp_path``.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.common.child_env import child_env  # noqa: E402
from vco_lib import hooks_settings, project_init, settings_merge  # noqa: E402
from vco_lib.hook_retirements import hook_command_key, vco_hook_script_identity  # noqa: E402
from vco_lib.parked_hooks import ParkedHook, ParkedHooksState  # noqa: E402

TEMPLATES = {
    "linux": REPO_ROOT / "templates" / "settings.json.linux.template",
    "windows": REPO_ROOT / "templates" / "settings.json.windows.template",
}
ANCHOR = "${CLAUDE_PROJECT_DIR}/"
USER_OWN = [
    "bash .claude/hooks/my-own-hook.sh",               # under .claude/hooks, not shipped
    "python3 /home/me/hook.py --loud",
    "bash wrapper.sh --target .claude/hooks/notify-stop.sh",  # a VCO path as an ARGUMENT
]


def _commands(hooks: dict) -> list[str]:
    return [
        h["command"]
        for groups in hooks.values()
        for g in groups
        for h in g.get("hooks", [])
        if isinstance(h, dict) and isinstance(h.get("command"), str)
    ]


def _relative_hook_paths(command: str) -> list[str]:
    """Every `.claude/hooks/` occurrence NOT immediately anchored."""
    norm = command.replace("\\", "/")
    return [m.group(0) for m in re.finditer(r"\.claude/hooks/\S+", norm)
            if not norm[: m.start()].endswith(ANCHOR)]


def _deanchor(command: str) -> str:
    """The pre-v0.2.97 spelling of a template command."""
    return command.replace(f'"{ANCHOR}.claude/hooks/', ".claude/hooks/").replace('.sh"', ".sh").replace(
        '.ps1"', ".ps1")


def _old_install(template: dict) -> dict:
    """A pre-v0.2.97 install's settings: every VCO command relative, the
    user's own hooks beside them."""
    old = copy.deepcopy(template)
    for groups in old["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                h["command"] = _deanchor(h["command"])
    old["hooks"].setdefault("Stop", []).append(
        {"hooks": [{"type": "command", "command": c} for c in USER_OWN]}
    )
    return old


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_the_templates_hold_no_relative_hook_path(flavour: str) -> None:
    template = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    commands = _commands(template["hooks"])
    assert len(commands) >= 40
    for command in commands:
        assert _relative_hook_paths(command) == [], command
        assert vco_hook_script_identity(command), f"not recognised as a VCO hook: {command}"
        # The shipped form is a fixed point of the rewrite.
        assert hooks_settings.anchor_hook_command(command) == command


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_a_fresh_project_gets_only_anchored_hooks(flavour: str, tmp_path: Path) -> None:
    """What the bundle WRITES for a new project (the create path)."""
    target = tmp_path / "proj" / ".claude" / "settings.json"
    status, _ = project_init._merge_settings_template_for_bundle(
        TEMPLATES[flavour], target, dry_run=False)
    assert status == "created"
    written = json.loads(target.read_text(encoding="utf-8"))
    for command in _commands(written["hooks"]):
        assert _relative_hook_paths(command) == [], command


@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_an_existing_project_is_rewritten_on_update_and_user_hooks_are_untouched(
    flavour: str, tmp_path: Path,
) -> None:
    """The ordinary bundle update — the same identity supersede that retired
    the ``VCT_DISABLE_HOOKS`` prefix — rewrites every relative VCO entry to
    the anchored form, exactly once each, in place; the user's own hooks,
    including one under ``.claude/hooks/`` VCO does not ship, keep their bytes."""
    template = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_old_install(template), indent=2) + "\n", encoding="utf-8")

    status, _ = project_init._merge_settings_template_for_bundle(
        TEMPLATES[flavour], target, dry_run=False, parked=ParkedHooksState(readable=True))
    assert status == "merged"
    merged = json.loads(target.read_text(encoding="utf-8"))["hooks"]
    commands = _commands(merged)
    for shipped in _commands(template["hooks"]):
        assert shipped in commands
    vco = [c for c in commands if c not in USER_OWN]
    assert vco and all(_relative_hook_paths(c) == [] for c in vco), vco
    assert sorted(vco) == sorted(_commands(template["hooks"])), "stacked or lost a VCO hook"
    for mine in USER_OWN:
        assert commands.count(mine) == 1, f"the user's own hook changed: {mine}"

    # Idempotent: the next update changes nothing.
    status, _ = project_init._merge_settings_template_for_bundle(
        TEMPLATES[flavour], target, dry_run=False, parked=ParkedHooksState(readable=True))
    assert status == "unchanged"


def _parked_entry(command: str) -> dict:
    return {
        "schema": hooks_settings.PARKED_SCHEMA_VERSION, "event": "Stop", "matcher": "",
        "group_index": 0, "hook_index": 0, "group_removed": True, "group_extra": {},
        "group_key_index": 0, "event_index": None, "hooks_key_index": None,
        "item": {"type": "command", "command": command, "timeout": 5},
    }


def _doc(tmp_path: Path, data: dict) -> hooks_settings.SettingsDoc:
    return hooks_settings.SettingsDoc(
        path=tmp_path / "settings.json", data=data, indent=2, trailing_newline=True)


def test_a_parked_relative_vco_entry_is_restored_anchored(tmp_path: Path) -> None:
    shipped = hooks_settings.shipped_hook_scripts(REPO_ROOT)
    assert shipped and "notify-stop.sh" in shipped
    doc = _doc(tmp_path, {"hooks": {}})
    assert hooks_settings.insert_hook(
        doc, _parked_entry("bash .claude/hooks/notify-stop.sh"), anchor_scripts=shipped)
    item = doc.data["hooks"]["Stop"][0]["hooks"][0]
    assert item == {"type": "command",
                    "command": 'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/notify-stop.sh"',
                    "timeout": 5}

    # Leave-alone: the user's OWN parked hook comes back byte-for-byte.
    doc = _doc(tmp_path, {"hooks": {}})
    assert hooks_settings.insert_hook(
        doc, _parked_entry("bash .claude/hooks/my-own-hook.sh"), anchor_scripts=shipped)
    assert doc.data["hooks"]["Stop"][0]["hooks"][0]["command"] == "bash .claude/hooks/my-own-hook.sh"


def test_unreadable_templates_anchor_nothing(tmp_path: Path) -> None:
    """No template → no list of VCO hooks → nothing is rewritten on a guess."""
    assert hooks_settings.shipped_hook_scripts(tmp_path) is None


def test_the_enable_cli_restores_a_parked_relative_entry_anchored(tmp_path: Path) -> None:
    """End to end through the verb the launcher and the hub run."""
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text('{\n  "hooks": {}\n}\n', encoding="utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.hooks_settings", "enable", "--project-folder", str(project),
         "--entry-json", json.dumps(_parked_entry("bash .claude/hooks/notify-stop.sh"))],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT),
        env=child_env(VCT_INSTALL_ROOT=str(REPO_ROOT)),
    )
    assert done.returncode == 0, done.stderr
    written = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert _commands(written["hooks"]) == ['bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/notify-stop.sh"']


def test_a_parked_relative_row_keeps_its_anchored_template_twin_out() -> None:
    """The resurrection guard across the rewrite: a hook the user disabled
    while it was spelled relatively must not come back when the template now
    ships it anchored."""
    template = json.loads(TEMPLATES["linux"].read_text(encoding="utf-8"))["hooks"]
    parked = ParkedHooksState(readable=True, hooks=(
        ParkedHook(event="Stop", matcher="", command="bash .claude/hooks/notify-stop.sh"),))
    user = {k: v for k, v in copy.deepcopy(template).items() if k != "Stop"}
    user["Stop"] = [{"hooks": [
        {"type": "command", "command": _deanchor(c)}
        for c in _commands({"Stop": template["Stop"]}) if "notify-stop.sh" not in c
    ]}]
    kept_out: list = []
    merged = settings_merge.merge_hooks_block(user, template, parked=parked, kept_out=kept_out)
    assert not any("notify-stop.sh" in c for c in _commands({"Stop": merged["Stop"]}))
    assert any(r["command"].endswith('notify-stop.sh"') for r in kept_out), kept_out


def test_the_editor_matches_both_spellings_as_one_registration(tmp_path: Path) -> None:
    """The Hooks tab / hub disable a hook by the command their DB row holds —
    possibly the old spelling; the editor must find the anchored entry."""
    anchored = 'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/notify-stop.sh"'
    for old in ("bash .claude/hooks/notify-stop.sh",
                '[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/notify-stop.sh'):
        assert hook_command_key(old) == hook_command_key(anchored), old
        doc = _doc(tmp_path, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": anchored}]}]}})
        parked = hooks_settings.remove_hook(doc, "Stop", "", old)
        assert parked["item"]["command"] == anchored
    # Two DIFFERENT commands running one script are not one registration.
    assert hook_command_key("bash .claude/hooks/notify-stop.sh --loud") != hook_command_key(anchored)


def test_the_list_verb_reports_one_key_for_both_spellings(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    anchored = 'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/notify-stop.sh"'
    (project / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": anchored}]}]}}), encoding="utf-8")
    old = "bash .claude/hooks/notify-stop.sh"
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.hooks_settings", "list", "--project-folder", str(project),
         "--keys-for-json", json.dumps([old])],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT), env=child_env(),
    )
    assert done.returncode == 0, done.stderr
    keys = json.loads(done.stdout)["keys"]
    assert keys[old] == keys[anchored]


def test_a_starter_script_is_seeded_for_the_anchored_form(tmp_path: Path) -> None:
    """The Hooks tab suggests the anchored form; its starter still lands."""
    result = hooks_settings.create_starter_script(
        tmp_path, 'bash "${CLAUDE_PROJECT_DIR}/.claude/hooks/brand-new.sh"', "Stop")
    assert result is not None and result["created"]
    assert (tmp_path / ".claude" / "hooks" / "brand-new.sh").is_file()
