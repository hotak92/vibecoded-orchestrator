# SPDX-License-Identifier: AGPL-3.0-or-later
"""v0.2.95 WP-8 — ``--no-auto-launch`` must not suppress the desktop entry.

The defect this pins, in full:

``scripts/post-install-launcher.sh`` gained its ``--no-auto-launch`` early
``exit 0`` on 2026-04-27 (``3c5539dc``). The desktop-shortcut step was
appended to the **end** of the same file one day later (``9d48c080``,
2026-04-28) — i.e. *after* that ``exit 0``. From then on the flag silently
suppressed the shortcut as well as the GUI spawn.

That matters because of who passes the flag:
``install.py::_run_desktop_icon_step`` — the function whose entire purpose is
creating the desktop icon (v0.2.6 "Bug C1": *"Direct `python install.py` runs
previously skipped the icon-creation step entirely"*) — invokes the helper as

    bash scripts/post-install-launcher.sh <root> --yes --no-auto-launch

*always*, "because install.py exits next anyway and we don't want a duplicate
launcher spawn". So on Linux/macOS that step could never actually create an
icon, and every install driven by ``install.sh`` (which relies solely on
install.py for this step) finished with a launcher binary but no desktop
entry.

The ``.ps1`` sibling never had the bug — it writes the shortcuts and *then*
checks ``-NoAutoLaunch`` — and its header states the intended contract
outright: *"install.py always passes this (it exits right after; **the user
opens the launcher from the shortcut**)"*. The shortcut is presupposed.

Isolation: a scratch repo root under ``tmp_path`` holding a stub launcher
binary, ``HOME`` redirected into ``tmp_path``. Nothing is downloaded or
built (a binary is already present, so the download/build ladder is never
reached) and the real user's ``~/.local/share/applications`` is untouched.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="post-install-launcher.sh is the POSIX helper; .ps1 is its sibling",
)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build a scratch install root + HOME. Returns (root, home, spawn_marker).

    The stub launcher binary records the fact that it was executed, which is
    how the "still does not spawn the GUI" half of the contract is observed.

    The stub is placed at the *locally built* candidate path rather than
    under ``launcher/dist/``: bundled binaries go through
    ``_bundled_binary_is_fresh``, which rejects anything without a
    ``.metadata.json`` sidecar, and a rejected binary sends the helper down
    the download/build ladder — exactly what must never run in a test.
    """
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(
        REPO_ROOT / "scripts" / "post-install-launcher.sh",
        root / "scripts" / "post-install-launcher.sh",
    )

    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)

    spawn_marker = tmp_path / "spawned.txt"
    bin_path = root / "launcher" / "src-tauri" / "target" / "release" / "vct-launcher"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text(
        f"#!/bin/sh\nprintf 'spawned\\n' >> '{spawn_marker}'\nexit 0\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)

    # Belt-and-braces isolation: even if some future edit reached an install
    # or download path, these deny-stubs shadow the real tools so nothing can
    # touch the network or the system package set.
    deny = tmp_path / "deny"
    deny.mkdir()
    for tool in ("sudo", "curl", "wget", "apt-get", "dnf", "pacman", "brew", "pkexec"):
        stub = deny / tool
        stub.write_text(
            f"#!/bin/sh\necho 'REFUSED: {tool} must not run in tests' >&2\nexit 1\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)

    return root, home, spawn_marker


def _run_helper(root: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    deny = root.parent / "deny"
    env = {
        "PATH": f"{deny}:/usr/bin:/bin",
        "HOME": str(home),
        "CI": "1",
    }
    return subprocess.run(
        ["bash", "scripts/post-install-launcher.sh", str(root), *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_no_auto_launch_still_writes_the_desktop_entry(tmp_path):
    """The exact argv install.py uses must still produce a desktop entry.

    This is the assertion that goes red if the ``--no-auto-launch`` gate is
    moved back above the desktop-shortcut step.
    """
    root, home, _ = _sandbox(tmp_path)
    res = _run_helper(root, home, "--yes", "--no-auto-launch")

    desktop_entry = home / ".local" / "share" / "applications" / "vct-launcher.desktop"
    assert desktop_entry.is_file(), (
        "post-install-launcher.sh --no-auto-launch produced no desktop entry at "
        f"{desktop_entry}.\n"
        "`--no-auto-launch` means 'skip the detached GUI spawn' — it must not "
        "also suppress the icon, because install.py's desktop-icon step always "
        "passes it.\n"
        f"--- helper output ---\n{res.stdout}\n{res.stderr}"
    )
    body = desktop_entry.read_text(encoding="utf-8")
    assert "Name=VCT Launcher" in body, f"malformed desktop entry:\n{body}"
    assert "vct-launcher" in body


def test_no_auto_launch_still_does_not_spawn_the_gui(tmp_path):
    """The flag must keep doing its one job: no detached launcher process."""
    root, home, spawn_marker = _sandbox(tmp_path)
    _run_helper(root, home, "--yes", "--no-auto-launch")

    # Give any (incorrectly) detached spawn a chance to appear.
    time.sleep(1.0)
    assert not spawn_marker.exists(), (
        "--no-auto-launch spawned the launcher binary anyway; the flag must "
        "still suppress the GUI spawn."
    )


def test_desktop_entry_is_also_written_on_the_normal_auto_launch_path(tmp_path):
    """Regression guard: moving the gate must not cost the normal path its icon."""
    root, home, _ = _sandbox(tmp_path)
    res = _run_helper(root, home, "--yes")

    desktop_entry = home / ".local" / "share" / "applications" / "vct-launcher.desktop"
    assert desktop_entry.is_file(), (
        "the ordinary (auto-launch) path lost its desktop entry.\n"
        f"--- helper output ---\n{res.stdout}\n{res.stderr}"
    )


def test_desktop_icon_opt_out_still_works_under_no_auto_launch(tmp_path):
    """``VCT_NO_DESKTOP_ICON=1`` remains the way to decline the icon.

    The icon has its own opt-out; that is precisely why ``--no-auto-launch``
    must not double as one.
    """
    root, home, _ = _sandbox(tmp_path)
    env = {
        "PATH": f"{tmp_path / 'deny'}:/usr/bin:/bin",
        "HOME": str(home),
        "CI": "1",
        "VCT_NO_DESKTOP_ICON": "1",
    }
    subprocess.run(
        [
            "bash",
            "scripts/post-install-launcher.sh",
            str(root),
            "--yes",
            "--no-auto-launch",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    desktop_entry = home / ".local" / "share" / "applications" / "vct-launcher.desktop"
    assert not desktop_entry.exists(), (
        "VCT_NO_DESKTOP_ICON=1 must still suppress the desktop entry"
    )


def test_install_py_still_passes_no_auto_launch_to_the_helper():
    """Premise pin, not the parity proof.

    The behavioural tests above matter only while install.py's desktop-icon
    step really does pass ``--no-auto-launch``. If that argv ever changes,
    this fails and sends the reader back to re-read why the gate sits where
    it sits.
    """
    src = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
    start = src.index("def _run_desktop_icon_step")
    body = src[start : start + 4000]
    posix_arm = body[body.index('cmd = [\n            "bash"') :]
    head = posix_arm[:400]
    assert '"--no-auto-launch"' in head, (
        "install.py::_run_desktop_icon_step no longer passes --no-auto-launch "
        "to post-install-launcher.sh. Re-check WP-8: the gate's placement was "
        "chosen because this caller always passes the flag."
    )
    assert '"--yes"' in head, (
        "install.py::_run_desktop_icon_step no longer passes --yes; the helper "
        "would then prompt inside a non-interactive install."
    )
