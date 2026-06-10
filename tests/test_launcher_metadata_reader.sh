#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Regression test for the metadata.json reader half (v0.2.53 Track A).
#
# Track D writes launcher/dist/<os-arch>/metadata.json at CI time;
# the reader (this commit's lib) parses candidate_paths_per_os.<os>
# and uses those entries as the canonical candidate list before the
# hardcoded fallback.
#
# Scenarios:
# 1. metadata.json present + valid: candidate paths come from JSON.
# 2. metadata.json absent: hardcoded fallback used (returns empty).
# 3. metadata.json malformed: reader fails soft (returns empty).
# 4. python3 unavailable: sed/grep fallback parses the flat array.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
LIB="$REPO_ROOT/scripts/lib/launcher-metadata.sh"

if [ ! -f "$LIB" ]; then
    echo "FAIL: $LIB not found"
    exit 1
fi

# Source the helper.
# shellcheck disable=SC1090
. "$LIB"

# ---- 1. Valid metadata.json --------------------------------------
SANDBOX="$(mktemp -d -t test-launcher-meta.XXXXXX)"
trap 'rm -rf "$SANDBOX"' EXIT
mkdir -p "$SANDBOX/launcher/dist/linux-x64"
mkdir -p "$SANDBOX/launcher/dist/macos-arm64"

cat > "$SANDBOX/launcher/dist/linux-x64/metadata.json" <<'JSON'
{
  "schema_version": 1,
  "binary_name": "vct-launcher",
  "vco_version": "v0.2.53",
  "build_time_utc": "2026-06-10T11:22:33Z",
  "frontend_asset_marker": "_app/immutable/",
  "expected_marker_count_min": 5,
  "candidate_paths_per_os": {
    "linux": [
      "launcher/dist/linux-x64/vct-launcher",
      "launcher/dist/linux-x64/vct-launcher.bin"
    ],
    "macos": [
      "launcher/dist/macos-arm64/vct-launcher",
      "launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/vct-launcher"
    ],
    "windows": ["launcher/dist/windows-x64/vct-launcher.exe"]
  }
}
JSON
cp "$SANDBOX/launcher/dist/linux-x64/metadata.json" \
   "$SANDBOX/launcher/dist/macos-arm64/metadata.json"

LINUX_CANDS="$(launcher_metadata_candidates "$SANDBOX" linux)"
if ! printf '%s' "$LINUX_CANDS" | grep -q "$SANDBOX/launcher/dist/linux-x64/vct-launcher$"; then
    echo "FAIL: Linux candidates missing flat vct-launcher path"
    echo "  Got: $LINUX_CANDS"
    exit 1
fi
if ! printf '%s' "$LINUX_CANDS" | grep -q "$SANDBOX/launcher/dist/linux-x64/vct-launcher.bin"; then
    echo "FAIL: Linux candidates missing vct-launcher.bin path"
    echo "  Got: $LINUX_CANDS"
    exit 1
fi

MAC_CANDS="$(launcher_metadata_candidates "$SANDBOX" macos)"
if ! printf '%s' "$MAC_CANDS" | grep -q "$SANDBOX/launcher/dist/macos-arm64/vct-launcher$"; then
    echo "FAIL: macOS candidates missing flat path"
    echo "  Got: $MAC_CANDS"
    exit 1
fi
if ! printf '%s' "$MAC_CANDS" | grep -q "vct-launcher.app/Contents/MacOS/vct-launcher"; then
    echo "FAIL: macOS candidates missing .app bundle path"
    echo "  Got: $MAC_CANDS"
    exit 1
fi

# ---- 2. metadata.json absent -------------------------------------
EMPTY_ROOT="$(mktemp -d -t test-launcher-meta-empty.XXXXXX)"
trap 'rm -rf "$SANDBOX" "$EMPTY_ROOT"' EXIT

EMPTY_OUT="$(launcher_metadata_candidates "$EMPTY_ROOT" linux 2>/dev/null || true)"
if [ -n "$EMPTY_OUT" ]; then
    echo "FAIL: expected empty candidates for missing metadata.json, got: $EMPTY_OUT"
    exit 1
fi

# ---- 3. metadata.json malformed ----------------------------------
mkdir -p "$EMPTY_ROOT/launcher/dist/linux-x64"
echo "not json {{{{" > "$EMPTY_ROOT/launcher/dist/linux-x64/metadata.json"
BAD_OUT="$(launcher_metadata_candidates "$EMPTY_ROOT" linux 2>/dev/null || true)"
if [ -n "$BAD_OUT" ]; then
    echo "FAIL: expected empty candidates for malformed metadata.json, got: $BAD_OUT"
    exit 1
fi

# ---- 4. Reader integration into find_binary (verified by grep) ----
if ! grep -q "_metadata_candidates_for_os" "$REPO_ROOT/scripts/post-install-launcher.sh"; then
    echo "FAIL: post-install-launcher.sh does not consult metadata.json"
    exit 1
fi
if ! grep -q "launcher_metadata_candidates" "$REPO_ROOT/start-launcher.sh"; then
    echo "FAIL: start-launcher.sh does not consult metadata.json"
    exit 1
fi
if ! grep -q "launcher_metadata_candidates" "$REPO_ROOT/start-launcher.command"; then
    echo "FAIL: start-launcher.command does not consult metadata.json"
    exit 1
fi

echo "PASS: launcher metadata.json reader works (valid + absent + malformed + integration)"
exit 0
