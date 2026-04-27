#!/usr/bin/env bash
# first-install.command — VibeCoded Tools first-time installer (macOS)
#
# Why .command: Finder treats files with the `.command` extension as
# double-clickable shell scripts. Clicking opens Terminal.app and runs
# the file. Without this extension, double-clicking a `.sh` on macOS
# either does nothing or opens it in TextEdit (depending on Finder
# preferences). The `.command` extension is the canonical way to ship
# a clickable shell installer on macOS.
#
# This file is a thin wrapper around first-install.sh — same logic,
# different filename so Finder shows the right icon + click behavior.
# We intentionally keep them as TWO files (not a symlink) because Git
# on Windows does not preserve symlinks reliably, and we want this
# repo to clone cleanly on every OS.
#
# Status: STUB. Delegates to install.sh. See first-install.sh for the
# full scope and the post-v1.0 TODO list.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "==============================================="
echo "  VibeCoded Tools — First-Time Installer (macOS)"
echo "==============================================="
echo ""
echo "This will:"
echo "  - Install Python 3.11+ via Homebrew (or print URL if brew absent)"
echo "  - Detect Podman/Docker; print install URLs if neither is present"
echo "  - Detect Apple Silicon (Metal) for GPU acceleration"
echo "  - Set up the orchestrator (~5-10 min)"
echo ""

if [ ! -f "$SCRIPT_DIR/install.sh" ]; then
    echo "ERROR: install.sh not found alongside first-install.command." >&2
    echo "       Make sure you ran first-install.command from the cloned repo root." >&2
    echo "       Repo: https://github.com/hotak92/vibecoded-orchestrator" >&2
    # On macOS, when launched from Finder, the Terminal window closes
    # immediately on exit — read a key first so the user can see the error.
    read -n 1 -s -r -p "Press any key to close this window..."
    exit 1
fi

# Forward all arguments — Finder won't pass any (it doesn't have a way
# to), but Terminal-launched runs ($./first-install.command --no-gpu-check)
# do.
"$SCRIPT_DIR/install.sh" "$@"
status=$?

echo ""
if [ $status -eq 0 ]; then
    echo "Install complete. To start the launcher: ./start-launcher.command"
else
    echo "Install failed (exit $status). See messages above."
fi

# Keep the Terminal window open after Finder-launched runs so the user
# can read the output. Skip this prompt if we're in a CI / non-TTY
# context (no stdin attached).
if [ -t 0 ]; then
    read -n 1 -s -r -p "Press any key to close this window..."
fi
exit $status

# TODO(post-v1.0): after install.sh succeeds, also:
#   - Build/install the Tauri launcher .app bundle into /Applications.
#   - Use osascript for a native progress dialog during long steps:
#       osascript -e 'display dialog "Installing VibeCoded Tools…" buttons {} giving up after 1'
#   - Codesign the .app bundle (ad-hoc signing OK for v1.0; notarized
#     signing for v1.1+).
