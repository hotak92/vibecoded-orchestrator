# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Every VCO hook command is anchored at the project root (v0.2.97).

Claude Code runs a hook command in the session's CURRENT directory, and that
directory follows ``cd`` and worktrees. Every hook VCO shipped was relative —
``bash .claude/hooks/x.sh`` / ``powershell … -File .claude/hooks/x.ps1`` — so
once a session's cwd moved, every VCO hook failed with "No such file or
directory" (seen live: a WorktreeCreate hook after a Bash ``cd`` persisted).

The shipped form is PER OS (review R8, G2 + M1):

* Linux/macOS — ``bash "${CLAUDE_PROJECT_DIR:-.}/.claude/hooks/x.sh"``. The
  POSIX ``:-`` default degrades to the project cwd when NEITHER of Claude
  Code's two mechanisms is present (placeholder substitution, the exported
  env var), instead of expanding to ``/.claude/hooks/x.sh``. Never worse than
  the pre-v0.2.97 relative form.
* Windows — ``powershell … -File "${CLAUDE_PROJECT_DIR}/.claude/hooks/x.ps1"``
  with the EXACT placeholder. Substitution is the documented mechanism and
  works whichever shell Claude Code spawns the command through; a ``:-``
  default is POSIX-only and would break under the PowerShell fallback shell.

Pinned here: the templates (and what a fresh project gets) hold no relative
hook path and carry the per-OS anchor form; the Linux form actually RESOLVES
under ``sh -c`` with ``CLAUDE_PROJECT_DIR`` unset (the fallback) and set (from
a foreign cwd); an existing project's VCO entries — relative, exact-placeholder,
or fallback-spelled — are rewritten by the ordinary bundle update to the
current form while the user's own hooks stay byte-for-byte; a launcher-parked
entry in any spelling is restored in the current form, and still keeps its
anchored template twin out of the merge; the hooks editor matches all the
spellings as one registration. Every settings file lives under ``tmp_path``.
"""
from __future__ import annotations

import copy
import json
import os
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
#: The two anchor spellings VCO has shipped. EXACT is the Windows form (and
#: the Linux form earlier in the v0.2.97 cycle); FALLBACK is the Linux/macOS
#: form shipped at tag time.
ANCHOR_EXACT = "${CLAUDE_PROJECT_DIR}/"
ANCHOR_FALLBACK = "${CLAUDE_PROJECT_DIR:-.}/"
#: The spellings of one registration the migration and the identity key must
#: all recognise: the pre-v0.2.97 relative form, the exact placeholder, and
#: the current fallback form.
SPELLINGS = ("relative", "exact_placeholder", "fallback")
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
            if not norm[: m.start()].endswith((ANCHOR_EXACT, ANCHOR_FALLBACK))]


def _deanchor(command: str) -> str:
    """The pre-v0.2.97 spelling of a template command."""
    out = command
    for anchor in (ANCHOR_FALLBACK, ANCHOR_EXACT):
        out = out.replace(f'"{anchor}.claude/hooks/', ".claude/hooks/")
    return out.replace('.sh"', ".sh").replace('.ps1"', ".ps1")


def _in_spelling(command: str, spelling: str) -> str:
    """A current template command respelled as an older registration form."""
    rel = _deanchor(command)
    if spelling == "relative":
        return rel
    if spelling == "exact_placeholder":
        at = rel.index(".claude/hooks/")
        return f'{rel[:at]}"{ANCHOR_EXACT}{rel[at:]}"'
    assert spelling == "fallback", spelling
    return command


def _old_install(template: dict, spelling: str = "relative") -> dict:
    """An older install's settings: every VCO command in `spelling`, the
    user's own hooks beside them."""
    old = copy.deepcopy(template)
    for groups in old["hooks"].values():
        for g in groups:
            for h in g["hooks"]:
                h["command"] = _in_spelling(h["command"], spelling)
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
def test_the_templates_carry_the_per_os_anchor_form(flavour: str) -> None:
    """Linux/macOS ship the `:-.` fallback form (it resolves even when Claude
    Code provides neither the substitution nor the env var); Windows ships the
    EXACT placeholder (`:-` is POSIX-only and breaks under the PowerShell
    shell fallback)."""
    template = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    commands = _commands(template["hooks"])
    assert commands
    for command in commands:
        if flavour == "linux":
            assert f'"{ANCHOR_FALLBACK}' in command, command
        else:
            assert f'"{ANCHOR_EXACT}' in command, command
            assert ":-" not in command, "PowerShell cannot expand a POSIX :- default"


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


