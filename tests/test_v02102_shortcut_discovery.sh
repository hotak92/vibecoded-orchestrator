#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Test: post-install-launcher.sh Step-2 launcher-binary selection rule
# (v0.2.102 S16-6 + M2, register §16 / P300:465).
#
# Background
# ==========
# The old candidate lists ranked the locally built
# launcher/src-tauri/target/release/* binaries ABOVE the bundled
# launcher/dist/<arch>/vct-launcher, and first-match-wins meant a stale
# months-old target/release leftover (exactly what install.py's tier-3
# fallback leaves behind) pinned the desktop shortcut forever: on the
# planning machine a v0.2.101 update rewrote both .desktop files to a
# 2026-06-13 artifact.
#
# The rule under test (ONE rule, mirrored in post-install-launcher.ps1):
#   1. dist first when it exists AND passes _bundled_binary_is_fresh;
#   2. a local build wins ONLY when it is newer than the dist binary
#      (mtime) — or when no fresh dist binary exists;
#   3. the chosen candidate and WHY are logged on one line.
#
# The tests extract the REAL arrays + find_binary out of the shipped
# script (same harness style as tests/test_post_install_launcher_dist_staging.py)
# so a rename or contract change fails loudly here.

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
LAUNCHER_SH="$ROOT/scripts/post-install-launcher.sh"

PASS=0
FAIL=0

# Extract a top-level shell function `name() { ... }` (closing brace in
# column 0) or a top-level array `name=( ... )` from the shipped script.
extract_fn() {
    awk -v fn="$1" 'index($0, fn"() {")==1 {p=1} p {print} p && $0=="}" {exit}' "$LAUNCHER_SH"
}
extract_arr() {
    awk -v a="$1" 'index($0, a"=(")==1 {p=1} p {print} p && $0==")" {exit}' "$LAUNCHER_SH"
}

