#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# scripts/lib/launcher-metadata.sh — read launcher/dist/<os-arch>/metadata.json
#
# v0.2.53 (Track A — reader half of the metadata.json contract).
# The release CI (Track D) writes a metadata.json next to the bundled
# launcher binary with the canonical schema:
#
#   {
#     "schema_version": 1,
#     "binary_name": "vct-launcher",
#     "vco_version": "v0.2.53",
#     "build_time_utc": "2026-06-10T11:22:33Z",
#     "frontend_asset_marker": "_app/immutable/",
#     "expected_marker_count_min": 5,
#     "candidate_paths_per_os": {
#       "macos":   ["launcher/dist/macos-arm64/vct-launcher",
#                   "launcher/dist/macos-arm64/vct-launcher.app/Contents/MacOS/vct-launcher"],
#       "linux":   ["launcher/dist/linux-x64/vct-launcher"],
#       "windows": ["launcher/dist/windows-x64/vct-launcher.exe"]
#     }
#   }
#
# See docs/INSTALL_ARCHITECTURE_v2.md §4.4 for the full schema +
# rationale.
#
# This file provides:
#   launcher_metadata_path REPO_ROOT OS
#       Echo the absolute path of metadata.json for the OS (or empty).
#   launcher_metadata_candidates REPO_ROOT OS
#       Echo one absolute candidate path per line, in priority order,
#       extracted from candidate_paths_per_os.<OS>. Empty if metadata
#       missing or schema unrecognised. Failure is soft — callers
#       fall back to their hardcoded candidates lists.
#
# Implementation deliberately avoids `jq` (not preinstalled on bare
# macOS or stock Linux). Uses python3 when available (POSIX guaranteed
# on every supported orchestrator host because install.py itself
# needs Python ≥3.11). Falls back to a tiny sed/grep parser when
# python3 is missing — handles flat string arrays only (no nested
# objects / escaped quotes) which is fine for the v1 schema.

launcher_metadata_path() {
    local repo_root="$1"
    local os="$2"
    case "$os" in
        macos)
            for d in macos-arm64 macos-x64 experimental_macOS; do
                local p="$repo_root/launcher/dist/$d/metadata.json"
                if [ -f "$p" ]; then printf '%s\n' "$p"; return 0; fi
            done
            ;;
        linux)
            local p="$repo_root/launcher/dist/linux-x64/metadata.json"
            if [ -f "$p" ]; then printf '%s\n' "$p"; return 0; fi
            ;;
        windows)
            local p="$repo_root/launcher/dist/windows-x64/metadata.json"
            if [ -f "$p" ]; then printf '%s\n' "$p"; return 0; fi
            ;;
    esac
    return 1
}

launcher_metadata_candidates() {
    local repo_root="$1"
    local os="$2"
    local meta
    meta="$(launcher_metadata_path "$repo_root" "$os" 2>/dev/null)" || return 1
    [ -z "$meta" ] && return 1

    if command -v python3 >/dev/null 2>&1; then
        # Python parse — picks candidate_paths_per_os.<os>, joins
        # to $repo_root, prints one per line.
        python3 - "$meta" "$repo_root" "$os" <<'PY' 2>/dev/null || return 1
import json, os, sys
meta_path, repo_root, os_name = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(meta_path, "r", encoding="utf-8") as fh:
        meta = json.load(fh)
except Exception:
    sys.exit(1)
if not isinstance(meta, dict):
    sys.exit(1)
if int(meta.get("schema_version", 0) or 0) < 1:
    sys.exit(1)
paths = (meta.get("candidate_paths_per_os") or {}).get(os_name) or []
for p in paths:
    if not isinstance(p, str) or not p:
        continue
    # Relative paths anchor at repo_root.
    if os.path.isabs(p):
        print(p)
    else:
        print(os.path.join(repo_root, p))
PY
        return 0
    fi

    # Minimal sed fallback for the v1 flat-array shape.
    # Extracts the substring between
    #   "candidate_paths_per_os": { ... "<os>": [ "<p1>", "<p2>", ... ] ...
    local block
    block="$(sed -n "/\"candidate_paths_per_os\"/,/^\s*}\s*$/p" "$meta" 2>/dev/null)"
    [ -z "$block" ] && return 1
    local arr_block
    arr_block="$(printf '%s\n' "$block" | sed -n "/\"$os\"/,/]/p" 2>/dev/null)"
    [ -z "$arr_block" ] && return 1
    printf '%s\n' "$arr_block" \
        | grep -oE '"[^"]+"' \
        | sed -E 's/^"//; s/"$//' \
        | grep -v "^$os$" \
        | while read -r p; do
            case "$p" in
                /*) printf '%s\n' "$p" ;;
                *)  printf '%s/%s\n' "$repo_root" "$p" ;;
            esac
        done
}
