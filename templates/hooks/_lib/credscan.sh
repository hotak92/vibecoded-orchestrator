# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/credscan.sh — shared credential-pattern scanner used by
# post-tool-security.sh and the V52-L.1 SubagentStop reconciler.
#
# Background:
#   The SubagentStop reconciler needs credential scanning applied to every file
#   a subagent modified during its run (a `Bash` shell-out that wrote a
#   credential isn't caught by PostToolUse, which only fires on file edits).
#   Rather than duplicate the logic, both consumers source this helper.
#
# PATTERN SOURCE (changed — read this before adding a regex):
#   The patterns are NOT defined here, and this file is no longer a hand-kept
#   mirror of post-tool-security.sh. Both now read the SAME vocabulary from
#   _lib/credshapes.sh (`content_scan` context), whose SSOT is
#   vco_lib/credential_shapes.py. The old arrangement — "post-tool-security.sh
#   is canonical, this file mirrors it" — is exactly what let this copy fall
#   behind: it was missing the GitHub fine-grained PAT shape and the unquoted
#   dotenv-style generic-secret shape long after post-tool-security.sh gained
#   them, so the reconciler was blind to the token type VCO's own secrets flow
#   provisions. Add shapes to the SSOT, never here.
#
# Functions:
#   scan_file_for_credentials <file_path>
#     Runs the content_scan patterns against $file_path. Echoes each matched
#     label (newline-delimited) on stdout. Empty output → clean.
#     Returns 0 always (caller checks output, not exit code).
#
# Never echoes any part of the file's contents — labels only.

# Locate the vocabulary next to this helper. ${BASH_SOURCE[0]} is THIS file
# even when sourced, so the lookup follows the deployed _lib/ directory.
_CREDSCAN_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
if [ -r "$_CREDSCAN_LIB_DIR/credshapes.sh" ]; then
    # shellcheck source=credshapes.sh disable=SC1091
    . "$_CREDSCAN_LIB_DIR/credshapes.sh"
fi

scan_file_for_credentials() {
    local file_path="$1"
    local alerts=()
    [ -z "$file_path" ] && return 0
    [ ! -f "$file_path" ] && return 0

    # A MISSING vocabulary must not look like a clean file. Callers treat empty
    # output as "no credentials found", so degrading silently here would turn a
    # broken install into a permanent all-clear. Emit a real alert label
    # instead, so the miss surfaces through the same notification + JSONL path
    # every other finding uses.
    if ! command -v credshapes_for_context >/dev/null 2>&1; then
        printf '%s\n' "credential scanner UNAVAILABLE (_lib/credshapes.sh missing)"
        return 0
    fi

    # Skip non-text files (binaries) — grep on a JPEG produces noise.
    # `file -b --mime` is portable across Linux + macOS.
    if command -v file >/dev/null 2>&1; then
        local mime
        mime=$(file -b --mime "$file_path" 2>/dev/null | head -n1 || echo "")
        # Skip when the MIME starts with `application/`, `image/`, etc.
        # but isn't a known text-like type.
        case "$mime" in
            text/*|application/json*|application/xml*|application/javascript*|application/x-shellscript*|application/x-sh*|application/x-empty*|inode/x-empty*) ;;
            ""|*"charset=binary"*)
                # Binary files: skip silently. The legitimate side-effect of
                # this skip is that the credential scanner won't find keys
                # embedded in compiled binaries — but it would not have
                # matched the regexes there anyway, so net cost is 0.
                return 0
                ;;
        esac
    fi
    # Also skip files >5 MB — see snapshot.sh's reasoning. Grepping a
    # 50 MB log for credentials wastes time + memory.
    local size_bytes
    size_bytes=$(stat -c '%s' "$file_path" 2>/dev/null || stat -f '%z' "$file_path" 2>/dev/null || echo "0")
    if [ "${size_bytes:-0}" -gt $((5 * 1024 * 1024)) ]; then
        return 0
    fi

    # One pass per shape, in SSOT declaration order so the reported label
    # sequence is stable.
    if ! credshapes_for_context content_scan; then
        printf '%s\n' "credential scanner UNAVAILABLE (content_scan context rejected)"
        return 0
    fi
    local i
    for i in "${!CREDSHAPES_PATTERNS[@]}"; do
        if grep -qE -- "${CREDSHAPES_PATTERNS[$i]}" "$file_path" 2>/dev/null; then
            alerts+=("${CREDSHAPES_LABELS[$i]}")
        fi
    done

    local label
    for label in "${alerts[@]}"; do
        printf '%s\n' "$label"
    done
    return 0
}
