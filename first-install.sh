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

# Sniff for our own flags before forwarding to install.sh. We accept
# --no-auto-launch (skip GUI auto-spawn at end) and pass-through everything
# else. --yes / --non-interactive / --quiet are forwarded but also picked
# up by post-install-launcher.sh as "no prompts" signals.
NO_AUTO_LAUNCH=0
HELPER_FLAGS=()
for arg in "$@"; do
    case "$arg" in
        --no-auto-launch) NO_AUTO_LAUNCH=1 ;;
        --yes|--non-interactive|--quiet) HELPER_FLAGS+=("--yes") ;;
    esac
done
if [ $NO_AUTO_LAUNCH -eq 1 ]; then
    HELPER_FLAGS+=("--no-auto-launch")
fi

# Forward all arguments to install.sh — same flag surface (`--no-containers`,
# `--gpu`, `--low-resource`, `--non-interactive`, etc.). Strip our own
# --no-auto-launch out (install.sh doesn't know it).
INSTALL_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-auto-launch) ;;  # consumed by us, not install.sh
        *) INSTALL_ARGS+=("$arg") ;;
    esac
done
if [ ${#INSTALL_ARGS[@]} -gt 0 ]; then
    "$SCRIPT_DIR/install.sh" "${INSTALL_ARGS[@]}"
else
    "$SCRIPT_DIR/install.sh"
fi
status=$?

echo ""
if [ $status -eq 0 ]; then
    # Hand off to the launcher-bootstrap helper. It probes for an existing
    # binary, offers download/build/skip, and (unless --no-auto-launch)
    # spawns the GUI detached so the user sees the launcher window at the
    # end of install. ALWAYS exits 0; never blocks our exit status.
    if [ -x "$SCRIPT_DIR/scripts/post-install-launcher.sh" ]; then
        if [ ${#HELPER_FLAGS[@]} -gt 0 ]; then
            "$SCRIPT_DIR/scripts/post-install-launcher.sh" "$SCRIPT_DIR" "${HELPER_FLAGS[@]}" || true
        else
            "$SCRIPT_DIR/scripts/post-install-launcher.sh" "$SCRIPT_DIR" || true
        fi
    else
        echo "Install complete. To start the launcher: ./start-launcher.sh"
        echo "(Or double-click start-launcher.desktop in your file manager.)"
    fi
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
