#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""Cross-platform desktop notification helper.

Replaces direct `notify-send` calls in hooks with a portable dispatcher:
    Linux   → notify-send (libnotify)
    macOS   → osascript display notification
    Windows → PowerShell New-BurntToastNotification (if module present),
              else PowerShell System.Windows.Forms balloon, else silent.

Hooks invoke this as:

    "$PY" "$PROJECT_DIR/.claude/scripts/notify.py" "Title" "Message" \
        [--urgency low|normal|critical] [--icon dialog-information|dialog-error|...] \
        [--expire-time MS]

All flags are optional and degrade gracefully on platforms that don't
honor them. Always exits 0 — failure to notify is never fatal to the
hook that called it. See VCO portability audit 2026-04-30, finding F2.
"""
from __future__ import annotations

import argparse
import platform
import shlex
import shutil
import subprocess
import sys
from typing import Sequence


_URGENCIES = {"low", "normal", "critical"}


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="notify",
        description="Cross-platform desktop notification (Linux/macOS/Windows).",
        add_help=True,
    )
    p.add_argument("title", help="Notification title (short).")
    p.add_argument("message", help="Notification body.")
    p.add_argument(
        "--urgency",
        choices=sorted(_URGENCIES),
        default="normal",
        help="low|normal|critical (Linux honors, others approximate).",
    )
    p.add_argument(
        "--icon",
        default="",
        help="Linux freedesktop icon name (e.g. dialog-information). Ignored elsewhere.",
    )
    p.add_argument(
        "--expire-time",
        type=int,
        default=0,
        help="Milliseconds until auto-dismiss (Linux only; 0 = system default).",
    )
    return p.parse_args(argv)


def _notify_linux(args: argparse.Namespace) -> int:
    """Use notify-send. Silent no-op if libnotify isn't installed."""
    if not shutil.which("notify-send"):
        return 0
    cmd = ["notify-send"]
    if args.icon:
        cmd += [f"--icon={args.icon}"]
    if args.expire_time > 0:
        cmd += [f"--expire-time={args.expire_time}"]
    cmd += [f"--urgency={args.urgency}", args.title, args.message]
    try:
        subprocess.run(cmd, check=False, timeout=5)
    except (subprocess.SubprocessError, OSError):
        pass
    return 0


def _notify_macos(args: argparse.Namespace) -> int:
    """osascript fires the standard macOS Notification Center toast.

    AppleScript single-quotes its strings; we escape any embedded
    double quotes by replacing with `'\"'`-style sequences via shlex.quote
    is not appropriate for AppleScript syntax, so we just strip control
    chars and inline.
    """
    if not shutil.which("osascript"):
        return 0

    def _esc(s: str) -> str:
        # AppleScript string: escape backslashes and double quotes.
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = f'display notification "{_esc(args.message)}" with title "{_esc(args.title)}"'
    try:
        subprocess.run(
            ["osascript", "-e", script], check=False, timeout=5
        )
    except (subprocess.SubprocessError, OSError):
        pass
    return 0


def _notify_windows(args: argparse.Namespace) -> int:
    """Try BurntToast first (modern toast), fall back to a Forms balloon.

    BurntToast is a community PowerShell module; many users won't have it.
    The Forms fallback works on every supported Windows version. If
    PowerShell itself isn't on PATH (rare), we silently no-op.
    """
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return 0

    title = args.title.replace("'", "''")
    message = args.message.replace("'", "''")

    # First attempt: BurntToast (silent if module missing).
    burnt_ps = (
        "if (Get-Module -ListAvailable -Name BurntToast) { "
        f"  Import-Module BurntToast; "
        f"  New-BurntToastNotification -Text '{title}', '{message}' "
        "}"
    )
    try:
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", burnt_ps],
            check=False,
            timeout=8,
            capture_output=True,
        )
        if result.returncode == 0:
            return 0
    except (subprocess.SubprocessError, OSError):
        pass

    # Fallback: System.Windows.Forms.NotifyIcon balloon.
    icon = "Information"
    if args.urgency == "critical":
        icon = "Error"
    elif args.urgency == "low":
        icon = "Info"
    timeout_ms = args.expire_time if args.expire_time > 0 else 5000
    forms_ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip({timeout_ms}, '{title}', '{message}', "
        f"[System.Windows.Forms.ToolTipIcon]::{icon}); "
        # Brief sleep so the balloon actually renders before NotifyIcon disposes.
        f"Start-Sleep -Milliseconds {min(timeout_ms, 3000)}; "
        "$n.Dispose()"
    )
    try:
        subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", forms_ps],
            check=False,
            timeout=10,
            capture_output=True,
        )
    except (subprocess.SubprocessError, OSError):
        pass
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(list(argv) if argv is not None else sys.argv[1:])
    system = platform.system()
    if system == "Linux":
        return _notify_linux(args)
    if system == "Darwin":
        return _notify_macos(args)
    if system == "Windows":
        return _notify_windows(args)
    # Unknown OS — silent.
    return 0


if __name__ == "__main__":
    # Always exit 0 — we never want notification failures to break a hook.
    try:
        main()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0)
