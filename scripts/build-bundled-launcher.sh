#!/usr/bin/env bash
# build-bundled-launcher.sh — rebuild launcher/dist/<arch>/vct-launcher
# from current source. Used by maintainers cutting a release to keep the
# bundled prebuilt binary in sync with the shipped source code.
#
# CRITICAL contract: the binary in launcher/dist/ MUST be reproducibly
# built from the exact source committed in the same repo state. End users
# clone HEAD → bundled binary AT THAT HEAD must work for them. Drift
# between bundled binary and source has happened — see the wizard skip-
# onboarding regression on 2026-04-28 where the bundled binary predated
# the wizard self-detect logic by several commits.
#
# Usage:
#   bash scripts/build-bundled-launcher.sh [--all]
#
#   --all  Build for every supported architecture (Linux x64, macOS arm64,
#          Windows x64). Default: build only for the host's arch.
#
# Requires:
#   - Node + pnpm (or npm)
#   - Rust toolchain (cargo)
#   - Linux only: libwebkit2gtk-4.1-dev + the rest of the Tauri Linux
#     build deps. See docs/RELEASING.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER_DIR="$REPO_ROOT/launcher"
SRC_TAURI="$LAUNCHER_DIR/src-tauri"
DIST_DIR="$LAUNCHER_DIR/dist"

BUILD_ALL=0
for arg in "$@"; do
    case "$arg" in
        --all) BUILD_ALL=1 ;;
        --help|-h)
            sed -n '1,/^set -euo/p' "$0" | head -n -1
            exit 0
            ;;
    esac
done

# Detect host arch.
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   HOST_TARGET="linux-x64";   HOST_BIN="vct-launcher" ;;
    Darwin-arm64)   HOST_TARGET="experimental_macOS"; HOST_BIN="vct-launcher" ;;
    Darwin-x86_64)  HOST_TARGET="experimental_macOS"; HOST_BIN="vct-launcher" ;;
    MINGW*|MSYS*|CYGWIN*) HOST_TARGET="windows-x64"; HOST_BIN="vct-launcher.exe" ;;
    *)
        echo "[build-bundled] Unrecognised host: $(uname -s)-$(uname -m). Aborting." >&2
        exit 1
        ;;
esac

cd "$LAUNCHER_DIR"

# Pick a package manager. pnpm preferred — npm fallback.
if command -v pnpm >/dev/null 2>&1; then
    PKG_MGR="pnpm"
elif command -v npm >/dev/null 2>&1; then
    PKG_MGR="npm"
else
    echo "[build-bundled] No pnpm or npm found. Install Node.js + pnpm first." >&2
    exit 1
fi

echo "[build-bundled] Using $PKG_MGR; target arch: $HOST_TARGET"

# Install JS deps if missing.
if [ ! -d node_modules ]; then
    echo "[build-bundled] $PKG_MGR install"
    if [ "$PKG_MGR" = "pnpm" ]; then
        pnpm install
    else
        npm install
    fi
fi

# Build the SvelteKit frontend EXPLICITLY first. Don't rely on Tauri's
# `beforeBuildCommand` — it silently no-ops if package-manager resolution
# differs between dev/CI runs, leaving `launcher/build/` empty. An empty
# frontendDist produces a release binary with no embedded assets, which
# loads as "Could not connect to localhost" at runtime (regression
# observed at commit 5abb8cf, 2026-04-28).
echo "[build-bundled] $PKG_MGR run build (SvelteKit frontend)"
if [ "$PKG_MGR" = "pnpm" ]; then
    pnpm run build
else
    npm run build
fi

# Sanity-check: frontend assets MUST exist before tauri build embeds them.
if [ ! -f "$LAUNCHER_DIR/build/index.html" ] || \
   ! ls "$LAUNCHER_DIR/build/_app/immutable/assets/" >/dev/null 2>&1; then
    echo "[build-bundled] FATAL: launcher/build/ is empty or missing _app/immutable/assets/." >&2
    echo "                Frontend build produced no output. Aborting before tauri build." >&2
    exit 1
fi
echo "[build-bundled] frontend assets present ($(ls "$LAUNCHER_DIR/build/_app/immutable/assets/" | wc -l) files)"

