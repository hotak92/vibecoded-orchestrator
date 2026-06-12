# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Boot-service unregister helpers for the uninstaller (v0.2.54 Track G).

install.py materializes TWO classes of boot-time autostart entries:

1. **Container-stack boot service** (``_materialize_boot_service``),
   registered on every default install (skipped only with
   ``--no-containers`` / ``VCT_DISABLE_BOOT_SERVICE=1``):

   - Linux:   systemd user unit at
     ``~/.config/systemd/user/claude-mcp-containers.service``
   - macOS:   LaunchAgent at
     ``~/Library/LaunchAgents/com.vibecodedtools.claude-mcp-containers.plist``
   - Windows: Scheduled Task named ``ClaudeMcpContainers``

2. **vct-hub boot service** (``vct-hub --register-boot``), opt-in via the
   launcher GUI Preferences page. Its own idempotent inverse is
   ``vct-hub --unregister-boot`` (see
   ``launcher/src-tauri/vct-hub/src/boot.rs``).

Before v0.2.54, ``install.py --uninstall`` removed neither. After the user
deleted the clone, all three OSes kept invoking
``scripts/launch-claude-mcp-stack.*`` from a deleted path at every
boot/logon — forever, silently. This module closes that gap.

Design contract (mirrors the materializers' soft-fail discipline):
every function returns a list of human-readable audit lines and NEVER
raises — a failed unregister is reported, not fatal. The uninstaller
appends the lines to its audit log.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional


def _run_quiet(cmd: list[str], timeout: int = 15) -> Optional[int]:
    """Run a command, swallowing output. Returns the exit code, or None
    if the command could not be spawned / timed out."""
    try:
        proc = subprocess.run(
            cmd, check=False, capture_output=True, timeout=timeout,
        )
        return proc.returncode
    except (OSError, subprocess.TimeoutExpired):
        return None


def unregister_container_boot_service(
    unit_name: str,
    plist_label: str,
    task_name: str,
    home: Optional[Path] = None,
    system: Optional[str] = None,
) -> list[str]:
    """Remove the container-stack boot autostart entry for the host OS.

    Inverse of install.py's ``_materialize_boot_service_{linux,macos,windows}``:

    - Linux:   ``systemctl --user disable --now <unit>`` + delete the unit
      file + ``daemon-reload``. Failures of the systemctl calls are
      tolerated (unit may never have been enabled / systemctl absent);
      the unit-file delete is what stops the boot retry loop.
    - macOS:   ``launchctl bootout gui/<uid> <plist>`` (modern) with
      ``launchctl unload -w <plist>`` legacy fallback + delete the plist.
    - Windows: ``schtasks /Delete /TN <task> /F``. The rendered task XML
      lives inside the install tree (``<install>/state/``) and goes away
      with the clone — no separate file delete needed here.

    Args:
        unit_name:   systemd unit filename (``claude-mcp-containers.service``).
        plist_label: launchd label (``com.vibecodedtools.claude-mcp-containers``).
        task_name:   Scheduled Task name (``ClaudeMcpContainers``).
        home:        Home-dir override for tests; defaults to ``Path.home()``.
        system:      ``platform.system()`` override for tests.

    Returns:
        Audit lines describing what was removed / skipped. Never raises.
    """
    audit: list[str] = []
    home = home or Path.home()
    os_name = system or platform.system()

    try:
        if os_name == "Linux":
            systemctl = shutil.which("systemctl")
            if systemctl:
                # Tolerate non-zero: unit may never have been enabled.
                _run_quiet([systemctl, "--user", "disable", "--now", unit_name])
            unit_path = home / ".config" / "systemd" / "user" / unit_name
            if unit_path.exists():
                try:
                    unit_path.unlink()
                    audit.append(f"removed systemd user unit {unit_path}")
                except OSError as e:
                    audit.append(
                        f"WARN: could not remove systemd unit {unit_path}: {e}"
                    )
            else:
                audit.append(f"no systemd user unit at {unit_path} (nothing to remove)")
            if systemctl:
                _run_quiet([systemctl, "--user", "daemon-reload"])
            else:
                audit.append(
                    "WARN: systemctl not on PATH — unit file removed (if present) "
                    "but disable/daemon-reload skipped"
                )

        elif os_name == "Darwin":
            plist_path = (
                home / "Library" / "LaunchAgents" / f"{plist_label}.plist"
            )
            launchctl = shutil.which("launchctl")
            if launchctl and plist_path.exists():
                uid = os.getuid() if hasattr(os, "getuid") else 0
                rc = _run_quiet([launchctl, "bootout", f"gui/{uid}", str(plist_path)])
                if rc != 0:
                    # Legacy fallback (pre-10.10 syntax; also covers agents
                    # loaded with `load -w` rather than `bootstrap`).
                    _run_quiet([launchctl, "unload", "-w", str(plist_path)])
            if plist_path.exists():
                try:
                    plist_path.unlink()
                    audit.append(f"removed LaunchAgent plist {plist_path}")
                except OSError as e:
                    audit.append(
                        f"WARN: could not remove LaunchAgent plist {plist_path}: {e}"
                    )
            else:
                audit.append(
                    f"no LaunchAgent plist at {plist_path} (nothing to remove)"
                )

        elif os_name == "Windows":
            schtasks = shutil.which("schtasks")
            if schtasks:
                rc = _run_quiet([schtasks, "/Delete", "/TN", task_name, "/F"])
                if rc == 0:
                    audit.append(f"deleted Scheduled Task {task_name}")
                else:
                    # Non-zero usually means the task doesn't exist —
                    # report either way so the audit log is explicit.
                    audit.append(
                        f"Scheduled Task {task_name} not removed "
                        f"(schtasks rc={rc}; task may not exist)"
                    )
            else:
                audit.append(
                    f"WARN: schtasks not on PATH — Scheduled Task {task_name} "
                    f"not removed; run `schtasks /Delete /TN {task_name} /F` manually"
                )
        else:
            audit.append(
                f"boot-service removal skipped: unsupported OS {os_name}"
            )
    except Exception as e:  # noqa: BLE001 — uninstall must never crash here
        audit.append(
            f"WARN: container boot-service removal raised "
            f"{type(e).__name__}: {e}"
        )
    return audit


def unregister_hub_boot_service(
    hub_binary: Optional[Path] = None,
) -> list[str]:
    """Best-effort ``vct-hub --unregister-boot``.

    The hub's boot autostart is opt-in (launcher GUI Preferences), so this
    is frequently a no-op — ``--unregister-boot`` is idempotent on the hub
    side (unregistering a never-registered unit succeeds).

    Args:
        hub_binary: explicit path to the vct-hub binary (the uninstaller
            resolves the bundled one from ``launcher/dist/<os-arch>/``).
            Falls back to ``vct-hub`` on PATH when None / missing.

    Returns:
        Audit lines. Never raises.
    """
    audit: list[str] = []
    candidate: Optional[str] = None
    if hub_binary is not None and Path(hub_binary).exists():
        candidate = str(hub_binary)
    else:
        candidate = shutil.which("vct-hub")

    if not candidate:
        audit.append(
            "vct-hub binary not found (bundled or on PATH) — hub boot "
            "autostart not unregistered; if you enabled it in the launcher "
            "Preferences, run `vct-hub --unregister-boot` manually"
        )
        return audit

    rc = _run_quiet([candidate, "--unregister-boot"], timeout=30)
    if rc == 0:
        audit.append("vct-hub --unregister-boot completed (idempotent)")
    else:
        audit.append(
            f"WARN: `{candidate} --unregister-boot` "
            + ("could not be spawned / timed out" if rc is None else f"exited {rc}")
        )
    return audit
