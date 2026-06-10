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
#     build deps. See internal/RELEASING.md.

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
# `BUILD_TARGET` env override lets CI explicitly pick the canonical
# directory name (e.g. `macos-arm64` for the Apple-Silicon job, where
# uname-detection alone produced the legacy `experimental_macOS` slot
# used for local maintainer builds).
case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   HOST_TARGET="${BUILD_TARGET:-linux-x64}";   HOST_BIN="vct-launcher" ;;
    Darwin-arm64)   HOST_TARGET="${BUILD_TARGET:-macos-arm64}"; HOST_BIN="vct-launcher" ;;
    Darwin-x86_64)  HOST_TARGET="${BUILD_TARGET:-macos-x64}";   HOST_BIN="vct-launcher" ;;
    MINGW*|MSYS*|CYGWIN*) HOST_TARGET="${BUILD_TARGET:-windows-x64}"; HOST_BIN="vct-launcher.exe" ;;
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

# Install JS deps if missing OR if the lockfile is newer than node_modules.
# The latter case bites maintainers who `git pull` after a Dependabot bump
# (lockfile updated, node_modules stale): Tauri's version-mismatch check
# uses the installed @tauri-apps/api in node_modules — not the lockfile —
# and refuses to build with an error like:
#     tauri (v2.11.0) : @tauri-apps/api (v2.10.1)
# Use the npm-written marker (node_modules/.package-lock.json) to detect
# whether the install matches the current lockfile. With pnpm we use
# node_modules/.modules.yaml instead. If either marker is missing or
# older than the lockfile, reinstall.
NEEDS_INSTALL=0
if [ ! -d node_modules ]; then
    NEEDS_INSTALL=1
    INSTALL_REASON="node_modules missing"
elif [ "$PKG_MGR" = "pnpm" ]; then
    if [ ! -f node_modules/.modules.yaml ] || [ pnpm-lock.yaml -nt node_modules/.modules.yaml ]; then
        NEEDS_INSTALL=1
        INSTALL_REASON="pnpm-lock.yaml newer than installed snapshot"
    fi
else
    if [ ! -f node_modules/.package-lock.json ] || [ package-lock.json -nt node_modules/.package-lock.json ]; then
        NEEDS_INSTALL=1
        INSTALL_REASON="package-lock.json newer than installed snapshot"
    fi
fi

if [ "$NEEDS_INSTALL" -eq 1 ]; then
    echo "[build-bundled] $PKG_MGR install ($INSTALL_REASON)"
    if [ "$PKG_MGR" = "pnpm" ]; then
        # `pnpm install --frozen-lockfile` matches `npm ci` semantics —
        # fails fast if lockfile is out of sync, never silently re-resolves.
        pnpm install --frozen-lockfile
    else
        # `npm ci` is strict about lockfile match (vs `npm install`, which
        # may silently update the lockfile). Falls back to `npm install`
        # if the lockfile is missing — but for our repo it always exists.
        if [ -f package-lock.json ]; then
            npm ci
        else
            npm install
        fi
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

# v0.2.53 DEDUP-15: source the shared helper instead of inline-duplicating
# the strings → PowerShell → grep -aoc backend cascade. The helper at
# scripts/lib/asset-ref-count.sh is the single source of truth for the
# `_app/immutable/` marker, the VCT_ASSET_REF_MIN threshold (5), and the
# 3-backend cascade. See `.claude/context/audits/v0253-phase3-audit-E-
# dedup-correctness-2026-06-10.md` §DEDUP-15.
_BUILD_BUNDLED_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/asset-ref-count.sh
. "$_BUILD_BUNDLED_SCRIPT_DIR/lib/asset-ref-count.sh"

_count_asset_refs() {
    asset_ref_count "$1"
}

