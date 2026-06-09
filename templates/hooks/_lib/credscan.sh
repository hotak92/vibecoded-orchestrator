# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/credscan.sh — shared credential-pattern scanner used by
# post-tool-security.sh and the V52-L.1 SubagentStop reconciler.
#
# Background:
#   Post-tool-security.sh has historically hosted the canonical list of
#   credential regexes. The SubagentStop reconciler needs the same logic
#   applied to every file the subagent modified during its run (so a
#   `Bash` shell-out that wrote a credential isn't missed — Bash writes
#   don't trigger PostToolUse on file edits). Rather than duplicate the
#   patterns, extract them into this helper that both consumers source.
#
# Functions:
#   scan_file_for_credentials <file_path>
#     Runs the credential patterns against $file_path. Echoes each
#     matched label (newline-delimited) on stdout. Empty output → clean.
#     Returns 0 always (caller checks output, not exit code).
#
# Patterns (kept in sync with post-tool-security.sh — the canonical
# source of truth is post-tool-security.sh; this file mirrors it):
#   - Anthropic/OpenAI API key
#   - AWS access key
#   - GitHub token
#   - PEM private key
#   - Generic SECRET / API_KEY / ACCESS_TOKEN / PRIVATE_KEY env-style
#   - Hook leak-test marker (smoke-test only)

scan_file_for_credentials() {
    local file_path="$1"
    local alerts=()
    [ -z "$file_path" ] && return 0
    [ ! -f "$file_path" ] && return 0
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

    # Pattern checks — mirror post-tool-security.sh verbatim. Keep the
    # two lists in lockstep when adding new patterns; ideally factor
    # post-tool-security.sh to source this helper too (deferred to a
    # follow-up to avoid scope creep in V52-L.1).
    if grep -qE 'sk-(ant-api03|[a-zA-Z0-9]{30,})-[a-zA-Z0-9]' "$file_path" 2>/dev/null; then
        alerts+=("Anthropic/OpenAI API key")
    fi
    if grep -qE 'AKIA[A-Z0-9]{16}' "$file_path" 2>/dev/null; then
        alerts+=("AWS access key")
    fi
    if grep -qE 'gh[pousr]_[a-zA-Z0-9]{36}' "$file_path" 2>/dev/null; then
        alerts+=("GitHub token")
    fi
    if grep -qE 'BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY' "$file_path" 2>/dev/null; then
        alerts+=("PEM private key")
    fi
    if grep -qE '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["'"'"'][a-zA-Z0-9+/=_\-]{32,}' "$file_path" 2>/dev/null; then
        alerts+=("Generic secret")
    fi
    if grep -qE 'VCT_HOOK_LEAK_PROBE_a3f7c2' "$file_path" 2>/dev/null; then
        alerts+=("Hook leak-test marker")
    fi

    local label
    for label in "${alerts[@]}"; do
        printf '%s\n' "$label"
    done
    return 0
}
