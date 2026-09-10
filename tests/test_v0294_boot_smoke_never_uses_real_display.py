# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""The launcher boot smoke never opens a window on the operator's live desktop.

Incident 2026-09-09 (twice, 06:27 and 20:19 CEST): `xvfb-run` was not
installed on the maintainer's machine, the smoke fell back to the REAL
`DISPLAY` "with a note", the launcher window flashed onto a live GNOME/X11
session (mutter 46.2, NVIDIA) and gnome-shell died on
``meta_window_get_work_area_for_logical_monitor: assertion failed
(logical_monitor)`` — the whole desktop session ended, every GUI app with it,
and the operator found the "session failed" lockdown screen hours later.
CI never saw it: release.yml installs xvfb first.

Rule pinned here, DRIVEN through the real script with a stub launcher:

* no xvfb-run + a real display + no opt-in  → exit 3, refusal names the fix,
  the launcher binary is NEVER executed;
* the explicit opt-in ``VCT_BOOT_SMOKE_REAL_DISPLAY=1`` runs it (with a
  warning) — the operator's deliberate choice, not a default;
* a pinned ``VCT_BOOT_SMOKE_XVFB_RUN=<path>`` is used as the runner.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SMOKE = REPO / "scripts" / "launcher-boot-smoke.sh"
MARKER = "[vct] setup complete"


def _bash() -> str:
    exe = shutil.which("bash")
    if exe is None:  # pragma: no cover - POSIX hosts always have bash
        pytest.skip("no bash on this machine")
    return exe


def _stub_launcher(tmp_path: Path) -> Path:
    """A 'launcher' that prints the boot marker and leaves a footprint."""
    binary = tmp_path / "bin" / "vct-launcher"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        "#!/bin/sh\n"
        f"echo RAN > '{tmp_path}/ran.txt'\n"
        f"echo '{MARKER}'\n"
        "sleep 5\n",
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _minimal_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """PATH with only coreutils dirs (no xvfb-run), a fake display, no opt-in."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "DISPLAY": ":99",
        # Probing is disabled so a machine that DOES have /usr/bin/xvfb-run
        # (CI installs it) exercises the same branch as one that does not.
        "VCT_BOOT_SMOKE_XVFB_RUN": "none",
    }
    env.update(extra)
    return env


def _run(tmp_path: Path, binary: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_bash(), str(SMOKE), str(binary), "20"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=120,
    )


def test_no_xvfb_and_a_real_display_refuses_without_running_the_launcher(tmp_path):
    binary = _stub_launcher(tmp_path)
    proc = _run(tmp_path, binary, _minimal_env(tmp_path))
    assert proc.returncode == 3, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "REFUSING to open the launcher window" in proc.stderr
    assert "sudo apt install xvfb" in proc.stderr
    assert "VCT_BOOT_SMOKE_REAL_DISPLAY=1" in proc.stderr
    assert not (tmp_path / "ran.txt").exists(), (
        "the launcher binary was executed on the real display despite the refusal"
    )


def test_the_explicit_opt_in_runs_it_and_says_so(tmp_path):
    binary = _stub_launcher(tmp_path)
    proc = _run(tmp_path, binary, _minimal_env(tmp_path, VCT_BOOT_SMOKE_REAL_DISPLAY="1"))
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "WARNING: VCT_BOOT_SMOKE_REAL_DISPLAY=1" in proc.stderr
    assert (tmp_path / "ran.txt").exists()
    assert "PASS: setup complete" in proc.stdout


def test_a_pinned_xvfb_run_is_used_as_the_runner(tmp_path):
    binary = _stub_launcher(tmp_path)
    fake_xvfb = tmp_path / "fake-xvfb-run"
    fake_xvfb.write_text(
        "#!/bin/sh\n"
        f"echo XVFB > '{tmp_path}/xvfb.txt'\n"
        '# xvfb-run signature: xvfb-run --auto-servernum <cmd...>\n'
        'shift\nexec "$@"\n',
        encoding="utf-8",
    )
    fake_xvfb.chmod(fake_xvfb.stat().st_mode | stat.S_IXUSR)
    proc = _run(tmp_path, binary, _minimal_env(tmp_path, VCT_BOOT_SMOKE_XVFB_RUN=str(fake_xvfb)))
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert (tmp_path / "xvfb.txt").exists(), "the pinned xvfb-run was not used"
    assert (tmp_path / "ran.txt").exists()


def test_no_display_at_all_still_fails_with_the_install_hint(tmp_path):
    binary = _stub_launcher(tmp_path)
    env = _minimal_env(tmp_path)
    env.pop("DISPLAY")
    proc = _run(tmp_path, binary, env)
    assert proc.returncode == 3
    assert "install xvfb" in proc.stderr
    assert not (tmp_path / "ran.txt").exists()


def test_check_display_mode_reports_the_rule_without_a_binary(tmp_path):
    """`--check-display` = the display rule alone, no binary needed.

    The pre-ship gate calls it before its cargo leg so a missing xvfb fails in
    seconds. Same three outcomes as the real run: refused (3), pinned runner (0),
    explicit opt-in (0) — and nothing is executed in any of them.
    """
    base = _minimal_env(tmp_path)
    refused = subprocess.run([_bash(), str(SMOKE), "--check-display"],
                             capture_output=True, text=True, env=base, cwd=str(tmp_path), timeout=60)
    assert refused.returncode == 3 and "REFUSING" in refused.stderr

    fake_xvfb = tmp_path / "fake-xvfb-run"
    fake_xvfb.write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    fake_xvfb.chmod(fake_xvfb.stat().st_mode | stat.S_IXUSR)
    pinned = subprocess.run([_bash(), str(SMOKE), "--check-display"], capture_output=True, text=True,
                            env=_minimal_env(tmp_path, VCT_BOOT_SMOKE_XVFB_RUN=str(fake_xvfb)),
                            cwd=str(tmp_path), timeout=60)
    assert pinned.returncode == 0 and "headless via" in pinned.stdout

    opted = subprocess.run([_bash(), str(SMOKE), "--check-display"], capture_output=True, text=True,
                           env=_minimal_env(tmp_path, VCT_BOOT_SMOKE_REAL_DISPLAY="1"),
                           cwd=str(tmp_path), timeout=60)
    assert opted.returncode == 0 and "explicit opt-in" in opted.stdout
