# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""embedding-failures-surface.{sh,ps1} — the fidelity leg cannot fail silently.

v0.2.92 MAJOR-3. The SessionStart fidelity leg spawns
``python -m vco_lib.embedding_fidelity notice``. That spawn used to end in
``2>/dev/null || true`` (``.sh``) / ``2>$null`` inside a swallowing ``catch``
(``.ps1``). On a machine in the documented shadow-copy state — a stale,
non-editable ``vco_lib`` in ``site-packages``, or a venv predating this module
— the spawn dies with ``No module named vco_lib.embedding_fidelity``, the
byte-offset marker is never advanced, and the ONE surface built to report
embedding-fidelity loss goes quiet **permanently**, with nothing anywhere
saying so. That is not "soft-fail"; it is an undiagnosable outage of a
reporting surface, and it is the inverse of the standing loud-fail rule for
``vco_lib`` imports.

The chosen visibility mechanism is a short diagnostic on **stdout**. Claude
Code injects a SessionStart hook's stdout as a system-reminder and discards its
stderr on exit 0, so stderr would have been exactly as invisible as nothing.
The hook's exit code is untouched — a hook still never breaks a session.

These tests drive the real hooks. The ``.ps1`` leg had only a source-scan
guard before this file (asserting the module name appears in the body), which
a comment satisfies; here it is executed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HOOK_SH = REPO / "templates" / "hooks" / "embedding-failures-surface.sh"
HOOK_PS1 = REPO / "templates" / "hooks" / "embedding-failures-surface.ps1"

DIAGNOSTIC_MARKER = "[embedding-failures-surface] the embedding-fidelity notice could not run"
NOTICE_MARKER = "Embedding fidelity note (NOT an outage)"

SHRINK_ROW = json.dumps({
    "kind": "shrink_summary",
    "timestamp": "2026-09-05T00:00:00+00:00",
    "shrinks": {"qwen3-embedding:0.6b": {
        "count": 3, "orig_chars": 30000, "sent_chars": 15000}},
    "floor_refusals": {},
}) + "\n"

#: What a missing module actually looks like on stderr — the field shape.
BROKEN_IMPORT_MESSAGE = "No module named vco_lib.embedding_fidelity"


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


def _stage(tmp_path: Path, *, rows: str = SHRINK_ROW) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    (project / ".claude" / "state").mkdir(parents=True)
    state = tmp_path / "vct"
    (state / "metrics").mkdir(parents=True)
    (state / "metrics" / "embedding_failures.jsonl").write_text(rows, encoding="utf-8")
    return project, state


