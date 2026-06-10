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

# M-P1-4 (v0.2.53): when Finder launches a .command file, the script's
# cwd is the user's $HOME — not the script's directory. Resolve
# SCRIPT_DIR explicitly + cd into it so relative paths (and the
# launcher binary's own cwd assumptions, e.g. for vct-hub spawn or
# .env reading) behave the same as when run from a Terminal session.
# `cd` is idempotent (no-op when already there), so re-runs are
# harmless.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

candidates=(
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher-temp"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/launcher"
    "$SCRIPT_DIR/launcher/src-tauri/target/debug/vct-launcher-temp"
    # Bundled prebuilt binary shipped in the repo. Canonical dist dir
    # has been `macos-arm64/` since v0.2.13 (install.py:16956 documents
    # this). The earlier `experimental_macOS/` slot is retained as a
    # legacy fallback for users on an old checkout — but the runtime
    # binary lives under `macos-arm64/` in every modern release.
    # M-P0-2 (v0.2.53): add macos-arm64 + keep experimental_macOS as
    # legacy.
    "$SCRIPT_DIR/launcher/dist/macos-arm64/vct-launcher"
    "$SCRIPT_DIR/launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/vct-launcher"
    "$SCRIPT_DIR/launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/VCT Launcher"
    # Legacy dist dir (pre-v0.2.13). Kept as fallback only.
    "$SCRIPT_DIR/launcher/dist/experimental_macOS/vct-launcher"
    # macOS .app bundle paths (post-v1.0 packaging). The internal
    # binary name inside the bundle is `productName` minus spaces — but
    # Tauri uses `mainBinaryName` if set, else `productName` verbatim.
    # Cover both spellings to be safe.
    "/Applications/VCT Launcher.app/Contents/MacOS/VCT Launcher"
    "/Applications/VCT Launcher.app/Contents/MacOS/vct-launcher"
    "$HOME/Applications/VCT Launcher.app/Contents/MacOS/VCT Launcher"
    "$HOME/Applications/VCT Launcher.app/Contents/MacOS/vct-launcher"
)

# Refuse to launch a release binary that has no embedded SvelteKit
# frontend. A build that ran with an empty `launcher/build/` produces
# a binary that compiles fine but renders "Could not connect to
# localhost" at runtime (regressed in 5abb8cf, 2026-04-28).
#
# NEW-1 + DEDUP-15 (v0.2.53): broad substring `_app/immutable/` via
# shared helper at `scripts/lib/asset-ref-count.sh`. Previous narrow
# substring `_app/immutable/assets` false-rejects Svelte 5 builds.
# shellcheck source=scripts/lib/asset-ref-count.sh
. "$SCRIPT_DIR/scripts/lib/asset-ref-count.sh"

_binary_has_embedded_frontend() {
    asset_ref_count_passes "$1"
}

LAUNCHER_BIN=""
SKIPPED_BROKEN=()
for cand in "${candidates[@]}"; do
    if [ -x "$cand" ]; then
        if _binary_has_embedded_frontend "$cand"; then
            LAUNCHER_BIN="$cand"
            break
        else
            SKIPPED_BROKEN+=("$cand")
            echo "WARNING: $cand has no embedded frontend — skipping (build was broken)." >&2
        fi
    fi
done

if [ -z "$LAUNCHER_BIN" ]; then
    echo "ERROR: launcher binary not found." >&2
    echo "" >&2
    echo "Searched:" >&2
    for cand in "${candidates[@]}"; do
        echo "  - $cand" >&2
    done
    if [ ${#SKIPPED_BROKEN[@]} -gt 0 ]; then
        echo "" >&2
        echo "Skipped broken binary/binaries (no embedded frontend):" >&2
        for b in "${SKIPPED_BROKEN[@]}"; do
            echo "  - $b" >&2
        done
        echo "" >&2
        echo "Rebuild with: bash scripts/build-bundled-launcher.sh" >&2
    fi
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
