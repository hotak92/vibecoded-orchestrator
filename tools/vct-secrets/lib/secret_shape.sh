# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# shellcheck shell=bash
#
# secret_shape.sh — the bash MIRROR of the single-line secret-value shape SSOT.
#
# SSOT: vco_lib/secret_value_shape.py (`is_single_line_secret`,
# `classify_secret_value`). Python is the source of truth (A>B>C rule). This
# file re-implements the predicate in pure bash because `vct` runs standalone
# from `~/.vct-secrets/vct` in git-credential-helper context with NO venv /
# PYTHONPATH guarantee — a `python -m` subprocess would silently no-op the
# write-guard exactly where it is most needed (Opus review change #2).
#
# Both `vct` and `migrate-shared.sh` SOURCE this one copy (SHARED-CODE RULE:
# never duplicate the predicate into two scripts). It is locked to the Python
# SSOT by tests/fixtures/secret_value_shape_parity.json, exercised by the bash
# parity leg in tools/vct-secrets/tests/test_vct.sh.
#
# The predicate NEVER prints or logs a secret value. It operates purely on the
# string it is handed and returns a verdict (exit code + a reason slug on
# stdout for the reject path).
#
# Portability: POSIX-ish bash (Linux / macOS / WSL2). Uses bash `[[ =~ ]]`
# (bash 3.2+, incl. macOS's bundled bash) and POSIX `sed`/`tr`. No GNU-only
# grep -P / sed -i / awk gensub.

# ---------------------------------------------------------------------------
# Named constants — mirror vco_lib/secret_value_shape.py EXACTLY.
# ---------------------------------------------------------------------------

# A github_pat-named single-line value at or above this length is almost
# certainly a concatenated / duplicated write. 40 (classic) / ~93 (fine-grained)
# are the real shapes; 200 is a wide margin no legit token reaches.
_SECRET_SHAPE_GITHUB_PAT_MAX_LEN=200

# The blob signature: a post-line-0 line that matches this at COLUMN 0 is an
# env-assignment / `export KEY=` continuation — the fingerprint of a blob.
# Mirrors Python's _BLOB_KEY_EQ_RE = r"^(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=".
# `[[:blank:]]` == `[ \t]` (space + tab, no newline) in the POSIX locale.
_SECRET_SHAPE_BLOB_RE='^(export[[:blank:]]+)?[A-Za-z_][A-Za-z0-9_]*='

# Legit multi-line allowlist (PEM / OpenSSH family). first non-empty line must
# match BEGIN, last non-empty line must match END. Mirrors
# _LEGIT_MULTILINE_ALLOWLIST in the Python SSOT.
_SECRET_SHAPE_PEM_BEGIN_RE='^-----BEGIN [A-Z0-9 ]*(PRIVATE KEY|CERTIFICATE|PUBLIC KEY)-----$'
_SECRET_SHAPE_PEM_END_RE='^-----END [A-Z0-9 ]*(PRIVATE KEY|CERTIFICATE|PUBLIC KEY)-----$'

# A GitHub classic PAT: `ghp_` + exactly 36 base62 chars → 40 total.
_SECRET_SHAPE_CLASSIC_PAT_RE='^ghp_[A-Za-z0-9]{36}$'

# github_pat-style key name (the length heuristic only applies to these).
# Mirrors Python's _GITHUB_PAT_KEY_RE = r"^github_pat(?:[._-].*)?$" (case-insensitive).
_SECRET_SHAPE_GITHUB_PAT_KEY_RE='^[Gg][Ii][Tt][Hh][Uu][Bb]_[Pp][Aa][Tt]([._-].*)?$'

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

# _secret_shape_forbidden_control_pattern — build the bracket expression of
# forbidden control bytes: 0x01-0x08, 0x0b, 0x0c, 0x0e-0x1f, 0x7f. Mirrors
# Python's _FORBIDDEN_CONTROL_RE = r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]".
# (\n=0x0a / \r=0x0d define line structure and are EXCLUDED; \t=0x09 allowed.
# NUL/0x00 can never appear in a bash string, so it is a no-op to omit it here.)
#
# The bracket is built from LITERAL control BYTES (via printf octal), NOT from
# backslash-octal escape sequences: GNU grep interprets `\001` inside `[]` as a
# byte but BSD grep does not, so an escape-based pattern is non-portable AND
# GNU's interpretation proved locale/version-sensitive. Placing the real bytes
# in the bracket is unambiguous under `LC_ALL=C` on both GNU and BSD grep.
_secret_shape_forbidden_control_pattern() {
    printf '['
    printf '\001-\010'   # 0x01-0x08
    printf '\013\014'    # 0x0b 0x0c (VT, FF)
    printf '\016-\037'   # 0x0e-0x1f
    printf '\177'        # 0x7f (DEL)
    printf ']'
}

