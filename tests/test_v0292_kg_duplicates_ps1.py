# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""kg-duplicates.ps1 (v0.2.92 delivery audit m3 / R42) — driven, not scanned.

The Windows sibling for the ``kg-duplicates`` bash wrapper did not exist:
Windows users were documented into a POSIX-only command (the audit's m3
pre-existing gap). This file proves the shipped ``.ps1`` WORKS through its
production entry points:

* the venv ladder refuses on STDERR with exit 1 when no candidate
  qualifies (the ``2>nul``-visible shape the m8 fix established for
  kg-sync — applied here from birth, not retrofitted);
* the happy path forwards EVERY argument to ``detect_duplicates.py``
  through a qualifying interpreter.

Interpreter-bound tests: skipped when no PowerShell is on PATH, refusing
to do so under CI (the parse gate's posture).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WRAPPER_PS1 = REPO / "templates" / "scripts" / "kg-duplicates.ps1"


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
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(WRAPPER_PS1, scripts / "kg-duplicates.ps1")
    return tmp_path


def test_refusal_is_on_stderr_with_exit_1(tmp_path: Path) -> None:
    exe = _require_pwsh()
    proj = _stage_project(tmp_path)
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("VCT_VENV", "VCT_INSTALL_ROOT", "VCT_ORCHESTRATOR_ROOT")
    }
    proc = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(proj / ".claude" / "scripts" / "kg-duplicates.ps1")],
        capture_output=True, text=True, env=env, cwd=str(proj), timeout=120,
    )
    assert proc.returncode == 1, (
        f"refused run must exit 1, got {proc.returncode}: "
        f"{proc.stdout!r} {proc.stderr!r}"
    )
    assert "kg-duplicates: ERROR - no Python environment" in proc.stderr, (
        f"refusal must be on STDERR: stderr={proc.stderr!r}"
    )
    assert proc.stdout.strip() == "", f"stdout must stay empty: {proc.stdout!r}"


def test_forwards_every_argument_to_detect_duplicates(tmp_path: Path) -> None:
    """The wrapper is a dumb forwarder: threshold/output flags must reach
    detect_duplicates.py verbatim, through a qualifying interpreter."""
    exe = _require_pwsh()
    proj = _stage_project(tmp_path)
    scripts = proj / ".claude" / "scripts"
    (scripts / "detect_duplicates.py").write_text(
        "import sys\nprint('ARGV=' + repr(sys.argv[1:]))\n", encoding="utf-8",
    )
    # A qualifying interpreter: weaviate must import from it.
    #
    # DELIBERATELY UNPINNED (no `child_env()`): this probe asks the same
    # question the shipped wrapper asks on a USER's machine — "can this
    # interpreter import weaviate?" — and `child_env()` injects the repo onto
    # PYTHONPATH, which would answer a different question and let the test
    # pass on a host where the wrapper itself would refuse. Allow-listed in
    # `tests/test_v0292_fixround_child_env_lint.py::_ALLOWLIST`.
    probe = subprocess.run(
        [sys.executable, "-c", "import weaviate"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            "no interpreter with the `weaviate` client available — the "
            "wrapper's qualification probe cannot pass on this host"
        )
    fake_venv = tmp_path / "fake-venv"
    (fake_venv / "bin").mkdir(parents=True)
    # sys.executable VERBATIM — a venv's bin/python is itself a symlink and
    # venv discovery keys on argv[0]'s directory, so resolving it silently
    # swaps in the system interpreter.
    py = fake_venv / "bin" / "python"
    py.write_text(f"#!/bin/sh\nexec '{sys.executable}' \"$@\"\n", encoding="utf-8")
    py.chmod(0o755)
    env = {
        **os.environ,
        "VCT_VENV": str(fake_venv),
        "VCT_INSTALL_ROOT": "",
        "VCT_ORCHESTRATOR_ROOT": "",
    }
    proc = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(scripts / "kg-duplicates.ps1"), "-threshold", "0.90",
         "-output", "report.md"],
        capture_output=True, text=True, env=env, cwd=str(proj), timeout=120,
    )
    assert proc.returncode == 0, f"{proc.stdout!r} {proc.stderr!r}"
    assert f"ARGV={repr(['-threshold', '0.90', '-output', 'report.md'])}" \
        in proc.stdout, proc.stdout


def test_wrapper_is_shipped_by_the_bundle_globs() -> None:
    """No manifest edit may be needed: `*.ps1` must already match."""
    import fnmatch
    from vco_lib.bundle_globs import script_patterns

    patterns = script_patterns()
    for name in ("kg-duplicates", "kg-duplicates.ps1"):
        assert any(fnmatch.fnmatch(name, p) for p in patterns), (
            f"{name} would NOT be copied into .claude/scripts/"
        )