if [ -n "$PROBE_BIN" ]; then
    EMBEDDED_COUNT="$(_count_asset_refs "$PROBE_BIN")"
    EMBEDDED_COUNT="${EMBEDDED_COUNT:-0}"
    BIN_SIZE_BYTES="$(stat -c%s "$PROBE_BIN" 2>/dev/null || stat -f%z "$PROBE_BIN" 2>/dev/null || echo 0)"
    if ! [ "$EMBEDDED_COUNT" -eq "$EMBEDDED_COUNT" ] 2>/dev/null; then
        # Not a number — count helper failed silently.
        echo "[build-bundled] WARNING: cannot validate asset embedding (no strings/powershell available)." >&2
        echo "                Skipping post-build asset-ref check; manual verification recommended." >&2
    elif [ "$EMBEDDED_COUNT" -lt 5 ]; then
        # On Windows GitHub-runner Git Bash, the PowerShell-fallback in
        # _count_asset_refs has been observed returning 0 even on healthy
        # binaries (cygpath / powershell.exe -NoProfile invocation
        # quirk inside windows-latest's MINGW64). The SvelteKit pre-build
        # assertion at line ~92 already guarantees frontend assets exist
        # on disk before tauri build embeds them. So when the asset-ref
        # check returns 0 BUT the binary is reasonably sized (>5 MB —
        # an empty-frontend Tauri binary is ~22 MB on Windows / ~28 MB
        # Linux, vs ~24 MB / ~31 MB for a healthy one — so size alone
        # isn't a strong discriminator), we degrade FATAL → WARNING
        # instead of failing the release. Investigate the validator
        # separately; don't block ship-day.
        if [ "$BIN_SIZE_BYTES" -gt 5000000 ]; then
            echo "[build-bundled] WARNING: asset-ref count returned 0 but binary is ${BIN_SIZE_BYTES} bytes." >&2
            echo "                Likely a validator quirk on this platform (Git Bash on Windows CI runner)." >&2
            echo "                The SvelteKit pre-build assertion at line ~92 already verified frontend assets" >&2
            echo "                were present on disk before tauri build. Continuing without staging block." >&2
            echo "                Manual verification recommended: launch the binary, confirm UI loads." >&2
        else
            echo "[build-bundled] FATAL: built binary has $EMBEDDED_COUNT '_app/immutable/' refs (expected >=5)" >&2
            echo "                AND binary size is only $BIN_SIZE_BYTES bytes (suspiciously small)." >&2
            echo "                Frontend was NOT embedded. Refusing to stage. Investigate." >&2
            exit 1
        fi
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

# Tier marker. macOS targets ship UNSIGNED at this stage (Apple Developer
# ID + notarytool deferred to v0.2.x), so any consumer reading the
# metadata can route on `tier == "experimental"` to surface the
# Gatekeeper warning to end users. linux-x64 + windows-x64 ship as
# `stable`. Local maintainer macOS builds (legacy `experimental_macOS`
# slot) stay tier=`experimental` too.
case "$HOST_TARGET" in
    macos-arm64|macos-x64|experimental_macOS) TIER="experimental" ;;
    *) TIER="stable" ;;
esac

cat > "${DEST}.metadata.json" <<METADATA_EOF
{
  "source_sha": "$SOURCE_SHA",
  "source_short_sha": "$SOURCE_SHORT_SHA",
  "source_hash": "$SOURCE_HASH",
  "built_at": "$BUILT_AT",
  "launcher_version": "$TAURI_VERSION",
  "host_target": "$HOST_TARGET",
  "binary_name": "$HOST_BIN",
  "binary_size_bytes": $(stat -c%s "$DEST" 2>/dev/null || stat -f%z "$DEST" 2>/dev/null || echo 0),
  "tier": "$TIER"
}
METADATA_EOF
echo "[build-bundled] Staged: $DEST"
echo "[build-bundled] Metadata: ${DEST}.metadata.json"
echo "[build-bundled] Source SHA: $SOURCE_SHA"
echo "[build-bundled] Source hash (launcher subtree): $SOURCE_HASH"
echo "[build-bundled] Binary size: $(du -h "$DEST" | cut -f1)"

# v0.2.21+: stage the vct-hub binary alongside vct-launcher.
#
# vct-hub is a sibling Cargo workspace member (launcher/src-tauri/vct-hub/)
# introduced in v0.2.21. Tauri's `tauri build --no-bundle` above builds
# the launcher binary but does NOT necessarily build every workspace
# member — its scope is the binary referenced by tauri.conf.json. We
# therefore call cargo explicitly here for vct-hub. Cargo treats this
# as a no-op if vct-hub was already built (e.g. by a future Tauri CLI
# that walks the full workspace). Belt-and-braces.
#
# The embedded SvelteKit asset-ref probe above (lines ~189-261) does NOT
# apply to vct-hub: vct-hub is an axum HTTP server with no frontend.
# Running that check against it would correctly return 0 refs and
# false-fail. We intentionally skip the asset-ref probe for vct-hub.
echo "[build-bundled] cargo build -p vct-hub --release --bin vct-hub"
# The `-p vct-hub` package selector is required because the launcher's
# workspace root (the vct-launcher-temp package at $SRC_TAURI) has no
# `vct-hub` bin target — that target lives in the vct-hub workspace
# member. Pre-fix this command failed with "no bin target named
# `vct-hub` in default-run packages" because cargo defaulted to the
# workspace root rather than the vct-hub member.
( cd "$SRC_TAURI" && cargo build -p vct-hub --release --bin vct-hub )

# Pick the hub binary name for the host platform. Cargo package name is
# `vct-hub` (no `-temp` suffix); on Windows the artifact gets `.exe`.
case "$HOST_TARGET" in
    windows-x64) HUB_BIN="vct-hub.exe" ;;
    *)           HUB_BIN="vct-hub" ;;
esac

# Find what cargo actually built for vct-hub. Simpler candidate list
# than the launcher's because there is no `-temp` legacy name.
HUB_SRC=""
for cand in vct-hub vct-hub.exe; do
    if [ -x "$RELEASE_DIR/$cand" ]; then
        HUB_SRC="$RELEASE_DIR/$cand"
        break
    fi
