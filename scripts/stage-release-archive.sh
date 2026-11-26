#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# stage-release-archive.sh — the ONE home for the release-zip layout.
#
# Builds <out-dir>/<archive-base>/ from `git archive <ref>` (exactly the
# tracked tree at that ref — no hand-written file list to drift), applies the
# explicit exclusions below, then lays the prebuilt binaries for ONE target at
# launcher/dist/<target>/ — the path every entry point reads them from
# (start-launcher.*, install.py `_launcher_binary_relative_path`, the
# `--bootstrap` probe) and the path a `git clone` has them at. With --zip it
# also writes <out-dir>/<archive-base>.zip.
#
# Callers: .github/workflows/release.yml (build job, the published asset) and
# .github/workflows/install-smoke-release-zip.yml (PR CI: stages the same
# layout from the checkout and installs from the extracted zip). Locked by
# tests/test_stage_release_archive.py.
#
# Usage:
#   scripts/stage-release-archive.sh --target <linux-x64|windows-x64|macos-arm64>
#       --out-dir <dir>
#       [--archive-base <name>] (default: vibecoded-orchestrator-<version>-<target>,
#                                <version> read from launcher/src-tauri/Cargo.toml
#                                at <ref> — the release's own naming rule)
#       [--ref <git-ref>]       (default HEAD)
#       [--repo <path>]         (default: the repo this script lives in)
#       [--bin-dir <path>]      (default: <repo>/launcher/dist/<target>; the
#                                files directly in it overlay the archived
#                                copies — the release passes nothing and gets
#                                the binaries it just built there)
#       [--cli <path>]          (optional vct-cli binary, placed beside them)
#       [--version <x.y.z>]     (copies docs/MIGRATION-<x.y.z>.md to the root
#                                when that doc exists)
#       [--zip]                 (also create <out-dir>/<archive-base>.zip)
#
# Exit codes: 0 ok, 1 staging/layout error, 2 usage error.

set -euo pipefail

TARGET=""
ARCHIVE_BASE=""
OUT_DIR=""
REF="HEAD"
REPO=""
BIN_DIR=""
CLI_BIN=""
VERSION=""
MAKE_ZIP=0

usage() { sed -n '2,/^# Exit codes/p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --target)       TARGET="${2:-}"; shift 2 ;;
        --archive-base) ARCHIVE_BASE="${2:-}"; shift 2 ;;
        --out-dir)      OUT_DIR="${2:-}"; shift 2 ;;
        --ref)          REF="${2:-}"; shift 2 ;;
        --repo)         REPO="${2:-}"; shift 2 ;;
        --bin-dir)      BIN_DIR="${2:-}"; shift 2 ;;
        --cli)          CLI_BIN="${2:-}"; shift 2 ;;
        --version)      VERSION="${2:-}"; shift 2 ;;
        --zip)          MAKE_ZIP=1; shift ;;
        -h|--help)      usage ;;
        *) echo "stage-release-archive: unknown argument: $1" >&2; usage ;;
    esac
done

die() { echo "::error::stage-release-archive: $*" >&2; exit 1; }

[ -n "$TARGET" ] && [ -n "$OUT_DIR" ] || usage

ALL_TARGETS=(linux-x64 windows-x64 macos-arm64)
case "$TARGET" in
    windows-x64) EXE=".exe" ;;
    linux-x64|macos-arm64) EXE="" ;;
    *) echo "stage-release-archive: unknown --target '$TARGET' (expected: ${ALL_TARGETS[*]})" >&2; exit 2 ;;
esac

if [ -z "$REPO" ]; then
    REPO="$(cd "$(dirname "$0")/.." && pwd)"
fi
git -C "$REPO" rev-parse --verify --quiet "${REF}^{commit}" >/dev/null \
    || die "ref '$REF' is not a commit in $REPO"