# Windows + OneDrive guard: if the repo lives under a OneDrive-synced
# path, the SvelteKit assets that pnpm just wrote may be in a
# dehydrated/syncing state when tauri-build's embedder reads them.
# The embedder gets ENOENT or zero bytes silently, builds a release
# binary with no embedded frontend, and the launcher renders "Could
# not connect to localhost" at runtime. Reported 2026-04-28 from a
# build inside C:\Users\<u>\OneDrive\Desktop\orchestrator\ — produced
# a 24 MB .exe with 0 asset refs vs the expected 31 MB / 31 refs.
case "$REPO_ROOT" in
    *OneDrive*|*onedrive*)
        echo "[build-bundled] WARNING: repo path contains 'OneDrive' — Tauri's embedder" >&2
        echo "                may fail to read SvelteKit assets due to OneDrive's virtual FS." >&2
        echo "                If the post-build asset-ref check below fails with 0 refs," >&2
        echo "                move the repo OUTSIDE the OneDrive-synced tree (e.g. C:\\dev\\)" >&2
        echo "                and rebuild." >&2
        ;;
esac

# Build with --no-bundle (we ship the binary, not the installer bundle).
# `npx` isn't always present on minimal Node installs (the user's
# `~/.local/bin/node` install on 2026-04-28 had `npm` but no `npx`).
# Prefer the locally-installed `node_modules/.bin/tauri` — that's what
# both pnpm and npx ultimately exec — falling back to package-manager
# wrappers if the local bin isn't there.
LOCAL_TAURI_BIN="$LAUNCHER_DIR/node_modules/.bin/tauri"
echo "[build-bundled] tauri build --no-bundle"
if [ -x "$LOCAL_TAURI_BIN" ]; then
    "$LOCAL_TAURI_BIN" build --no-bundle
elif [ "$PKG_MGR" = "pnpm" ]; then
    pnpm tauri build --no-bundle
elif command -v npx >/dev/null 2>&1; then
    npx tauri build --no-bundle
else
    # Last resort: `npm exec` (npm 7+) — equivalent to npx for local bins.
    npm exec --no -- tauri build --no-bundle
fi

# Sanity-check: the built binary MUST contain references to embedded
# SvelteKit assets. If we can find zero `_app/immutable/` matches,
# the frontend was NOT embedded (Tauri's frontendDist was empty or
# unreadable at build time). Refuse to stage a broken binary.
#
# `strings` is binutils-only and not in Git Bash on Windows. Fall
# back to PowerShell's byte-stream string scan when strings is
# absent. We grep for the broader `_app/immutable/` (no trailing
# `assets`) because newer SvelteKit / Svelte 5 emits chunks under
# `_app/immutable/{chunks,entry,nodes}/` instead of `assets/`, so
# requiring `assets/` produced false negatives on Windows builds
# (reported 2026-04-28 from a Git-Bash build that also lacked
# `strings` — it false-failed even though the .exe was healthy).
RELEASE_DIR="$SRC_TAURI/target/release"
PROBE_BIN=""
for cand in vct-launcher vct-launcher-temp launcher \
            vct-launcher.exe vct-launcher-temp.exe launcher.exe; do
    if [ -f "$RELEASE_DIR/$cand" ]; then
        PROBE_BIN="$RELEASE_DIR/$cand"
        break
    fi
done

_count_asset_refs() {
    # Returns the integer count of `_app/immutable/` substrings in the
    # binary's bytes. Tries strings → PowerShell → grep -ao fallback.
    local bin="$1"
    if command -v strings >/dev/null 2>&1; then
        strings "$bin" 2>/dev/null | grep -c '_app/immutable/' || true
        return
    fi
    # Windows fallback: PowerShell byte-scan. Works in Git Bash MSYS
    # because powershell.exe is on PATH on every modern Windows.
    if command -v powershell.exe >/dev/null 2>&1; then
        # Convert MSYS path to Windows path for PowerShell.
        local winpath
        winpath="$(cygpath -w "$bin" 2>/dev/null || echo "$bin")"
        powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
            \$bytes = [System.IO.File]::ReadAllBytes('$winpath');
            \$s = [System.Text.Encoding]::ASCII.GetString(\$bytes);
            (\$s.Split([string[]]@('_app/immutable/'), [System.StringSplitOptions]::None).Count - 1)
        " 2>/dev/null | tr -d '\r' | tr -d '[:space:]'
        return
    fi
    # Last-resort: `grep -ao` (binary-mode regex count) on platforms
    # that have neither strings nor powershell.
    grep -aoc '_app/immutable/' "$bin" 2>/dev/null || echo 0
}