# _secret_shape_has_control VALUE → exit 0 if VALUE contains a forbidden
# control char, exit 1 otherwise. Never prints the value.
_secret_shape_has_control() {
    local val="$1" pat
    pat=$(_secret_shape_forbidden_control_pattern)
    printf '%s' "$val" | LC_ALL=C grep -q "$pat"
}

# _secret_shape_normalize_lines — read a value on stdin, emit it with `\r\n`
# and lone `\r` converted to `\n` (so a `while read` loop sees the same line
# boundaries Python's str.splitlines() would: \n, \r, and \r\n).
_secret_shape_normalize_lines() {
    # Strip a trailing \r on each line (handles \r\n), then convert any
    # remaining lone \r to \n (handles bare-\r line separators). POSIX sed+tr.
    sed 's/\r$//' | tr '\r' '\n'
}

# _secret_shape_rstrip_crlf_ws VALUE → echo VALUE with trailing \r/\n stripped
# then trailing whitespace stripped. Mirrors Python
# `value.rstrip("\r\n").rstrip()`. Preserves interior newlines.
_secret_shape_rstrip_crlf_ws() {
    local t="$1"
    # Phase 1: rstrip("\r\n") — strip trailing CR/LF chars only.
    while [ -n "$t" ]; do
        case "$t" in
            *$'\n') t="${t%$'\n'}" ;;
            *$'\r') t="${t%$'\r'}" ;;
            *) break ;;
        esac
    done
    # Phase 2: rstrip() — strip trailing whitespace (space/tab/newline/CR).
    while [ -n "$t" ]; do
        case "$t" in
            *' ')    t="${t% }" ;;
            *$'\t')  t="${t%$'\t'}" ;;
            *$'\n')  t="${t%$'\n'}" ;;
            *$'\r')  t="${t%$'\r'}" ;;
            *) break ;;
        esac
    done
    printf '%s' "$t"
}

# _secret_shape_is_single_line VALUE → exit 0 if VALUE (already trimmed) has no
# interior \n or \r, exit 1 otherwise.
_secret_shape_is_single_line() {
    case "$1" in
        *$'\n'*|*$'\r'*) return 1 ;;
        *) return 0 ;;
    esac
}

# _secret_shape_is_github_pat_key KEY → exit 0 if KEY is a github_pat-style key.
_secret_shape_is_github_pat_key() {
    local key="$1"
    [ -n "$key" ] || return 1
    # Mirror Python's key_name.strip() before matching.
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    [[ "$key" =~ $_SECRET_SHAPE_GITHUB_PAT_KEY_RE ]]
}

# _secret_shape_is_legit_multiline VALUE → exit 0 if VALUE matches an
# allowlisted legit multi-line format (currently PEM/OpenSSH). Mirrors
# Python _is_legit_multiline: first non-empty line matches BEGIN, last
# non-empty line matches END, and at least 2 non-empty lines.
_secret_shape_is_legit_multiline() {
    local val="$1" line stripped first='' last='' count=0
    while IFS= read -r line || [ -n "$line" ]; do
        # rstrip each line (Python `_non_empty_lines` rstrips then drops empties).
        stripped="${line%"${line##*[![:space:]]}"}"
        case "$stripped" in '') continue ;; esac
        [ "$count" -eq 0 ] && first="$stripped"
        last="$stripped"
        count=$((count + 1))
    done < <(printf '%s' "$val" | _secret_shape_normalize_lines)
    [ "$count" -ge 2 ] || return 1
    [[ "$first" =~ $_SECRET_SHAPE_PEM_BEGIN_RE ]] || return 1
    [[ "$last" =~ $_SECRET_SHAPE_PEM_END_RE ]] || return 1
    return 0
}

# _secret_shape_has_blob_signature VALUE → exit 0 if any line AFTER line 0 is a
# column-0 `KEY=` / `export KEY=`. Mirrors Python _has_blob_signature
# (iterates value.splitlines()[1:]).
_secret_shape_has_blob_signature() {
    local val="$1" line n=0
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "$n" -gt 0 ] && [[ "$line" =~ $_SECRET_SHAPE_BLOB_RE ]]; then
            return 0
        fi
        n=$((n + 1))
    done < <(printf '%s' "$val" | _secret_shape_normalize_lines)
    return 1
}

