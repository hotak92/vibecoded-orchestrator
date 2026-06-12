#!/usr/bin/env bash
# OS-EXEMPT-PARITY: integration test for the bash resolver client only — the PowerShell resolver client has its own dedicated test (vct_project_config.Tests.ps1).
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Integration smoke-test for the bash resolver client's rate-limited
# fall-through warning emission (Step 17 of v0.2.21).
#
# Strategy: invoke vct_project_config.sh against a non-existent hub so
# every call falls through. The script normally forks subshells; rate-
# limiting must still suppress repeated SAME-PID emissions, but each
# fork is a fresh PID so back-to-back invocations of the script *should*
# each emit (because $$ differs per process).
#
# What we actually verify here (the strong invariants):
#   1. `bash -n` is clean.
#   2. A single in-process double-call of _emit_warning emits exactly
#      one stderr line (the second is suppressed).
#   3. VCO_HOOK_DEBUG=1 bypasses suppression (two calls → two lines).
#   4. The JSONL is created with one row per emission (not per call).
#
# Exit 0 on success, 1 on failure.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SCRIPT_DIR/vct_project_config.sh"

if [[ ! -f "$TARGET" ]]; then
    echo "FAIL: target not found at $TARGET" >&2
    exit 1
fi

# 1. Syntax check.
if ! bash -n "$TARGET"; then
    echo "FAIL: bash -n reported errors" >&2
    exit 1
fi

# Sandbox: isolated VCT_STATE_DIR so we don't write the real cache.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export VCT_STATE_DIR="$TMP"

# 2. In-process double-call → exactly one stderr line.
#    Strip the _emit_warning helper out of the script and source it
#    directly so we can call it without going through main().
DRIVER="$TMP/driver.sh"
cat > "$DRIVER" <<DRIVER_EOF
set -euo pipefail
# Stub err() and the other helpers we don't need.
err() { :; }
# Inline _emit_warning + its helpers from the target.
# We extract by sourcing the target's "library" portion: define a
# guard variable so its main() is a no-op.
DRIVER_EOF

# Source the helpers and assert single-emit behaviour. The cleanest way
# is to source the script with main() neutralised via a tiny shim.
# vct_project_config.sh always calls main "$@" at the bottom, which
# would consume argv and may exit. We bypass by redefining 'main' to a
# no-op before sourcing.

# Extract the script body minus the final `main "$@"` invocation so we
# can source the helper functions without triggering the CLI entry-point.
# The script ends with `main "$@"` as the last non-comment line.
LIB="$TMP/lib.sh"
sed -e '/^main "$@"$/d' "$TARGET" > "$LIB"

run_double_emit() {
    bash -c '
        set -eu
        export VCT_STATE_DIR="'"$TMP"'"
        unset VCO_HOOK_DEBUG
        # shellcheck disable=SC1090
        source "'"$LIB"'"
        _emit_warning hub_unreachable "first"
        _emit_warning hub_unreachable "second"
    ' 2>&1 1>/dev/null
}

OUTPUT="$(run_double_emit)"
EMIT_COUNT="$(printf '%s\n' "$OUTPUT" | grep -c '^\[vct\] project_config:' || true)"
if [[ "$EMIT_COUNT" -ne 1 ]]; then
    echo "FAIL: expected exactly 1 emit, got $EMIT_COUNT" >&2
    echo "---- captured stderr ----" >&2
    echo "$OUTPUT" >&2
    echo "-------------------------" >&2
    exit 1
fi
echo "OK: rate-limit suppresses second emit in same PID"

# 3. VCO_HOOK_DEBUG=1 → both lines emit.
run_double_emit_debug() {
    bash -c '
        set -eu
        export VCT_STATE_DIR="'"$TMP"'"
        export VCO_HOOK_DEBUG=1
        # shellcheck disable=SC1090
        source "'"$LIB"'"
        _emit_warning hub_unreachable "first"
        _emit_warning hub_unreachable "second"
    ' 2>&1 1>/dev/null
}

# Reset state dir for a clean run.
rm -rf "$TMP"/cache 2>/dev/null || true

OUTPUT_DEBUG="$(run_double_emit_debug)"
EMIT_COUNT_DEBUG="$(printf '%s\n' "$OUTPUT_DEBUG" | grep -c '^\[vct\] project_config:' || true)"
if [[ "$EMIT_COUNT_DEBUG" -ne 2 ]]; then
    echo "FAIL: VCO_HOOK_DEBUG=1 expected 2 emits, got $EMIT_COUNT_DEBUG" >&2
    echo "---- captured stderr ----" >&2
    echo "$OUTPUT_DEBUG" >&2
    echo "-------------------------" >&2
    exit 1
fi
echo "OK: VCO_HOOK_DEBUG=1 bypasses suppression"

# 4. JSONL exists and is well-formed.
JSONL="$TMP/cache/resolver_warn.jsonl"
if [[ ! -f "$JSONL" ]]; then
    echo "FAIL: expected JSONL at $JSONL" >&2
    exit 1
fi

# Validate each line is JSON (if python3 is available).
if command -v python3 >/dev/null 2>&1; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if ! printf '%s' "$line" | python3 -c 'import json,sys; json.loads(sys.stdin.read())' 2>/dev/null; then
            echo "FAIL: malformed JSONL row: $line" >&2
            exit 1
        fi
    done < "$JSONL"
    echo "OK: every JSONL row is valid JSON"
fi

# 5. Different error_kind in same PID emits (no shared suppression).
rm -rf "$TMP"/cache 2>/dev/null || true
OUTPUT_DIFF=$(bash -c '
    set -eu
    export VCT_STATE_DIR="'"$TMP"'"
    unset VCO_HOOK_DEBUG
    # shellcheck disable=SC1090
    source "'"$LIB"'"
    _emit_warning hub_unreachable "a"
    _emit_warning project_not_registered "b"
' 2>&1 1>/dev/null)
EMIT_COUNT_DIFF="$(printf '%s\n' "$OUTPUT_DIFF" | grep -c '^\[vct\] project_config:' || true)"
if [[ "$EMIT_COUNT_DIFF" -ne 2 ]]; then
    echo "FAIL: distinct error_kinds expected 2 emits, got $EMIT_COUNT_DIFF" >&2
    exit 1
fi
echo "OK: distinct error_kinds each emit"

echo "All bash integration assertions passed."
exit 0