def _working_venv(tmp_path: Path) -> Path:
    """A ``$VCT_VENV`` whose python is this interpreter with the repo on
    ``PYTHONPATH`` — so the module under test is THIS checkout's, never a
    shadow copy in some other venv's site-packages."""
    venv = tmp_path / "venv-ok"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    shim = venv / "bin" / "python"
    shim.write_text(
        "#!/bin/sh\n"
        f'PYTHONPATH="{REPO}${{PYTHONPATH:+:$PYTHONPATH}}" exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return venv


def _broken_venv(tmp_path: Path) -> Path:
    """A ``$VCT_VENV`` whose python fails exactly the way the shadow-copy
    state fails: exit 1, one line on stderr, nothing on stdout."""
    venv = tmp_path / "venv-broken"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    shim = venv / "bin" / "python"
    shim.write_text(
        "#!/bin/sh\n"
        f'echo "/usr/bin/python: {BROKEN_IMPORT_MESSAGE}" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return venv


def _isolated_hooks_dir(tmp_path: Path) -> Path:
    """The hooks copied to a directory whose 2-up parent is NOT a VCO clone.

    ``resolve_vco_venv_python``'s last tiers probe ``<hooks>/../../.venv`` and
    only accept it when that root holds ``install.py`` + ``first-install.sh``.
    Running the shipped copy out of ``templates/hooks/`` would therefore find
    the orchestrator's own venv, and "no interpreter resolved" would be
    unreachable. This mirrors the installed layout (``<project>/.claude/hooks``)
    where that discriminator legitimately fails.
    """
    hooks = tmp_path / "installed" / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copytree(HOOK_SH.parent / "_lib", hooks / "_lib")
    for hook in (HOOK_SH, HOOK_PS1):
        shutil.copyfile(hook, hooks / hook.name)
    return hooks


def _env(project: Path, state: Path, venv: Path, tmp_path: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "CLAUDE_PROJECT_DIR": str(project),
        "VCT_STATE_DIR": str(state),
        "VCT_CLAUDE_DIR": str(tmp_path / "claude_home"),
        "VCT_VENV": str(venv),
    })
    env.pop("VCT_DISABLE_HOOKS", None)
    return env


def _run_sh(project, state, venv, tmp_path):
    return subprocess.run(
        ["bash", str(HOOK_SH)],
        env=_env(project, state, venv, tmp_path),
        capture_output=True, text=True, timeout=60,
    )


def _run_ps1(exe, project, state, venv, tmp_path):
    return subprocess.run(
        [exe, "-NoProfile", "-File", str(HOOK_PS1)],
        env=_env(project, state, venv, tmp_path),
        capture_output=True, text=True, timeout=120,
    )


# --------------------------------------------------------------------------- #
# .sh
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(sys.platform == "win32", reason="bash hooks are POSIX-only")
def test_sh_a_broken_interpreter_is_reported_not_swallowed(tmp_path):
    project, state = _stage(tmp_path)
    result = _run_sh(project, state, _broken_venv(tmp_path), tmp_path)

    assert result.returncode == 0, "a hook must never break the session"
    assert DIAGNOSTIC_MARKER in result.stdout, (
        "the fidelity leg failed and said nothing — the surface is now "
        f"silently dead.\nstdout={result.stdout!r}\nstderr={result.stderr!r}")
    assert BROKEN_IMPORT_MESSAGE in result.stdout, (
        "the diagnostic does not name the actual error, so it cannot be acted "
        f"on: {result.stdout!r}")
    assert "install.py --update" in result.stdout, "no remedy named"


@pytest.mark.skipif(sys.platform == "win32", reason="bash hooks are POSIX-only")
def test_sh_the_report_is_not_one_shot(tmp_path):
    """The failure state persists, so the report must too.

    A diagnostic that fires once and then stays quiet reproduces the very
    defect: the surface is dead, and after one session nothing says so.
    """
    project, state = _stage(tmp_path)
    venv = _broken_venv(tmp_path)
    first = _run_sh(project, state, venv, tmp_path)
    second = _run_sh(project, state, venv, tmp_path)
    assert DIAGNOSTIC_MARKER in first.stdout
    assert DIAGNOSTIC_MARKER in second.stdout, (
        "the second session was silent about a still-broken surface: "
        f"{second.stdout!r}")


@pytest.mark.skipif(sys.platform == "win32", reason="bash hooks are POSIX-only")
def test_sh_a_working_interpreter_prints_the_notice_and_no_diagnostic(tmp_path):
    """The decision, not just the act: the diagnostic is conditional.

    A version that printed the warning unconditionally would satisfy the two
    tests above while crying wolf on every healthy session.
    """
    project, state = _stage(tmp_path)
    result = _run_sh(project, state, _working_venv(tmp_path), tmp_path)

    assert result.returncode == 0, result.stderr
    assert NOTICE_MARKER in result.stdout, (
        f"the notice did not reach stdout: {result.stdout!r}")
    assert DIAGNOSTIC_MARKER not in result.stdout, (
        f"a healthy run reported a failure: {result.stdout!r}")


@pytest.mark.skipif(sys.platform == "win32", reason="bash hooks are POSIX-only")
def test_sh_an_unresolvable_venv_is_reported_too(tmp_path):
    """The other way this leg went permanently quiet.

    ``resolve_vco_venv_python`` returning empty used to hit an empty ``else``
    — no notice, no explanation, indistinguishable from "nothing to report".
    Rows are owed and cannot be rendered; the hook has to say so.
    """
    project, state = _stage(tmp_path)
    hooks = _isolated_hooks_dir(tmp_path)
    env = _env(project, state, tmp_path / "no-such-venv", tmp_path)
    env.pop("VCT_INSTALL_ROOT", None)
    result = subprocess.run(
        ["bash", str(hooks / HOOK_SH.name)],
        env=env, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0
    assert DIAGNOSTIC_MARKER in result.stdout, (
        f"an unresolvable venv silently produced no notice: {result.stdout!r}")
    assert "no VCO venv resolved" in result.stdout


# --------------------------------------------------------------------------- #
# .ps1 — driven, not scanned (R42)
# --------------------------------------------------------------------------- #


def test_ps1_reaches_the_notice(tmp_path):
    """The Windows leg's wiring, executed.

    Before this, the only ``.ps1`` coverage asserted that the string
    ``vco_lib.embedding_fidelity notice`` appears in the body — which the
    file's own explanatory comment satisfies. Here the hook runs and the
    notice has to come out of it.
    """
    exe = _require_pwsh()
    project, state = _stage(tmp_path)
    result = _run_ps1(exe, project, state, _working_venv(tmp_path), tmp_path)

    assert result.returncode == 0, result.stderr
    assert NOTICE_MARKER in result.stdout, (
        f"the .ps1 fidelity leg produced no notice.\nstdout={result.stdout!r}\n"
        f"stderr={result.stderr!r}")
    assert "qwen3-embedding:0.6b" in result.stdout


def test_ps1_a_broken_interpreter_is_reported_not_swallowed(tmp_path):
    exe = _require_pwsh()
    project, state = _stage(tmp_path)
    result = _run_ps1(exe, project, state, _broken_venv(tmp_path), tmp_path)

    assert result.returncode == 0, "a hook must never break the session"
    assert DIAGNOSTIC_MARKER in result.stdout, (
        "the .ps1 fidelity leg failed silently.\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}")
    assert BROKEN_IMPORT_MESSAGE in result.stdout


def test_ps1_a_working_interpreter_prints_no_diagnostic(tmp_path):
    exe = _require_pwsh()
    project, state = _stage(tmp_path)
    result = _run_ps1(exe, project, state, _working_venv(tmp_path), tmp_path)
    assert DIAGNOSTIC_MARKER not in result.stdout, (
        f"a healthy run reported a failure: {result.stdout!r}")


def test_ps1_an_unresolvable_venv_is_reported_too(tmp_path):
    exe = _require_pwsh()
    project, state = _stage(tmp_path)
    hooks = _isolated_hooks_dir(tmp_path)
    env = _env(project, state, tmp_path / "no-such-venv", tmp_path)
    env.pop("VCT_INSTALL_ROOT", None)
    result = subprocess.run(
        [exe, "-NoProfile", "-File", str(hooks / HOOK_PS1.name)],
        env=env, capture_output=True, text=True, timeout=120)

    assert result.returncode == 0
    assert DIAGNOSTIC_MARKER in result.stdout, (
        f"an unresolvable venv silently produced no notice: {result.stdout!r}")
    assert "no VCO venv resolved" in result.stdout
