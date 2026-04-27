#!/usr/bin/env bash
# start-launcher.sh — Run the VibeCoded Tools launcher (Linux)
#
# Run this AFTER first-install.sh has completed successfully. Locates
# the built Tauri binary, exec's it. If the binary doesn't exist yet,
# prints a clear instruction telling the user to run first-install.sh.
#
# Binary search order (resilient to renames between v0.x and v1.0):
#   1. launcher/src-tauri/target/release/vct-launcher        (planned v1.0 name)
#   2. launcher/src-tauri/target/release/vct-launcher-temp   (current pre-v1.0 name)
#   3. launcher/src-tauri/target/release/launcher            (alternate)
# Plus the AppImage / .deb install path under /usr/bin/vct-launcher
# for users who installed via package manager (post-v1.0).
#
# Status: STUB — works against the current dev-build binary path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Search paths in priority order. Add to this list as packaging
# matures (snap, flatpak, .deb, AppImage, etc.). First match wins.
candidates=(
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher-temp"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/launcher"
    # Debug build fallback — useful for contributors who only ran
    # `cargo build` (no --release). Slower startup but works.
    "$SCRIPT_DIR/launcher/src-tauri/target/debug/vct-launcher-temp"
    # System-wide install paths (post-v1.0 packaging).
    "/usr/bin/vct-launcher"
    "/usr/local/bin/vct-launcher"
    "$HOME/.local/bin/vct-launcher"
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
    echo "Searched:" >&2
    for cand in "${candidates[@]}"; do
        echo "  - $cand" >&2
    done
    echo "" >&2
    echo "Run ./first-install.sh first to set up VibeCoded Tools." >&2
    echo "If you already did, the launcher binary may not have been built yet." >&2
    echo "Build it manually:" >&2
    echo "  cd launcher && pnpm install && pnpm tauri build" >&2
    echo "(Or: cargo build --release --manifest-path launcher/src-tauri/Cargo.toml)" >&2
    exit 1
fi

# Forward args (e.g. --headless, --debug). Use exec so the launcher
# becomes PID 1 of this shell — no extra bash process lingering.
exec "$LAUNCHER_BIN" "$@"