check() {
    # check <name> <expected-substring> <actual>
    local name="$1" want="$2" got="$3"
    if printf '%s' "$got" | grep -qF "$want"; then
        echo "PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $name — wanted '$want' in:" >&2
        printf '%s\n' "$got" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Run find_binary in a sandbox with the REAL arrays/functions from the
# shipped script and stubbed helpers. Args:
#   run_find_binary <os> <repo_tmp> <freshness: fresh|stale> [arch]
# `arch` (default: the OS's canonical arch) is echoed by a shadowed `uname`
# so the host-arch gate is testable on any machine.
# Prints "OUT:<path>" / "ERR:<reason line>" / "RC:<n>" for the caller.
run_find_binary() {
    local os="$1" repo="$2" freshness="$3" arch="${4:-}"
    [ -z "$arch" ] && arch="$([ "$os" = macos ] && echo arm64 || echo x86_64)"
    local dist="$repo/launcher/dist"
    mkdir -p "$dist/linux-x64" "$dist/macos-arm64"

    local fresh_stub
    if [ "$freshness" = "stale" ]; then
        fresh_stub='_bundled_binary_is_fresh() {
            case "$1" in
                "$REPO_ROOT"/launcher/dist/*) echo "[launcher] (stub) stale" >&2; return 1 ;;
                *) return 0 ;;
            esac
        }'
    else
        fresh_stub='_bundled_binary_is_fresh() { return 0; }'
    fi

    {
        printf 'set -uo pipefail\n'
        printf 'REPO_ROOT="%s"\nOS="%s"\nHOME="%s"\n' "$repo" "$os" "$repo/home"
        printf 'uname() { echo "%s"; }\n' "$arch"
        printf '_log_event() { :; }\n'
        printf '_json_escape() { printf %%s "$1"; }\n'
        printf '_metadata_candidates_for_os() { return 0; }\n'
        printf '%s\n' "$fresh_stub"
        for a in candidates_dist candidates_dist_mac candidates_local \
                 candidates_system candidates_system_mac; do
            extract_arr "$a"
        done
        extract_fn _newer_mtime
        extract_fn _report_choice
        extract_fn find_binary
        printf 'reason_file="$(mktemp)"\n'
        printf 'out="$(find_binary 2>"$reason_file")"; rc=$?\n'
        printf 'printf "RC:%%s\\nOUT:%%s\\n" "$rc" "$out"\n'
        printf 'printf "ERR:%%s\\n" "$(cat "$reason_file")"\n'
    } > "$repo/harness.sh"

    bash "$repo/harness.sh" 2>&1
}

# Plant a fake executable binary at $1 with mtime $2 (touch -d format).
plant_bin() {
    local path="$1" when="$2"
    mkdir -p "$(dirname "$path")"
    printf '#!/bin/sh\necho fake\n' > "$path"
    chmod +x "$path"
    touch -d "$when" "$path"
}

# ---------------------------------------------------------------------------
# Arm 1 (THE regression): older target/release/vct-launcher-temp + fresh
# dist → dist chosen. Pre-fix (local listed above dist) this picked the
# stale local build.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-10-01 12:00"
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher-temp" "2026-06-13 12:00"
RES=$(run_find_binary linux "$T/repo" fresh)
check "arm1-linux-stale-local-loses (stdout)"  "$T/repo/launcher/dist/linux-x64/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm1-linux-stale-local-loses (reason)"  "Selected $T/repo/launcher/dist/linux-x64/vct-launcher (bundled dist binary, freshness verified)" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 2 (leave-alone): a genuinely NEWER local build beats the dist binary.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-06-13 12:00"
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher" "2026-10-01 12:00"
RES=$(run_find_binary linux "$T/repo" fresh)
check "arm2-newer-local-wins (stdout)"  "$T/repo/launcher/src-tauri/target/release/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm2-newer-local-wins (reason)"  "local build is newer than the dist binary" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 3 (freshness leg): a dist binary the freshness check REJECTS loses
# to any local build — rule 1 requires "exists AND passes fresh".
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-10-01 12:00"
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher-temp" "2026-06-13 12:00"
RES=$(run_find_binary linux "$T/repo" stale)
check "arm3-stale-dist-loses (stdout)"  "$T/repo/launcher/src-tauri/target/release/vct-launcher-temp" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm3-stale-dist-loses (reason)"  "no fresh bundled dist binary available" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 4 (macOS list, same rule): older target/release + fresh
# dist/macos-arm64 → dist chosen.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/macos-arm64/vct-launcher" "2026-10-01 12:00"
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher" "2026-06-13 12:00"
RES=$(run_find_binary macos "$T/repo" fresh)
check "arm4-macos-stale-local-loses (stdout)"  "$T/repo/launcher/dist/macos-arm64/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm4-macos-stale-local-loses (reason)"  "bundled dist binary, freshness verified" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 5 (macOS, leave-alone): newer local build wins over dist.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/macos-arm64/vct-launcher" "2026-06-13 12:00"
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher-temp" "2026-10-01 12:00"
RES=$(run_find_binary macos "$T/repo" fresh)
check "arm5-macos-newer-local-wins (stdout)"  "$T/repo/launcher/src-tauri/target/release/vct-launcher-temp" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm5-macos-newer-local-wins (reason)"  "local build is newer than the dist binary" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 6 (cross-arch, macOS): a checkout may carry BOTH committed dist trees
# (launcher/dist/linux-x64 AND launcher/dist/macos-arm64). On a macOS host
# the LINUX ELF must never be chosen — not even when its mtime is NEWER
# than the mac dist (review H-C1: the linux-x64 entry used to be appended
# unconditionally and first).
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/macos-arm64/vct-launcher" "2026-06-13 12:00"
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-10-01 12:00"   # newer — must still lose
RES=$(run_find_binary macos "$T/repo" fresh)
check "arm6-macos-ignores-linux-dist (stdout)"  "$T/repo/launcher/dist/macos-arm64/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm6-macos-ignores-linux-dist (reason)"  "bundled dist binary, freshness verified" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 7 (cross-arch, Linux): the mirror — a NEWER committed mac dist must
# never be chosen on a Linux host.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-06-13 12:00"
plant_bin "$T/repo/launcher/dist/macos-arm64/vct-launcher" "2026-10-01 12:00" # newer — must still lose
RES=$(run_find_binary linux "$T/repo" fresh)
check "arm7-linux-ignores-mac-dist (stdout)"  "$T/repo/launcher/dist/linux-x64/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm7-linux-ignores-mac-dist (reason)"  "bundled dist binary, freshness verified" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 8 (host arch, batch-5 follow-up): linux-x64 is the ONLY committed
# Linux dist binary; on an arm64/aarch64 Linux host it cannot execute, so
# a FRESH linux-x64 dist must lose to an OLDER local (arm) build — the
# dist list is empty on that arch.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-10-01 12:00"     # fresh but x86_64
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher" "2026-06-13 12:00" # older but native
RES=$(run_find_binary linux "$T/repo" fresh aarch64)
check "arm8-aarch64-linux-ignores-x64-dist (stdout)"  "$T/repo/launcher/src-tauri/target/release/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm8-aarch64-linux-ignores-x64-dist (reason)"  "no fresh bundled dist binary available" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 9 (host arch, leave-alone): on an x86_64 Linux host the same fresh
# linux-x64 dist still wins over the older local build.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
plant_bin "$T/repo/launcher/dist/linux-x64/vct-launcher" "2026-10-01 12:00"
plant_bin "$T/repo/launcher/src-tauri/target/release/vct-launcher" "2026-06-13 12:00"
RES=$(run_find_binary linux "$T/repo" fresh x86_64)
check "arm9-x86_64-linux-still-uses-x64-dist (stdout)"  "$T/repo/launcher/dist/linux-x64/vct-launcher" "$(printf '%s\n' "$RES" | grep '^OUT:')"
check "arm9-x86_64-linux-still-uses-x64-dist (reason)"  "bundled dist binary, freshness verified" "$(printf '%s\n' "$RES" | grep 'Selected')"
rm -rf "$T"

# ---------------------------------------------------------------------------
# Arm 10 (fall-through preserved): no repo-local candidate at all →
# find_binary fails (RC:1), so the script falls through to download/build.
# ---------------------------------------------------------------------------
T=$(mktemp -d)
mkdir -p "$T/repo"
RES=$(run_find_binary linux "$T/repo" fresh)
check "arm10-no-candidate-fails" "RC:1" "$(printf '%s\n' "$RES" | grep '^RC:')"
rm -rf "$T"

echo ""
echo "shortcut-discovery selection rule: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] || exit 1
echo "PASS: post-install-launcher.sh selection rule honours dist-first + mtime"
exit 0
