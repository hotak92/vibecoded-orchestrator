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

# v0.2.53 (Track A): metadata.json reader. If the release CI emitted
# launcher/dist/<os-arch>/metadata.json (Track D's work), its
# candidate_paths_per_os.linux array drives the search. The hardcoded
# fallback below covers dev builds + checkouts pre-dating Track D.
# See docs/INSTALL_ARCHITECTURE_v2.md §4.4 for the schema.
metadata_candidates=()
if [ -f "$SCRIPT_DIR/scripts/lib/launcher-metadata.sh" ]; then
    # shellcheck source=scripts/lib/launcher-metadata.sh
    . "$SCRIPT_DIR/scripts/lib/launcher-metadata.sh"
    if meta_lines="$(launcher_metadata_candidates "$SCRIPT_DIR" linux 2>/dev/null)"; then
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            metadata_candidates+=("$line")
        done <<META
$meta_lines
META
    fi
fi

# Search paths in priority order. Add to this list as packaging
# matures (snap, flatpak, .deb, AppImage, etc.). First match wins.
candidates=(
    "${metadata_candidates[@]+"${metadata_candidates[@]}"}"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/vct-launcher-temp"
    "$SCRIPT_DIR/launcher/src-tauri/target/release/launcher"
    # Debug build fallback — useful for contributors who only ran
    # `cargo build` (no --release). Slower startup but works.
    "$SCRIPT_DIR/launcher/src-tauri/target/debug/vct-launcher-temp"
    # Bundled prebuilt binary shipped in the repo. Last-resort fallback
    # for users who pulled the repo without running first-install.sh.
    "$SCRIPT_DIR/launcher/dist/linux-x64/vct-launcher"
    # System-wide install paths (post-v1.0 packaging).
    "/usr/bin/vct-launcher"
    "/usr/local/bin/vct-launcher"
    "$HOME/.local/bin/vct-launcher"
)

# Refuse to launch a release binary that has no embedded SvelteKit
# frontend. A build that ran with an empty `launcher/build/` produces
# a binary that compiles fine but renders "Could not connect to
# localhost" at runtime (regressed in 5abb8cf, 2026-04-28).
#
# NEW-1 + DEDUP-15 (v0.2.53): substring is `_app/immutable/` (broad —
# matches all SvelteKit emission shapes including chunks/, entry/,
# nodes/, and assets/). Previously this site used the narrow
# `_app/immutable/assets` which false-rejects healthy Svelte 5 builds.
# See `.claude/context/audits/shell-scripts-dedup-2026-06-10.md` §4.
# The shared implementation lives at `scripts/lib/asset-ref-count.sh`.
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
        echo "Rebuild with: cd launcher && bash scripts/rebuild-dist-binary.sh" >&2
    fi
    echo "" >&2
    echo "Run ./first-install.sh first to set up VibeCoded Tools." >&2
    echo "If you already did, the launcher binary may not have been built yet." >&2
    echo "Build it with the wrapper script (handles RUSTFLAGS + tauri --no-bundle):" >&2
    echo "  cd launcher && bash scripts/rebuild-dist-binary.sh" >&2
    echo "" >&2
    echo "DO NOT use 'cargo build --release' — it produces a binary with no" >&2
    echo "embedded SvelteKit frontend that hangs at startup with" >&2
    echo "'Could not connect to localhost'. The wrapper runs" >&2
    echo "'pnpm tauri build --no-bundle', which is the only correct path." >&2
    exit 1
fi

# Forward args (e.g. --headless, --debug). Use exec so the launcher
# becomes PID 1 of this shell — no extra bash process lingering.
exec "$LAUNCHER_BIN" "$@"
