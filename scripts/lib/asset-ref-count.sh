#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# scripts/lib/asset-ref-count.sh — count SvelteKit asset references
# inside a launcher binary to detect "no embedded frontend" builds.
#
# A Tauri launcher binary built without running the frontend pipeline
# (e.g. `cargo build --release` without first `pnpm tauri build`) is
# missing the embedded SvelteKit static-asset references and will
# render "Could not connect to localhost" at runtime. The marker we
# count is `_app/immutable/` — the directory prefix SvelteKit emits
# for ALL asset chunks (chunks, entry, nodes, AND assets/). See
# `.claude/context/audits/shell-scripts-dedup-2026-06-10.md` §Finding 4.
#
# Drift history:
# - CI / build / check scripts use the BROAD substring `_app/immutable/`
#   (correct on Svelte 5 builds that emit chunks/ entry/ nodes/ but
#   no assets/ dir).
# - Runtime scripts (start-launcher.*, post-install-launcher.sh,
#   first-install.bat) historically used the NARROW substring
#   `_app/immutable/assets` (rejects healthy Svelte 5 builds on
#   Windows + fresh builds → "skipped broken binary" false-positive).
#
# v0.2.53 unifies both sides on the broad substring via this helper
# (and its PowerShell sibling at asset-ref-count.ps1). All callsites
# source this file and call `asset_ref_count "$binary_path"` /
# `asset_ref_count_passes "$binary_path"`.
#
# Usage:
#   source scripts/lib/asset-ref-count.sh
#   count=$(asset_ref_count "/path/to/binary")
#   if asset_ref_count_passes "/path/to/binary"; then echo embedded; fi

# The minimum count we require for a binary to be considered "frontend
# embedded". 5 is the historical threshold across all callsites — keep
# it parameterised so callers can override if needed.
: "${VCT_ASSET_REF_MIN:=5}"

# Marker substring. Broad form (matches all SvelteKit emission shapes).
# See drift discussion in shell-scripts-dedup audit Finding 4.
ASSET_REF_MARKER="_app/immutable/"

# Count occurrences of the marker in the binary. Returns the count on
# stdout.
#
# Backend cascade (try first, fall through on absence):
#   1. `strings | grep -c`         — POSIX (Linux + macOS + WSL)
#   2. PowerShell byte-scan        — Git Bash on Windows (no binutils;
#                                    powershell.exe is on PATH everywhere)
#   3. `grep -aoc`                 — last-resort, slower binary scan
#   4. -1 sentinel                 — none of the above available
#
# v0.2.53 DEDUP-15 absorbed the PowerShell + grep -aoc fallbacks from
# build-bundled-launcher.sh + check-bundled-binaries.sh so both CI
# scripts can source this helper instead of inline-duplicating ~24 LoC.
asset_ref_count() {
    local bin="$1"
    if [ -z "$bin" ] || [ ! -f "$bin" ]; then
        echo 0
        return 0
    fi
    # Backend 1: strings | grep -c (POSIX).
    if command -v strings >/dev/null 2>&1; then
        # `|| true` so a zero-match grep (exit 1) doesn't trip set -e
        # in callers. `strings` itself can fail on unreadable files;
        # trap that with the file-exists check above + `2>/dev/null`.
        strings "$bin" 2>/dev/null | grep -c "$ASSET_REF_MARKER" || true
        return 0
    fi
    # Backend 2: PowerShell byte-scan (Git Bash on Windows).
    if command -v powershell.exe >/dev/null 2>&1; then
        local winpath
        winpath="$(cygpath -w "$bin" 2>/dev/null || echo "$bin")"
        powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "
            \$bytes = [System.IO.File]::ReadAllBytes('$winpath');
            \$s = [System.Text.Encoding]::ASCII.GetString(\$bytes);
            (\$s.Split([string[]]@('$ASSET_REF_MARKER'), [System.StringSplitOptions]::None).Count - 1)
        " 2>/dev/null | tr -d '\r' | tr -d '[:space:]'
        return 0
    fi
    # Backend 3: grep -aoc (binary-mode regex count).
    if command -v grep >/dev/null 2>&1; then
        grep -aoc "$ASSET_REF_MARKER" "$bin" 2>/dev/null || echo 0
        return 0
    fi
    # Backend 4: nothing available — sentinel for callers to skip check.
    echo -1
}

# Predicate: returns 0 (success) when the binary's marker count is
# >= VCT_ASSET_REF_MIN OR when `strings` is unavailable (in which
# case we trust the binary rather than false-fail). Returns 1 (fail)
# otherwise.
asset_ref_count_passes() {
    local bin="$1"
    local count
    count="$(asset_ref_count "$bin")"
    # `strings` missing → trust the binary.
    if [ "${count:-0}" -eq -1 ]; then
        return 0
    fi
    [ "${count:-0}" -ge "${VCT_ASSET_REF_MIN}" ]
}
