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

# First-run quarantine note: if Gatekeeper just whitelisted us via right-click
# → Open, the com.apple.quarantine xattr is still present. Future runs will
# have it cleared, so this is the user's signal that the warning is expected
# only on the very first run. We can't strip it ourselves (the OS would have
# already blocked us) — only document it.
if xattr -p com.apple.quarantine "$0" 2>/dev/null | grep -q .; then
    echo "Note: macOS Gatekeeper just allowed this script. Future runs will work normally."
    echo "      (If you saw 'developer cannot be verified' and right-clicked → Open, that's why.)"
    echo ""
fi

echo "==============================================="
echo "  VibeCoded Tools — First-Time Installer (macOS)"
echo "==============================================="
echo ""
echo "This will:"
echo "  - Auto-install Python 3.11+, Node.js 18+, and Podman via Homebrew if missing"
echo "    (interactive prompt before any brew invocation)"
echo "  - Auto-start the Podman daemon (podman machine start) if installed but stopped"
echo "    (deferral written to UPDATE_DEFERRED.md if start fails — e.g. machine not yet initialized)"
echo "  - Detect Apple Silicon (Metal) for GPU acceleration (drivers stay manual)"
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

# Sniff for our own flags before forwarding (see first-install.sh for the
# rationale; same logic here).
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

INSTALL_ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-auto-launch) ;;
        *) INSTALL_ARGS+=("$arg") ;;
    esac
done

# Forward all install args — Finder won't pass any, but Terminal-launched
# runs ($./first-install.command --no-gpu-check) do.
if [ ${#INSTALL_ARGS[@]} -gt 0 ]; then
    "$SCRIPT_DIR/install.sh" "${INSTALL_ARGS[@]}"
else
    "$SCRIPT_DIR/install.sh"
fi
status=$?

echo ""
if [ $status -eq 0 ]; then
    # Same launcher-bootstrap helper as Linux. It detects macOS via $OSTYPE
    # and uses .app bundle paths + hdiutil for DMG mounts.
    if [ -x "$SCRIPT_DIR/scripts/post-install-launcher.sh" ]; then
        if [ ${#HELPER_FLAGS[@]} -gt 0 ]; then
            "$SCRIPT_DIR/scripts/post-install-launcher.sh" "$SCRIPT_DIR" "${HELPER_FLAGS[@]}" || true
        else
            "$SCRIPT_DIR/scripts/post-install-launcher.sh" "$SCRIPT_DIR" || true
        fi
    else
        echo "Install complete. To start the launcher: ./start-launcher.command"
    fi
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
#   - Codesign the .app bundle (ad-hoc signing OK for v1.0; notarized
#     signing for v1.1+).
#   - Use osascript for a native progress dialog during long steps:
#       osascript -e 'display dialog "Installing VibeCoded Tools…" buttons {} giving up after 1'
