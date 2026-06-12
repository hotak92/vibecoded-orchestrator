# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Tests for the v0.2.54 Track G boot-service unregister step of
`install.py --uninstall`.

Two layers:

1. Unit tests for `vco_lib.boot_service_cleanup` with subprocess + which
   monkeypatched and a tmp home dir — assert the right OS tool is invoked
   with the right artefact name and that on-disk unit/plist files are
   removed. NEVER touches the real user home or real systemctl/launchctl/
   schtasks.
2. A live `install.py --uninstall --dry-run` subprocess test asserting the
   printed plan now enumerates the boot-autostart step (the user-facing
   contract that closed the WINDOWS-FIRST-RUN-CHECK.md known gap).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from vco_lib import boot_service_cleanup as bsc  # noqa: E402

UNIT = "claude-mcp-containers.service"
PLIST = "com.vibecodedtools.claude-mcp-containers"
TASK = "ClaudeMcpContainers"


class _Recorder:
    """Records every command passed to _run_quiet; returns a fixed rc."""

    def __init__(self, rc: int = 0):
        self.calls: list[list[str]] = []
        self.rc = rc

    def __call__(self, cmd, timeout: int = 15):
        self.calls.append(list(cmd))
        return self.rc


# ---------------------------------------------------------------------------
# Linux arm
# ---------------------------------------------------------------------------


def test_linux_removes_unit_file_and_disables(tmp_path, monkeypatch):
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit_path = unit_dir / UNIT
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    rec = _Recorder(rc=0)
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: f"/usr/bin/{name}")

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Linux",
    )

    assert not unit_path.exists(), "unit file must be deleted"
    assert any("removed systemd user unit" in line for line in audit)
    # disable --now BEFORE the delete, daemon-reload after.
    flat = [" ".join(c) for c in rec.calls]
    assert any("disable --now " + UNIT in c for c in flat)
    assert any("daemon-reload" in c for c in flat)


def test_linux_no_unit_file_is_reported_not_fatal(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: f"/usr/bin/{name}")

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Linux",
    )
    assert any("nothing to remove" in line for line in audit)


def test_linux_without_systemctl_still_removes_unit_file(tmp_path, monkeypatch):
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / UNIT).write_text("[Unit]\n", encoding="utf-8")

    rec = _Recorder()
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: None)

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Linux",
    )
    assert not (unit_dir / UNIT).exists()
    assert rec.calls == [], "no subprocess without systemctl"
    assert any("systemctl not on PATH" in line for line in audit)


# ---------------------------------------------------------------------------
# macOS arm
# ---------------------------------------------------------------------------


def test_macos_boots_out_and_removes_plist(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist_path = agents / f"{PLIST}.plist"
    plist_path.write_text("<plist/>\n", encoding="utf-8")

    rec = _Recorder(rc=0)
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: f"/bin/{name}")

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Darwin",
    )
    assert not plist_path.exists()
    assert any("removed LaunchAgent plist" in line for line in audit)
    flat = [" ".join(c) for c in rec.calls]
    assert any("bootout" in c and str(plist_path) in c for c in flat)


def test_macos_bootout_failure_falls_back_to_unload(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist_path = agents / f"{PLIST}.plist"
    plist_path.write_text("<plist/>\n", encoding="utf-8")

    rec = _Recorder(rc=1)  # every call "fails"
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: f"/bin/{name}")

    bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Darwin",
    )
    flat = [" ".join(c) for c in rec.calls]
    assert any("unload -w" in c for c in flat), "legacy fallback must fire"
    assert not plist_path.exists(), "plist removed regardless of launchctl rc"


# ---------------------------------------------------------------------------
# Windows arm
# ---------------------------------------------------------------------------


def test_windows_deletes_scheduled_task(tmp_path, monkeypatch):
    rec = _Recorder(rc=0)
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(
        bsc.shutil, "which",
        lambda name: r"C:\Windows\System32\schtasks.exe" if name == "schtasks" else None,
    )

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Windows",
    )
    assert any(f"deleted Scheduled Task {TASK}" in line for line in audit)
    flat = [" ".join(c) for c in rec.calls]
    assert any(f"/Delete /TN {TASK} /F" in c for c in flat)


def test_windows_without_schtasks_prints_manual_command(tmp_path, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: None)

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Windows",
    )
    assert any("schtasks /Delete /TN" in line for line in audit)


# ---------------------------------------------------------------------------
# vct-hub --unregister-boot
# ---------------------------------------------------------------------------


def test_hub_unregister_uses_explicit_binary(tmp_path, monkeypatch):
    hub = tmp_path / "vct-hub"
    hub.write_text("#!/bin/sh\n", encoding="utf-8")

    rec = _Recorder(rc=0)
    monkeypatch.setattr(bsc, "_run_quiet", rec)

    audit = bsc.unregister_hub_boot_service(hub)
    assert rec.calls == [[str(hub), "--unregister-boot"]]
    assert any("completed" in line for line in audit)


def test_hub_unregister_falls_back_to_path(monkeypatch):
    rec = _Recorder(rc=0)
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(
        bsc.shutil, "which",
        lambda name: "/usr/local/bin/vct-hub" if name == "vct-hub" else None,
    )
    bsc.unregister_hub_boot_service(None)
    assert rec.calls == [["/usr/local/bin/vct-hub", "--unregister-boot"]]


def test_hub_unregister_missing_binary_is_soft(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(bsc, "_run_quiet", rec)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: None)

    audit = bsc.unregister_hub_boot_service(None)
    assert rec.calls == []
    assert any("--unregister-boot` manually" in line for line in audit)


def test_helpers_never_raise_on_subprocess_explosion(tmp_path, monkeypatch):
    def _boom(cmd, timeout=15):
        raise RuntimeError("unexpected explosion")

    monkeypatch.setattr(bsc, "_run_quiet", _boom)
    monkeypatch.setattr(bsc.shutil, "which", lambda name: f"/usr/bin/{name}")
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / UNIT).write_text("[Unit]\n", encoding="utf-8")

    audit = bsc.unregister_container_boot_service(
        UNIT, PLIST, TASK, home=tmp_path, system="Linux",
    )
    assert any("WARN" in line for line in audit), "exception surfaced as audit WARN"


# ---------------------------------------------------------------------------
# Live --uninstall --dry-run plan contract
# ---------------------------------------------------------------------------


def test_uninstall_dry_run_plan_lists_boot_service_step():
    """The printed uninstall plan must enumerate the boot-autostart removal
    (the user-facing contract that closed the v0.2.54 known gap documented
    in docs/post-install/WINDOWS-FIRST-RUN-CHECK.md). Dry-run removes
    nothing, so this is safe to run on any machine."""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "install.py"), "--uninstall", "--dry-run"],
        capture_output=True, text=True, timeout=110, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout
    assert "Remove boot autostart" in out, out[-2000:]
    assert "vct-hub --unregister-boot" in out, out[-2000:]
    assert "Dry-run mode" in out
