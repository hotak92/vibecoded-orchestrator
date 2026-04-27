#!/usr/bin/env bash
# first-install.sh — VibeCoded Tools first-time installer (Linux)
#
# DOUBLE-CLICKABLE OR TERMINAL ENTRY POINT. This is what a brand-new
# user runs once. Carries zero pre-install dependencies beyond bash
# itself (which ships with every Linux distro and macOS). It will:
#
#   1. Check we have bash + a terminal we can prompt in.
#   2. Detect & install Python 3.11+ (delegates to install.sh which
#      handles apt/dnf/pacman + brew + python.org URL fallback).
#   3. Detect & install a container runtime (Podman/Docker) — install.py
#      prompts for this; pkexec elevates on Linux.
#   4. Run install.py (creates venv, installs deps, brings services up).
#   5. (post-v1.0) Build/launch the Tauri launcher.
#
# Status: STUB. Currently delegates everything to install.sh which already
# handles steps 1-4. Steps 5+ (Tauri build + first-launch GUI) tracked in
# .claude/context/plans/first-install-entry-points.md.
#
# Why a stub for now: install.sh + install.py already do the heavy
# lifting (Python detection + auto-install, container-runtime prompt,
# GPU detection, venv, services, .env). All this script adds is a more
# discoverable filename so a user landing on the GitHub repo sees
# "first-install.sh" and knows it's the right thing to run.
#
# Discoverability path: README points users to download/extract the
# repo, then run ./first-install.sh. That's it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# A bit of feedback in case the user double-clicked from a file manager
# that doesn't show stdout. Most file managers DO open a terminal for
# scripts marked executable, but a heads-up doesn't hurt.
echo "==============================================="
echo "  VibeCoded Tools — First-Time Installer"
echo "==============================================="
echo ""
echo "This will:"
echo "  - Install Python 3.11+ if missing"
echo "  - Install Podman or Docker if neither is found"
echo "  - Detect GPU drivers and recommend installs"
echo "  - Set up the orchestrator (~5-10 min)"
echo ""
echo "If anything fails you'll see a clear URL to follow."
echo ""

# Sanity-check we actually have install.sh next to us. If someone copied
# this file out of the repo, fail loud rather than producing a confusing
# `command not found: install.sh`.
if [ ! -f "$SCRIPT_DIR/install.sh" ]; then
    echo "ERROR: install.sh not found alongside first-install.sh." >&2
    echo "       Make sure you ran first-install.sh from the cloned repo root." >&2
    echo "       Repo: https://github.com/hotak92/vibecoded-orchestrator" >&2
    exit 1
fi

# Optional Zenity heads-up — purely cosmetic. Skip on no-DISPLAY / non-TTY
# (CI, ssh-without-X) so we don't hang waiting on a dialog that never shows.
if [ -n "${DISPLAY:-}" ] && command -v zenity >/dev/null 2>&1; then
    zenity --info --no-wrap --title="VibeCoded Tools" \
        --text="Setting up VibeCoded Tools.\nThis takes ~5-10 minutes.\nProgress shows in the terminal." \
        2>/dev/null &
fi

# Forward all arguments to install.sh — same flag surface (`--no-containers`,
# `--gpu`, `--low-resource`, `--non-interactive`, etc.).
"$SCRIPT_DIR/install.sh" "$@"
status=$?

echo ""
if [ $status -eq 0 ]; then
    echo "Install complete. To start the launcher: ./start-launcher.sh"
    echo "(Or double-click start-launcher.desktop in your file manager.)"
else
    echo "Install failed (exit $status). See messages above."
fi

# Keep the window open after a Finder/Files double-click so the user can
# read the output. Skip in CI / piped runs (no TTY on stdin).
if [ -t 0 ]; then
    read -n 1 -s -r -p "Press any key to close this window..." || true
    echo ""
fi
exit $status

# TODO(post-v1.0): after install.sh succeeds, also:
#   - Build the Tauri launcher (cd launcher && pnpm install && pnpm tauri build)
#     OR download a pre-built binary from a GitHub release.
#   - install.py copies first-install.desktop / start-launcher.desktop into
#     ~/.local/share/applications/ so they appear in the apps menu.
#   - Optionally also drop them on ~/Desktop/ for max discoverability.