if [ -n "$PROBE_BIN" ]; then
    EMBEDDED_COUNT="$(_count_asset_refs "$PROBE_BIN")"
    EMBEDDED_COUNT="${EMBEDDED_COUNT:-0}"
    if ! [ "$EMBEDDED_COUNT" -eq "$EMBEDDED_COUNT" ] 2>/dev/null; then
        # Not a number — count helper failed silently.
        echo "[build-bundled] WARNING: cannot validate asset embedding (no strings/powershell available)." >&2
        echo "                Skipping post-build asset-ref check; manual verification recommended." >&2
    elif [ "$EMBEDDED_COUNT" -lt 5 ]; then
        echo "[build-bundled] FATAL: built binary has $EMBEDDED_COUNT '_app/immutable/' refs (expected >=5)." >&2
        echo "                Frontend was NOT embedded. Refusing to stage. Investigate." >&2
        exit 1
    else
        echo "[build-bundled] embedded asset refs: $EMBEDDED_COUNT (passes sanity check)"
    fi
fi

# Find what was actually built. Tauri may produce vct-launcher-temp during
# the rename transition; both names map to the same binary.
RELEASE_DIR="$SRC_TAURI/target/release"
SRC_BIN=""
for cand in vct-launcher vct-launcher-temp launcher; do
    if [ -x "$RELEASE_DIR/$cand" ]; then
        SRC_BIN="$RELEASE_DIR/$cand"
        break
    fi
done
if [ -z "$SRC_BIN" ]; then
    echo "[build-bundled] No release binary found in $RELEASE_DIR" >&2
    exit 1
fi

# Stage into launcher/dist/<arch>/
DEST="$DIST_DIR/$HOST_TARGET/$HOST_BIN"
mkdir -p "$(dirname "$DEST")"
cp "$SRC_BIN" "$DEST"
chmod +x "$DEST"

# Versioning metadata so post-install-launcher.sh can detect when the
# bundled binary is stale vs the source on the user's clone. Schema is
# documented at launcher/dist/README.md → "Versioning metadata".
SOURCE_SHA="$(cd "$REPO_ROOT" && git rev-parse HEAD 2>/dev/null || echo '')"
SOURCE_SHORT_SHA="$(cd "$REPO_ROOT" && git rev-parse --short HEAD 2>/dev/null || echo '')"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TAURI_VERSION="$(grep '^version =' "$SRC_TAURI/Cargo.toml" | head -1 | sed -E 's/^version *= *"(.*)".*/\1/')"

# Compute a content hash of the launcher source that determines binary
# compatibility — if launcher/src-tauri/src/ + launcher/src/ + Cargo.lock
# + package.json haven't changed, the bundled binary is fresh.
SOURCE_HASH=""
if command -v git >/dev/null 2>&1; then
    SOURCE_HASH="$(cd "$REPO_ROOT" && git ls-tree HEAD launcher/src-tauri/src/ launcher/src/ launcher/src-tauri/Cargo.toml launcher/src-tauri/Cargo.lock launcher/package.json 2>/dev/null | git hash-object --stdin 2>/dev/null || echo '')"
fi

cat > "${DEST}.metadata.json" <<METADATA_EOF
{
  "source_sha": "$SOURCE_SHA",
  "source_short_sha": "$SOURCE_SHORT_SHA",
  "source_hash": "$SOURCE_HASH",
  "built_at": "$BUILT_AT",
  "launcher_version": "$TAURI_VERSION",
  "host_target": "$HOST_TARGET",
  "binary_name": "$HOST_BIN",
  "binary_size_bytes": $(stat -c%s "$DEST" 2>/dev/null || stat -f%z "$DEST" 2>/dev/null || echo 0)
}
METADATA_EOF
echo "[build-bundled] Staged: $DEST"
echo "[build-bundled] Metadata: ${DEST}.metadata.json"
echo "[build-bundled] Source SHA: $SOURCE_SHA"
echo "[build-bundled] Source hash (launcher subtree): $SOURCE_HASH"
echo "[build-bundled] Binary size: $(du -h "$DEST" | cut -f1)"
echo
echo "[build-bundled] Reminder: if THIRD_PARTY_LICENSES.txt is stale, regenerate it:"
echo "    cd $SRC_TAURI"
echo "    cargo install cargo-about    # one-time"
echo "    cargo about generate -c about.toml -o ../dist/THIRD_PARTY_LICENSES.html"
echo
echo "[build-bundled] Then commit launcher/dist/ and push."

# Cross-arch builds are out of scope — those need OS-specific runners
# (Apple Silicon Mac / Windows VM / Linux ARM box) or a CI matrix.
if [ "$BUILD_ALL" -eq 1 ]; then
    echo
    echo "[build-bundled] --all requested but only the host arch was built."
    echo "                Cross-arch builds need either:"
    echo "                  - GitHub Actions matrix (see docs/RELEASING.md)"
    echo "                  - Run this script directly on each platform"
    echo "                  - Tauri's cross-build via 'cross' (advanced)"
fi