@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("flavour", sorted(TEMPLATES))
def test_an_existing_project_is_rewritten_on_update_and_user_hooks_are_untouched(
    flavour: str, spelling: str, tmp_path: Path,
) -> None:
    """The ordinary bundle update — the same identity supersede that retired
    the ``VCT_DISABLE_HOOKS`` prefix — rewrites every VCO entry in ANY prior
    spelling (relative, exact placeholder, this cycle's fallback form) to the
    current template form, exactly once each, in place; the user's own hooks,
    including one under ``.claude/hooks/`` VCO does not ship, keep their bytes."""
    template = json.loads(TEMPLATES[flavour].read_text(encoding="utf-8"))
    target = tmp_path / "proj" / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(_old_install(template, spelling), indent=2) + "\n", encoding="utf-8")

    status, _ = project_init._merge_settings_template_for_bundle(
        TEMPLATES[flavour], target, dry_run=False, parked=ParkedHooksState(readable=True))
    # An older spelling is superseded in place; an install already in the
    # current form is a no-op. Either way the OUTCOME below is the contract:
    # exactly the template's commands, each once, user hooks untouched.
    assert status in {"merged", "unchanged"}, status
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


def _sh_env(**set_vars: str) -> dict:
    """``os.environ`` for a shell probe, with ``CLAUDE_PROJECT_DIR`` removed
    unless the probe sets it explicitly."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env.update(set_vars)
    return env


def test_the_linux_form_resolves_with_and_without_the_env_var(tmp_path: Path) -> None:
    """The rendered Linux command string, EXECUTED under ``sh -c`` the way
    Claude Code runs a shell-form hook: with ``CLAUDE_PROJECT_DIR`` unset it
    falls back to the project cwd (the ``:-.`` default); with it set it
    resolves from a DIFFERENT cwd. The exact-placeholder form without the env
    var is the failure the fallback exists to prevent — pinned as contrast."""
    project = tmp_path / "proj"
    hooks_dir = project / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    relative = "bash .claude/hooks/anchor-fallback-probe.sh"
    rendered = hooks_settings.anchor_hook_command(relative)
    assert rendered == f'bash "{ANCHOR_FALLBACK}.claude/hooks/anchor-fallback-probe.sh"'

    probe = hooks_dir / "anchor-fallback-probe.sh"

    def _run(command: str, env: dict, cwd: str, sentinel: Path) -> int:
        probe.write_text(f'#!/bin/sh\nprintf ok > "{sentinel}"\n', encoding="utf-8")
        done = subprocess.run(
            ["/bin/sh", "-c", command], cwd=cwd, env=env,
            capture_output=True, text=True, timeout=30)
        return done.returncode

    # No env var, cwd = the project: the `:-.` default resolves the script.
    sentinel = tmp_path / "fired-fallback-unset"
    assert _run(rendered, _sh_env(), str(project), sentinel) == 0, \
        "the fallback form must resolve from the project cwd"
    assert sentinel.exists()

    # Env var set, cwd somewhere else entirely: the anchored path wins.
    sentinel = tmp_path / "fired-anchored-foreign-cwd"
    assert _run(rendered, _sh_env(CLAUDE_PROJECT_DIR=str(project)), str(tmp_path), sentinel) == 0, \
        "the anchored form must resolve from a foreign cwd"
    assert sentinel.exists()

    # Contrast: the exact-placeholder spelling without the env var expands to
    # `/.claude/hooks/...` and fails — why Linux does not ship that form.
    exact = rendered.replace(ANCHOR_FALLBACK, ANCHOR_EXACT)
    sentinel = tmp_path / "fired-exact-unset"
    assert _run(exact, _sh_env(), str(project), sentinel) != 0, \
        "the exact form MUST fail without the env var"
    assert not sentinel.exists()

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


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_parked_vco_entry_is_restored_in_the_current_form(spelling: str, tmp_path: Path) -> None:
    shipped = hooks_settings.shipped_hook_scripts(REPO_ROOT)
    assert shipped and "notify-stop.sh" in shipped
    current = f'bash "{ANCHOR_FALLBACK}.claude/hooks/notify-stop.sh"'
    parked_command = _in_spelling(current, spelling)
    doc = _doc(tmp_path, {"hooks": {}})
    assert hooks_settings.insert_hook(doc, _parked_entry(parked_command), anchor_scripts=shipped)
    item = doc.data["hooks"]["Stop"][0]["hooks"][0]
    assert item == {"type": "command", "command": current, "timeout": 5}

    # Leave-alone: the user's OWN parked hook comes back byte-for-byte.
    doc = _doc(tmp_path, {"hooks": {}})
    assert hooks_settings.insert_hook(
        doc, _parked_entry("bash .claude/hooks/my-own-hook.sh"), anchor_scripts=shipped)
    assert doc.data["hooks"]["Stop"][0]["hooks"][0]["command"] == "bash .claude/hooks/my-own-hook.sh"


def test_a_windows_parked_entry_is_restored_with_the_exact_placeholder(tmp_path: Path) -> None:
    """The Windows form keeps the exact placeholder — no `:-` default, which
    the PowerShell shell fallback cannot expand."""
    shipped = hooks_settings.shipped_hook_scripts(REPO_ROOT)
    assert shipped is not None and "notify-stop.ps1" in shipped
    doc = _doc(tmp_path, {"hooks": {}})
    assert hooks_settings.insert_hook(
        doc, _parked_entry(
            "powershell -NoProfile -ExecutionPolicy Bypass -File .claude/hooks/notify-stop.ps1"),
        anchor_scripts=shipped)
    command = doc.data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert command == (
        "powershell -NoProfile -ExecutionPolicy Bypass -File "
        f'"{ANCHOR_EXACT}.claude/hooks/notify-stop.ps1"'
    )
    assert ":-" not in command


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
    assert _commands(written["hooks"]) == [f'bash "{ANCHOR_FALLBACK}.claude/hooks/notify-stop.sh"']


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


def test_the_editor_matches_all_spellings_as_one_registration(tmp_path: Path) -> None:
    """The Hooks tab / hub disable a hook by the command their DB row holds —
    possibly any older spelling; the editor must find the anchored entry."""
    current = f'bash "{ANCHOR_FALLBACK}.claude/hooks/notify-stop.sh"'
    for spelling, old in {
        "relative": "bash .claude/hooks/notify-stop.sh",
        "exact_placeholder": f'bash "{ANCHOR_EXACT}.claude/hooks/notify-stop.sh"',
        "fallback": current,
    }.items():
        assert hook_command_key(old) == hook_command_key(current), spelling
        doc = _doc(tmp_path, {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": current}]}]}})
        parked = hooks_settings.remove_hook(doc, "Stop", "", old)
        assert parked["item"]["command"] == current
    # The guard-prefixed pre-v0.2.97 form is the same registration too.
    assert hook_command_key('[ -n "$VCT_DISABLE_HOOKS" ] || bash .claude/hooks/notify-stop.sh') \
        == hook_command_key(current)
    # Two DIFFERENT commands running one script are not one registration.
    assert hook_command_key("bash .claude/hooks/notify-stop.sh --loud") != hook_command_key(current)


def test_the_list_verb_reports_one_key_for_every_spelling(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    current = f'bash "{ANCHOR_FALLBACK}.claude/hooks/notify-stop.sh"'
    (project / ".claude" / "settings.json").write_text(json.dumps(
        {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": current}]}]}}), encoding="utf-8")
    others = ["bash .claude/hooks/notify-stop.sh",
              f'bash "{ANCHOR_EXACT}.claude/hooks/notify-stop.sh"']
    done = subprocess.run(
        [sys.executable, "-m", "vco_lib.hooks_settings", "list", "--project-folder", str(project),
         "--keys-for-json", json.dumps(others)],
        capture_output=True, text=True, timeout=60, cwd=str(REPO_ROOT), env=child_env(),
    )
    assert done.returncode == 0, done.stderr
    keys = json.loads(done.stdout)["keys"]
    assert keys[current] == keys[others[0]] == keys[others[1]]


def test_a_starter_script_is_seeded_for_the_anchored_form(tmp_path: Path) -> None:
    """The Hooks tab suggests the anchored form; its starter still lands."""
    result = hooks_settings.create_starter_script(
        tmp_path, f'bash "{ANCHOR_FALLBACK}.claude/hooks/brand-new.sh"', "Stop")
    assert result is not None and result["created"]
    assert (tmp_path / ".claude" / "hooks" / "brand-new.sh").is_file()
