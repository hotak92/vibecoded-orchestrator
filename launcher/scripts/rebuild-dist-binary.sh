#!/usr/bin/env bash
# rebuild-dist-binary.sh — clean rebuild of launcher/dist/<arch>/vct-launcher
#
# Encapsulates the two privacy + correctness invariants that have
# repeatedly tripped maintainers up:
#
#   1. Use `pnpm tauri build --no-bundle` (NOT `cargo build --release`).
#      Plain cargo produces a binary that tries to load its frontend
#      from http://localhost:1420 (the Vite dev server) and hangs at
#      startup with "Could not connect to localhost: Connection
#      refused". Only `tauri build` runs the frontend pipeline and
#      embeds static assets for tauri://localhost/ resolution at runtime.
#
#   2. Set RUSTFLAGS for path-privacy. The .cargo/config.toml only
#      remaps OS-level prefixes (/home, /Users, C:\Users); the username
#      segment after still leaks ($HOME/<your-user>/.cargo/registry/...)
#      without an env-var-driven per-user remap.
#
# This script handles both. After it runs, the binary at
# launcher/dist/<arch>/vct-launcher is ready to commit.
#
# Usage:
#   cd launcher
#   bash scripts/rebuild-dist-binary.sh
#
# CI workflow uses this same script for the "Launcher binary leak-check"
# job to keep the build path identical between local and CI.

set -euo pipefail

# ------------------------------------------------------------
# Locate ourselves
# ------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_TAURI="$LAUNCHER_DIR/src-tauri"
DIST_DIR="$LAUNCHER_DIR/dist"

cd "$LAUNCHER_DIR"

# ------------------------------------------------------------
# OS / arch detection
# ------------------------------------------------------------
case "$(uname -s)" in
    Linux*)     ARCH_DIR="linux-x64"; BIN_NAME="vct-launcher"; STRIP_RE="^/home/[^/]+/" ;;
    # M-P0-2 (v0.2.53): canonical macOS dist dir is `macos-arm64/`
    # (matches install.py:16956 + release.yml asset naming since
    # v0.2.13). `experimental_macOS/` was the pre-v0.2.13 slot and
    # is no longer the correct write target.
    Darwin*)    ARCH_DIR="macos-arm64"; BIN_NAME="vct-launcher.app"; STRIP_RE="^/Users/[^/]+/" ;;
    CYGWIN*|MINGW*|MSYS*) echo "Use Windows-native PowerShell, not bash, on Windows. See launcher/dist/README.md."; exit 1 ;;
    *) echo "Unknown OS: $(uname -s)"; exit 1 ;;
esac

# ------------------------------------------------------------
# Privacy: RUSTFLAGS for per-user remap
# ------------------------------------------------------------
if [ -z "${HOME:-}" ]; then
    echo "ERROR: HOME env var not set. Cannot apply privacy remap." >&2
    exit 1
fi

# Cargo concatenates env-var RUSTFLAGS with [build] rustflags from
# .cargo/config.toml. Both sets of --remap-path-prefix flags are
# applied; the more-specific (longer-prefix) ones win.
export RUSTFLAGS="--remap-path-prefix=$HOME=<home> --remap-path-prefix=$HOME/.cargo=<cargo> ${RUSTFLAGS:-}"

# ------------------------------------------------------------
# Build
# ------------------------------------------------------------
echo "==> Installing npm deps (idempotent)..."
if [ ! -d node_modules ]; then
    npm install
fi

echo "==> Building with: pnpm tauri build --no-bundle"
echo "    (RUSTFLAGS active: $RUSTFLAGS)"
./node_modules/.bin/tauri build --no-bundle

# ------------------------------------------------------------
# Stage into dist/
# ------------------------------------------------------------
mkdir -p "$DIST_DIR/$ARCH_DIR"

if [ "$ARCH_DIR" = "macos-arm64" ]; then
    SRC="$SRC_TAURI/target/release/bundle/macos/vct-launcher.app"
    DEST="$DIST_DIR/$ARCH_DIR/vct-launcher.app"
    cp -R "$SRC" "$DEST"
    xattr -cr "$DEST"
    BIN_FOR_GREP="$DEST/Contents/MacOS/vct-launcher"
else
    SRC="$SRC_TAURI/target/release/vct-launcher-temp"
    DEST="$DIST_DIR/$ARCH_DIR/vct-launcher"
    cp "$SRC" "$DEST"
    chmod +x "$DEST"
    BIN_FOR_GREP="$DEST"
fi

# ------------------------------------------------------------
# Verify zero leaks
# ------------------------------------------------------------
echo "==> Verifying privacy: strings | grep -E '$STRIP_RE'"
HITS=$(strings "$BIN_FOR_GREP" | grep -E "$STRIP_RE" | wc -l)

if [ "$HITS" -gt 0 ]; then
    echo "ERROR: $HITS path-leak match(es) found in $DEST" >&2
    echo "Sample:" >&2
    strings "$BIN_FOR_GREP" | grep -E "$STRIP_RE" | head -3 >&2
    echo "" >&2
    echo "RUSTFLAGS may not have been applied. Verify env was set" >&2
    echo "BEFORE this script ran (it inherits, doesn't reset):" >&2
    echo "  echo \"\$RUSTFLAGS\"" >&2
    exit 1
fi

# ------------------------------------------------------------
# Sanity: confirm tauri build, not cargo build
# ------------------------------------------------------------
# A cargo-only build embeds 'http://localhost:1420' as the WebView's
# main URL (instead of 'tauri://localhost/'). Detect by absence of the
# tauri-static-asset string AND presence of the localhost:1420 dev-URL
# string in a context that isn't just an icon path.
if ! strings "$BIN_FOR_GREP" | grep -q "tauri://localhost"; then
    echo "ERROR: $DEST appears to be a 'cargo build --release' artifact" >&2
    echo "(no 'tauri://localhost' string found). Re-run with" >&2
    echo "'pnpm tauri build --no-bundle' to embed static assets." >&2
    exit 1
fi

echo "==> OK: $DEST"
echo "    size: $(stat -c%s "$BIN_FOR_GREP" 2>/dev/null || stat -f%z "$BIN_FOR_GREP" 2>/dev/null) bytes"
echo "    privacy: 0 path-leak matches"
echo "    runtime: tauri://localhost asset embedding confirmed"
echo ""
echo "Next: 'git add launcher/dist/$ARCH_DIR/' and commit."