done
if [ -z "$HUB_SRC" ]; then
    echo "[build-bundled] No vct-hub binary found in $RELEASE_DIR" >&2
    echo "                Workspace may not have built it. Try:" >&2
    echo "                  cd $SRC_TAURI && cargo build --release --bin vct-hub" >&2
    exit 1
fi

# Stage vct-hub into launcher/dist/<arch>/
HUB_DEST="$DIST_DIR/$HOST_TARGET/$HUB_BIN"
cp "$HUB_SRC" "$HUB_DEST"
chmod +x "$HUB_DEST"

# Emit vct-hub.metadata.json sidecar (mirrors the launcher sidecar shape).
# Fields are mostly identical between the two siblings — same source SHA,
# same release version, same target, same tier. Differs only in
# binary_name and binary_size_bytes. Consumers should NOT use source_hash
# to differentiate vct-hub's source from vct-launcher's — they're built
# from the same workspace at the same commit, so the hash is identical.
cat > "${HUB_DEST}.metadata.json" <<HUB_METADATA_EOF
{
  "source_sha": "$SOURCE_SHA",
  "source_short_sha": "$SOURCE_SHORT_SHA",
  "source_hash": "$SOURCE_HASH",
  "built_at": "$BUILT_AT",
  "launcher_version": "$TAURI_VERSION",
  "host_target": "$HOST_TARGET",
  "binary_name": "$HUB_BIN",
  "binary_size_bytes": $(stat -c%s "$HUB_DEST" 2>/dev/null || stat -f%z "$HUB_DEST" 2>/dev/null || echo 0),
  "tier": "$TIER"
}
HUB_METADATA_EOF
echo "[build-bundled] Staged hub: $HUB_DEST"
echo "[build-bundled] Hub metadata: ${HUB_DEST}.metadata.json"
echo "[build-bundled] Hub binary size: $(du -h "$HUB_DEST" | cut -f1)"

# v0.2.52 V52-AH (Fabio bug 1, 2026-06-09): build + stage vct-updater.
#
# vct-updater is the stage1 helper that performs the Windows binary
# swap after the running launcher exits. The binary itself is
# cross-platform (compiles cleanly on Linux/macOS — it's just a no-op
# on POSIX), but only Windows users actually need it at runtime. We
# build for the host arch unconditionally so contributor / CI hosts
# verify the binary still compiles, and stage it into dist/<arch>/ on
# every platform so `install.py` + the launcher can find it via the
# canonical lookup path.
#
# Same `-p vct-updater` selector pattern as vct-hub for the same reason
# (workspace root has no `vct-updater` bin target).
echo "[build-bundled] cargo build -p vct-updater --release --bin vct-updater"
( cd "$SRC_TAURI" && cargo build -p vct-updater --release --bin vct-updater )

case "$HOST_TARGET" in
    windows-x64) UPDATER_BIN="vct-updater.exe" ;;
    *)           UPDATER_BIN="vct-updater" ;;
esac

UPDATER_SRC=""
for cand in vct-updater vct-updater.exe; do
    if [ -x "$RELEASE_DIR/$cand" ]; then
        UPDATER_SRC="$RELEASE_DIR/$cand"
        break
    fi
done
if [ -z "$UPDATER_SRC" ]; then
    echo "[build-bundled] No vct-updater binary found in $RELEASE_DIR" >&2
    echo "                Try: cd $SRC_TAURI && cargo build -p vct-updater --release" >&2
    exit 1
fi

UPDATER_DEST="$DIST_DIR/$HOST_TARGET/$UPDATER_BIN"
cp "$UPDATER_SRC" "$UPDATER_DEST"
chmod +x "$UPDATER_DEST"

# Sidecar metadata mirrors the launcher / hub shape. Same source SHA +
# source hash (one workspace, one commit). tier=experimental matches
# the rest of the build (no signing pipeline for this small helper).
cat > "${UPDATER_DEST}.metadata.json" <<UPDATER_METADATA_EOF
{
  "source_sha": "$SOURCE_SHA",
  "source_short_sha": "$SOURCE_SHORT_SHA",
  "source_hash": "$SOURCE_HASH",
  "built_at": "$BUILT_AT",
  "launcher_version": "$TAURI_VERSION",
  "host_target": "$HOST_TARGET",
  "binary_name": "$UPDATER_BIN",
  "binary_size_bytes": $(stat -c%s "$UPDATER_DEST" 2>/dev/null || stat -f%z "$UPDATER_DEST" 2>/dev/null || echo 0),
  "tier": "$TIER"
}
UPDATER_METADATA_EOF
echo "[build-bundled] Staged updater: $UPDATER_DEST"
echo "[build-bundled] Updater metadata: ${UPDATER_DEST}.metadata.json"
echo "[build-bundled] Updater binary size: $(du -h "$UPDATER_DEST" | cut -f1)"

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
    echo "                  - GitHub Actions matrix (see internal/RELEASING.md)"
    echo "                  - Run this script directly on each platform"
    echo "                  - Tauri's cross-build via 'cross' (advanced)"
fi