# ---------------------------------------------------------------------------
# Public predicate — the bash mirror of is_single_line_secret().
# ---------------------------------------------------------------------------

# _is_single_line_secret VALUE [KEY_NAME] [ALLOW_MULTILINE]
#
#   VALUE           — the raw secret value about to be written.
#   KEY_NAME        — optional; enables the github_pat length heuristic.
#   ALLOW_MULTILINE — optional; "1" to vouch for an unrecognised multi-line
#                     value (the `vct set --allow-multiline` escape hatch).
#
# Prints NOTHING on accept. On reject, echoes the machine-stable reason slug on
# stdout ("control_char" | "github_pat_over_200" | "blob_key_eq_continuation" |
# "embedded_newline") and returns non-zero. NEVER prints the value.
#
# Check ORDER is load-bearing and MUST match the Python SSOT exactly.
_is_single_line_secret() {
    local value="$1" key_name="${2:-}" allow_multiline="${3:-0}"

    # 1. Control char anywhere → reject (not bypassed by allow_multiline).
    if _secret_shape_has_control "$value"; then
        printf 'control_char'
        return 1
    fi

    # 2. Is this a github_pat-style key? (drives the length heuristic below).
    local is_gh_pat=0
    if _secret_shape_is_github_pat_key "$key_name"; then
        is_gh_pat=1
    fi

    # 3. Trim trailing CR/LF + whitespace, then branch on single- vs multi-line.
    local trimmed
    trimmed=$(_secret_shape_rstrip_crlf_ws "$value")

    if _secret_shape_is_single_line "$trimmed"; then
        # Genuinely single-line. github_pat length heuristic (>= MAX_LEN).
        # allow_multiline does NOT bypass this — an over-long github_pat is
        # corruption regardless of caller intent.
        if [ "$is_gh_pat" -eq 1 ] && [ "${#trimmed}" -ge "$_SECRET_SHAPE_GITHUB_PAT_MAX_LEN" ]; then
            printf 'github_pat_over_200'
            return 1
        fi
        return 0
    fi

    # 4. Multi-line. Legit allowlisted format (PEM/cert/OpenSSH) → accept.
    if _secret_shape_is_legit_multiline "$trimmed"; then
        return 0
    fi

    # 5. Caller explicitly vouches for an unrecognised multi-line format.
    if [ "$allow_multiline" = "1" ]; then
        return 0
    fi

    # 6. Blob signature (column-0 KEY= after line 0) → reject.
    if _secret_shape_has_blob_signature "$trimmed"; then
        printf 'blob_key_eq_continuation'
        return 1
    fi

    # 7. Multi-line, not a legit format, no column-0 KEY= → reject generic.
    printf 'embedded_newline'
    return 1
}

# ---------------------------------------------------------------------------
# Taxonomy — the bash mirror of classify_secret_value() (Part C).
# ---------------------------------------------------------------------------

# _classify_secret_value VALUE [KEY_NAME] → echoes one taxonomy tag on stdout:
#   ok | legit_multiline | blob | length_corruption
# Never prints the value. Mirrors the Python classify_secret_value branch order.
_classify_secret_value() {
    local value="$1" key_name="${2:-}" reason ok trimmed
    reason=$(_is_single_line_secret "$value" "$key_name" 0)
    ok=$?
    trimmed=$(_secret_shape_rstrip_crlf_ws "$value")

    if [ "$ok" -eq 0 ]; then
        # Accepted: single-line or a legit PEM.
        if _secret_shape_is_legit_multiline "$trimmed"; then
            printf 'legit_multiline'
            return 0
        fi
        # Single-line: a ghp_-prefixed token whose shape is wrong (not the exact
        # 40-char classic PAT) is length_corruption — malformed but single-line,
        # nothing to split; recovery is manual.
        case "$trimmed" in
            ghp_*)
                if [[ "$trimmed" =~ $_SECRET_SHAPE_CLASSIC_PAT_RE ]]; then
                    printf 'ok'
                else
                    printf 'length_corruption'
                fi
                return 0
                ;;
        esac
        printf 'ok'
        return 0
    fi

    # Rejected: github_pat_over_200 is a single-line over-long token (nothing to
    # split → length_corruption); everything else is a splittable/malformed blob.
    if [ "$reason" = "github_pat_over_200" ]; then
        printf 'length_corruption'
    else
        printf 'blob'
    fi
    return 0
}