if [ -z "$ARCHIVE_BASE" ]; then
    cargo_version="$(git -C "$REPO" show "$REF:launcher/src-tauri/Cargo.toml" \
        | grep -m1 '^version = ' | sed -E 's/^version *= *"([^"]+)".*/\1/')"
    [ -n "$cargo_version" ] || die "could not read the version from launcher/src-tauri/Cargo.toml at $REF"
    ARCHIVE_BASE="vibecoded-orchestrator-$cargo_version-$TARGET"
fi
case "$ARCHIVE_BASE" in
    */*|*\\*|.|..) echo "stage-release-archive: --archive-base must be a plain folder name" >&2; exit 2 ;;
esac
[ -n "$BIN_DIR" ] || BIN_DIR="$REPO/launcher/dist/$TARGET"
[ -d "$BIN_DIR" ] || die "binary directory not found: $BIN_DIR"

# The three binaries every install needs, at launcher/dist/<target>/.
REQUIRED_BINARIES=("vct-launcher$EXE" "vct-hub$EXE" "vct-updater$EXE")

# ── Exclusions ──────────────────────────────────────────────────────────
# Everything git tracks ships, EXCEPT (paths relative to the archive root):
#   tests/                 the developer test suite (~1 500 files, ~28 MB):
#                          no runtime role, and the orchestrator root's own
#                          code graph would index all of it on every user's
#                          machine.
#   .github/               CI workflow definitions; they run on GitHub only.
#   launcher/dist/<other>/ the committed binaries of the OTHER two targets:
#                          the wrong OS for this zip (≈40-55 MB each), and the
#                          by-name binary lookups in the launcher download
#                          paths must find exactly one vct-launcher.
#   vendor/lean-ctx/<other>/ third-party prebuilts for the other targets
#                          (today only the Windows lean-ctx.exe, ~58 MB).
# Nothing else is dropped: the earlier hand list also left out vco_lib/,
# scripts/, launcher/, migrations/, pyproject.toml, VCThelpers/ and vendor/,
# which is why a v0.2.108 zip could not install at all.
EXCLUDES=(tests .github)
for t in "${ALL_TARGETS[@]}"; do
    [ "$t" = "$TARGET" ] || EXCLUDES+=("launcher/dist/$t" "vendor/lean-ctx/$t")
done

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
STAGE="$OUT_DIR/$ARCHIVE_BASE"
rm -rf "$STAGE"

# 1. The tracked tree at REF. `git archive` applies .gitattributes eol rules
#    exactly like a checkout (.bat/.cmd CRLF, everything else LF) and keeps
#    the executable bits git records.
git -C "$REPO" archive --format=tar --prefix="$ARCHIVE_BASE/" "$REF" \
    | tar -xf - -C "$OUT_DIR"

# 2. Exclusions.
for rel in "${EXCLUDES[@]}"; do
    rm -rf "${STAGE:?}/$rel"
done

# 3. Binaries for this target. Every regular file directly in BIN_DIR
#    overlays the archived copy (binaries + their .metadata.json sidecars);
#    sub-folders such as the release's `_release/` rename area are skipped.
DIST="$STAGE/launcher/dist/$TARGET"
mkdir -p "$DIST"
for f in "$BIN_DIR"/*; do
    [ -f "$f" ] || continue
    cp "$f" "$DIST/"
done
for b in "${REQUIRED_BINARIES[@]}"; do
    [ -f "$DIST/$b" ] || die "required binary missing: launcher/dist/$TARGET/$b (looked in $BIN_DIR)"
done
if [ -n "$CLI_BIN" ]; then
    [ -f "$CLI_BIN" ] || die "--cli binary not found: $CLI_BIN"
    cp "$CLI_BIN" "$DIST/vct-cli$EXE"
fi
if [ -z "$EXE" ]; then
    for f in "$DIST"/vct-*; do
        case "$f" in *.json|*.md|*.toml|*.txt) ;; *) chmod +x "$f" ;; esac
    done
fi

# 4. Local-machine config beside the launcher binary (the launcher and the
#    service reconciler read `vct-config.toml` from the binary's folder).
if [ -f "$STAGE/vct-config.example.toml" ]; then
    cp "$STAGE/vct-config.example.toml" "$DIST/vct-config.toml"
fi

# 5. Per-release migration notes at the root, for visibility on extraction.
if [ -n "$VERSION" ] && [ -f "$STAGE/docs/MIGRATION-$VERSION.md" ]; then
    cp "$STAGE/docs/MIGRATION-$VERSION.md" "$STAGE/MIGRATION-$VERSION.md"
fi

# 6. Layout contract — the entries an install from this tree needs. A miss
#    fails the release before anything is uploaded. Every entry point ships
#    on every OS: install.py's validate_source_repo() requires install.py
#    AND first-install.sh side by side, on Windows too.
missing=0
for rel in install.py first-install.sh first-install.command first-install.bat \
           install.sh install.ps1 start-launcher.sh start-launcher.command \
           start-launcher.bat uninstall.sh uninstall.bat pyproject.toml \
           requirements.txt vco_lib/__init__.py VCThelpers/__init__.py \
           scripts/lib migrations claude_mcp_servers templates \
           infrastructure tools; do
    if [ ! -e "$STAGE/$rel" ]; then
        echo "::error::stage-release-archive: layout entry missing: $rel" >&2
        missing=$((missing + 1))
    fi
done
for b in "${REQUIRED_BINARIES[@]}" "vct-cli$EXE"; do
    if [ -e "$STAGE/$b" ]; then
        echo "::error::stage-release-archive: binary at the archive root: $b (belongs in launcher/dist/$TARGET/)" >&2
        missing=$((missing + 1))
    fi
done
[ "$missing" -eq 0 ] || die "$missing layout problem(s) in $STAGE"

echo "::group::Staging tree summary ($ARCHIVE_BASE)"
du -sh "$STAGE"
du -sh "$STAGE"/* 2>/dev/null || true
ls -l "$DIST"
echo "::endgroup::"

# 7. Optional zip. Info-ZIP `zip` where present (Linux/macOS runners; keeps
#    the Unix mode bits), else PowerShell 7's Compress-Archive (Windows
#    runners ship no `zip`). Either way the zip holds <archive-base>/... .
if [ "$MAKE_ZIP" -eq 1 ]; then
    ZIP="$OUT_DIR/$ARCHIVE_BASE.zip"
    rm -f "$ZIP"
    if command -v zip >/dev/null 2>&1; then
        (cd "$OUT_DIR" && zip -r -X -9 -q --symlinks "$ARCHIVE_BASE.zip" "$ARCHIVE_BASE")
    elif command -v pwsh >/dev/null 2>&1; then
        win_stage="$STAGE"; win_zip="$ZIP"
        if command -v cygpath >/dev/null 2>&1; then
            win_stage="$(cygpath -w "$STAGE")"; win_zip="$(cygpath -w "$ZIP")"
        fi
        STAGE_PATH="$win_stage" ZIP_PATH="$win_zip" pwsh -NoProfile -NonInteractive -Command \
            'Compress-Archive -Path $env:STAGE_PATH -DestinationPath $env:ZIP_PATH -CompressionLevel Optimal -Force'
    else
        die "neither 'zip' nor 'pwsh' is available to create $ZIP"
    fi
    [ -f "$ZIP" ] || die "zip was not created: $ZIP"
    ls -l "$ZIP"
fi
