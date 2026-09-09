# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""kg-sync / kg-sync.ps1 stream + env parity (v0.2.92 delivery audit m8).

The bash sibling refuses on STDERR (a `>&2` block) and exports
``VIRTUAL_ENV`` before invoking the sync script. The PowerShell sibling
printed the identical refusal with ``Write-Host`` — the HOST stream, which
``2>/dev/null``, CI capture and every stderr-scraping consumer never see —
and never set ``$env:VIRTUAL_ENV``, so subprocess libraries probing it saw
no venv. Both are executed here THROUGH THE WRAPPER (no source scan):

* the refusal leg drives the real ``.ps1`` on a host with no qualifying
  venv and asserts exit 3 with the message on stderr and an EMPTY stdout;
* the VIRTUAL_ENV leg drives it through a qualifying interpreter and
  asserts the target script inherited the venv ROOT.

Interpreter-bound tests: skipped when no PowerShell is on PATH, refusing
to do so under CI (the parse gate's posture — a gate that is switched off
in the environment that should run it reports nothing).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.common.wrapper_staging import stage_scripts

REPO = Path(__file__).resolve().parent.parent
WRAPPER_PS1 = REPO / "templates" / "scripts" / "kg-sync.ps1"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _require_pwsh() -> str:
    exe = _powershell()
    if exe is None:
        assert not os.environ.get("CI"), (
            "PowerShell is absent in CI — this gate cannot run and must not "
            "be reported as passing."
        )
        pytest.skip("no PowerShell interpreter on this machine")
    return exe


def _stage_project(tmp_path: Path) -> Path:
    """A project-shaped dir with the wrapper installed where the bundle
    puts it (``<proj>/.claude/scripts/kg-sync.ps1``) plus a fake sync
    target the happy-path leg can observe."""
    scripts = tmp_path / ".claude" / "scripts"
    # v0.2.94: the wrapper and the `vct_venv_ladder.ps1` it dot-sources are
    # ONE shipped unit; staging only the wrapper stages a BROKEN install.
    stage_scripts(scripts, "kg-sync.ps1")
    (scripts / "sync_knowledge_graph.py").write_text(textwrap.dedent(
        """
        import os, sys
        # The observable contract: what the wrapper passed in.
        sys.stdout.write("VIRTUAL_ENV=" + os.environ.get("VIRTUAL_ENV", "") + "\\n")
        sys.stdout.write("KG_SYNC_PROJECT_ROOT="
                         + os.environ.get("KG_SYNC_PROJECT_ROOT", "") + "\\n")
        """
    ), encoding="utf-8")
    return tmp_path


def test_refusal_goes_to_stderr_not_the_host_stream(tmp_path: Path) -> None:
    """A refused run must be VISIBLE to stderr consumers: exit 3, the
    refusal text on stderr, nothing on stdout. Pre-m8 the message went to
    the host stream, where `2>nul` / CI error-scraping never saw it."""
    exe = _require_pwsh()
    proj = _stage_project(tmp_path)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")
    }
    proc = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(proj / ".claude" / "scripts" / "kg-sync.ps1"), "--all"],
        capture_output=True, text=True, env=env, cwd=str(proj), timeout=120,
    )
    assert proc.returncode == 3, (
        f"refused run must exit 3 (did-not-run), got {proc.returncode}: "
        f"{proc.stdout!r} {proc.stderr!r}"
    )
    assert "kg-sync: ERROR - no Python environment" in proc.stderr, (
        f"refusal must be on STDERR (m8): stderr={proc.stderr!r}"
    )
    assert "Refusing to run with an unqualified interpreter" in proc.stderr
    assert proc.stdout.strip() == "", (
        f"a refused run must not write the success surface to stdout: "
        f"{proc.stdout!r}"
    )


def test_happy_path_exports_the_venv_root(tmp_path: Path) -> None:
    """The target script must see VIRTUAL_ENV set to the venv ROOT — the
    same fact `export VIRTUAL_ENV="$VENV_PATH"` gives the bash sibling's
    children. Driven through the wrapper with a qualifying interpreter
    (``VCT_VENV`` tier; the qualification probe itself must pass)."""
    exe = _require_pwsh()
    # DELIBERATELY NOT `child_env()`: this mirrors the PYTHONPATH the SHIPPED
    # wrapper sets for its own qualification probe, so the test qualifies on
    # exactly the terms the wrapper does. `child_env()` builds a different
    # (pytest-pinned) environment, which would make the probe answer a
    # question the wrapper never asks. Allow-listed in
    # `tests/test_v0292_fixround_child_env_lint.py::_ALLOWLIST`.
    probe = subprocess.run(
        [sys.executable, "-c", "import weaviate"],
        capture_output=True, text=True,
        env={**os.environ,
             "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'claude_mcp_servers'}"},
    )
    if probe.returncode != 0:
        pytest.skip(
            "no interpreter with the `weaviate` client available — the "
            "wrapper's qualification probe cannot pass on this host"
        )
    proj = _stage_project(tmp_path)
    # A "venv" whose bin/python execs the real interpreter. A SYMLINK is
    # not enough: CPython locates pyvenv.cfg next to argv[0] WITHOUT
    # resolving symlinks, so a symlinked bin/python silently degrades to
    # the system interpreter and loses the venv's site-packages — the
    # wrapper's qualification probe then rightly refuses it. A tiny exec
    # wrapper keeps the real interpreter (and its site-packages) while
    # giving the venv its own bin/python path.
    fake_venv = tmp_path / "fake-venv"
    (fake_venv / "bin").mkdir(parents=True)
    # sys.executable VERBATIM — never .resolve()d: a venv's bin/python is
    # itself a symlink to the base interpreter, and venv discovery keys on
    # argv[0]'s directory (pyvenv.cfg), so resolving the symlink silently
    # replaces the venv interpreter with the system one.
    real = sys.executable
    py = fake_venv / "bin" / "python"
    py.write_text(f"#!/bin/sh\nexec '{real}' \"$@\"\n", encoding="utf-8")
    py.chmod(0o755)
    env = {
        **os.environ,
        "VCT_VENV": str(fake_venv),
        "VCT_INSTALL_ROOT": "",
        "VCT_ORCHESTRATOR_ROOT": "",
        "PYTHONPATH": f"{REPO}{os.pathsep}{REPO / 'claude_mcp_servers'}",
    }
    proc = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(proj / ".claude" / "scripts" / "kg-sync.ps1"), "--all"],
        capture_output=True, text=True, env=env, cwd=str(proj), timeout=120,
    )
    assert proc.returncode == 0, (
        f"happy-path run failed: {proc.stdout!r} {proc.stderr!r}"
    )
    lines = dict(
        ln.split("=", 1) for ln in proc.stdout.splitlines() if "=" in ln
    )
    assert lines.get("VIRTUAL_ENV") == str(fake_venv), (
        f"VIRTUAL_ENV must be the venv ROOT, got {lines.get('VIRTUAL_ENV')!r}"
    )
    assert lines.get("KG_SYNC_PROJECT_ROOT") == str(proj)
