#!/usr/bin/env bash
# start-launcher.command — Run the VibeCoded Tools launcher (macOS)
#
# Same logic as start-launcher.sh, with the .command extension so
# Finder treats it as double-clickable. See first-install.command for
# why we ship two files instead of a symlink.
#
# Binary search includes /Applications/VCT Launcher.app/Contents/MacOS/
# for users who installed via the .app bundle (post-v1.0). Bundle path
# format is set by Tauri's bundler — see launcher/src-tauri/tauri.conf.json
# `productName` = "VCT Launcher".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

candidates=(
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher-temp"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/launcher"
    "$SCRIPT_DIR/launcher/src-tauri/target/debug/vct-launcher-temp"
    # macOS .app bundle paths (post-v1.0 packaging). The internal
    # binary name inside the bundle is `productName` minus spaces — but
    # Tauri uses `mainBinaryName` if set, else `productName` verbatim.
    # Cover both spellings to be safe.
    "/Applications/VCT Launcher.app/Contents/MacOS/VCT Launcher"
    "/Applications/VCT Launcher.app/Contents/MacOS/vct-launcher"
    "$HOME/Applications/VCT Launcher.app/Contents/MacOS/VCT Launcher"
    "$HOME/Applications/VCT Launcher.app/Contents/MacOS/vct-launcher"
)

LAUNCHER_BIN=""
for cand in "${candidates[@]}"; do
    if [ -x "$cand" ]; then
        LAUNCHER_BIN="$cand"
        break
    fi
done

if [ -z "$LAUNCHER_BIN" ]; then
    echo "ERROR: launcher binary not found." >&2
    echo "" >&2
    echo "Run ./first-install.command first to set up VibeCoded Tools." >&2
    echo "If you already did, the launcher binary may not have been built yet." >&2
    echo "Build it manually:" >&2
    echo "  cd launcher && pnpm install && pnpm tauri build" >&2
    if [ -t 0 ]; then
        read -n 1 -s -r -p "Press any key to close this window..."
    fi
    exit 1
fi

exec "$LAUNCHER_BIN" "$@"
