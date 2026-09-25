# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""v0.2.97: the bundled session-state module's settings have a reader.

``launcher/bundled_manifests/vct-session-state.json`` declares
``CONTEXT_STATE_MAX_LINES`` and ``MEMORY_MAX_LINES``; before v0.2.97 nothing
read them (the manifest did not even parse, nothing materialized it, and the
``context-size-check`` hook hard-coded 500/300 and never looked at
MEMORY.md). The hook now resolves each: env var → the vct-hub ``/env``
through the shipped resolver (hub → file store → the project's ``.env``) →
the default. The hub half (``/env`` serving the bundled module's settings)
is pinned in Rust: ``project_env_delivers_the_bundled_session_state_settings``.

Every run pins the hub to the discard port and the file store / Claude home
to tmp dirs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "templates" / "hooks" / "context-size-check.sh"
RESOLVER = REPO / "templates" / "scripts" / "vct_secrets_resolve.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash hook")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project laid out the way the bundle ships it: the hook under
    .claude/hooks, the resolver under .claude/scripts."""
    proj = tmp_path / "my_proj"
    hooks = proj / ".claude" / "hooks"
    scripts = proj / ".claude" / "scripts"
    shutil.copytree(REPO / "templates" / "hooks" / "_lib", hooks / "_lib")
    shutil.copy2(HOOK, hooks / HOOK.name)
    scripts.mkdir(parents=True)
    shutil.copy2(RESOLVER, scripts / RESOLVER.name)
    lib = REPO / "templates" / "scripts" / "lib"
    if lib.is_dir():
        shutil.copytree(lib, scripts / "lib")
    return proj


def _run(project: Path, tmp_path: Path, **env_extra: str) -> str:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CONTEXT_STATE_MAX_LINES", "MEMORY_MAX_LINES", "VCT_DISABLE_HOOKS",
                        "VCT_HUB_TOKEN")}
    env.update({
        "CLAUDE_PROJECT_DIR": str(project),
        "VCT_HUB_PORT": "9",
        "VCT_STATE_DIR": str(tmp_path / "state"),
        "VCT_SECRETS_DIR": str(tmp_path / "secrets"),
        "VCT_CLAUDE_DIR": str(tmp_path / "claude-home"),
        "HOME": str(tmp_path / "home"),
    })
    env.update(env_extra)
    done = subprocess.run(
        [BASH, str(project / ".claude" / "hooks" / HOOK.name)],
        input=json.dumps({}), capture_output=True, text=True, cwd=project, env=env, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _context(project: Path, lines: int) -> None:
    (project / ".claude" / "CONTEXT_STATE.md").write_text("x\n" * lines)


def test_the_default_threshold_is_unchanged(project: Path, tmp_path: Path) -> None:
    """Leave-alone: no setting anywhere → the pre-v0.2.97 500 / 300."""
    _context(project, 320)
    out = _run(project, tmp_path)
    assert "Size Notice" in out and "(warning threshold: 300 lines)" in out
    _context(project, 120)
    assert _run(project, tmp_path) == ""


def test_the_env_setting_moves_the_threshold(project: Path, tmp_path: Path) -> None:
    _context(project, 120)
    out = _run(project, tmp_path, CONTEXT_STATE_MAX_LINES="100")
    assert "Size Alert (CRITICAL)" in out and "(threshold: 100 lines)" in out


def test_the_setting_resolves_through_the_shipped_resolver(project: Path, tmp_path: Path) -> None:
    """With nothing in the environment, the value comes through
    `vct_secrets_resolve.sh` — here its project `.env` tier (hub on the
    discard port); a launcher-set module setting arrives the same way via
    the hub's `/env` (pinned in Rust)."""
    (project / ".env").write_text("CONTEXT_STATE_MAX_LINES=100\n")
    _context(project, 120)
    out = _run(project, tmp_path)
    assert "(threshold: 100 lines)" in out


@pytest.mark.parametrize("bad", ["abc", "10", "999999", "-5"])
def test_an_unusable_value_falls_back_to_the_default(project: Path, tmp_path: Path, bad: str) -> None:
    _context(project, 320)
    out = _run(project, tmp_path, CONTEXT_STATE_MAX_LINES=bad)
    assert "(warning threshold: 300 lines)" in out


def test_memory_md_is_checked_at_its_setting(project: Path, tmp_path: Path) -> None:
    """MEMORY_MAX_LINES now has a reader: Claude Code's auto-memory file for
    THIS project (path slug = every non-alphanumeric char → '-')."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(project))
    memory = tmp_path / "claude-home" / "projects" / slug / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- entry\n" * 210)
    out = _run(project, tmp_path)
    assert "MEMORY.md Size Notice" in out and "(threshold: 200 lines)" in out
    assert "MEMORY.md" not in _run(project, tmp_path, MEMORY_MAX_LINES="300")


def test_the_manifest_defaults_are_what_the_hook_applies() -> None:
    manifest = json.loads((REPO / "launcher" / "bundled_manifests" / "vct-session-state.json").read_text())
    defaults = {s["key"]: s["default"] for s in manifest["settings"]}
    assert defaults == {"CONTEXT_STATE_MAX_LINES": 500, "MEMORY_MAX_LINES": 200}
    hook = HOOK.read_text()
    assert "resolve_threshold CONTEXT_STATE_MAX_LINES 500" in hook
    assert "resolve_threshold MEMORY_MAX_LINES 200" in hook
    ps1 = HOOK.with_suffix(".ps1").read_text(encoding="utf-8-sig")
    assert 'Resolve-Threshold -Key "CONTEXT_STATE_MAX_LINES" -Default 500' in ps1
    assert 'Resolve-Threshold -Key "MEMORY_MAX_LINES" -Default 200' in ps1


PWSH = shutil.which("pwsh")

_MANIFEST_SETTINGS = json.loads(
    (REPO / "launcher" / "bundled_manifests" / "vct-session-state.json").read_text()
)["settings"]


def _bound_cases(setting: dict) -> list[tuple[str, int]]:
    """(value, what the hook must apply) at and around the manifest's bounds."""
    lo, hi, default = setting["min"], setting["max"], setting["default"]
    return [(str(lo - 1), default), (str(lo), lo), (str(hi), hi), (str(hi + 1), default), ("", default)]


def _extract(source: str, pattern: str) -> str:
    match = re.search(pattern, source, re.S | re.M)
    assert match, pattern
    return match.group(0)


@pytest.mark.parametrize("setting", _MANIFEST_SETTINGS, ids=[s["key"] for s in _MANIFEST_SETTINGS])
def test_the_bash_hook_applies_the_manifests_bounds(setting: dict) -> None:
    """R7b F12: the hook's range check is the manifest's ``min``/``max`` — run
    the hook's own ``resolve_threshold`` at and around them. Raise the
    manifest ``max`` and the launcher accepts a value this hook would
    silently ignore; this goes red instead."""
    fn = _extract(HOOK.read_text(), r"^resolve_threshold\(\) \{\n.*?^\}\n")
    for value, want in _bound_cases(setting):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), setting["key"]: value}
        snippet = f'RESOLVED_SETTINGS=""\n{fn}resolve_threshold {setting["key"]} {setting["default"]}\n'
        done = subprocess.run([str(BASH), "-c", snippet], env=env, capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == str(want), (setting["key"], value, done.stdout)


@pytest.mark.skipif(PWSH is None, reason="PowerShell sibling needs pwsh")
@pytest.mark.parametrize("setting", _MANIFEST_SETTINGS, ids=[s["key"] for s in _MANIFEST_SETTINGS])
def test_the_powershell_hook_applies_the_manifests_bounds(setting: dict, tmp_path: Path) -> None:
    fn = _extract(HOOK.with_suffix(".ps1").read_text(encoding="utf-8-sig"),
                  r"^function Resolve-Threshold \{\n.*?^\}\n")
    lib = tmp_path / "lib.ps1"
    lib.write_text("$ResolvedSettings = @{}\n" + fn, encoding="utf-8")
    for value, want in _bound_cases(setting):
        env = {k: v for k, v in os.environ.items() if k != setting["key"]}
        if value:
            env[setting["key"]] = value
        done = subprocess.run(
            [str(PWSH), "-NoProfile", "-NonInteractive", "-Command",
             f'. "{lib}"; Resolve-Threshold -Key "{setting["key"]}" -Default {setting["default"]}'],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == str(want), (setting["key"], value, done.stdout)


@pytest.mark.skipif(PWSH is None, reason="PowerShell sibling needs pwsh")
def test_the_powershell_sibling_resolves_the_same_way(tmp_path: Path) -> None:
    """The .ps1 hook: default, env setting, the shipped resolver's `.env`
    tier (run as a child — it writes with [Console]::Out), MEMORY.md."""
    proj = tmp_path / "my_proj"
    hooks = proj / ".claude" / "hooks"
    scripts = proj / ".claude" / "scripts"
    shutil.copytree(REPO / "templates" / "hooks" / "_lib", hooks / "_lib")
    shutil.copy2(HOOK.with_suffix(".ps1"), hooks)
    scripts.mkdir(parents=True)
    shutil.copy2(RESOLVER.with_suffix(".ps1"), scripts)

    def run(**extra: str) -> str:
        env = {k: v for k, v in os.environ.items()
               if k not in ("CONTEXT_STATE_MAX_LINES", "MEMORY_MAX_LINES", "VCT_HUB_TOKEN")}
        env.update(CLAUDE_PROJECT_DIR=str(proj), VCT_HUB_PORT="9",
                   VCT_STATE_DIR=str(tmp_path / "state"), VCT_SECRETS_DIR=str(tmp_path / "secrets"),
                   VCT_CLAUDE_DIR=str(tmp_path / "ch"), HOME=str(tmp_path / "home"))
        env.update(extra)
        done = subprocess.run(
            [str(PWSH), "-NoProfile", "-File", str(hooks / "context-size-check.ps1")],
            input="{}", capture_output=True, text=True, cwd=proj, env=env, timeout=120,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    _context(proj, 320)
    assert "warning threshold: 300 lines" in run()
    _context(proj, 120)
    assert run().strip() == ""
    assert "threshold: 100 lines" in run(CONTEXT_STATE_MAX_LINES="100")
    (proj / ".env").write_text("CONTEXT_STATE_MAX_LINES=100\n")
    assert "threshold: 100 lines" in run()
    (proj / ".env").unlink()
    assert run(CONTEXT_STATE_MAX_LINES="abc").strip() == ""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(proj))
    memory = tmp_path / "ch" / "projects" / slug / "memory" / "MEMORY.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("- e\n" * 210)
    assert "MEMORY.md Size Notice" in run()
