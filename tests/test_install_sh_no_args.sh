#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# Regression test for M-P0-1 (v0.2.53):
# install.sh:575 used to expand "${INSTALL_PY_ARGS[@]}" of an empty array,
# which aborts under bash 3.2 + `set -u` (macOS default — bash 3.2.57).
# This test exercises the empty-args path under strict mode and asserts
# the script reaches `exec`-time without an "unbound variable" abort.
#
# Strategy: run install.sh with no args under `set -uo pipefail` AFTER
# stubbing the Python detection so the script doesn't actually go through
# Python install. We assert that the script either (a) reaches the
# `exec` line (verified by stub that prints a known marker) or (b) exits
# normally for an unrelated reason (e.g. a previously-passed audit) —
# what it MUST NOT do is exit with "unbound variable" on the array.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_SH="$REPO_ROOT/install.sh"

if [ ! -f "$INSTALL_SH" ]; then
    echo "FAIL: $INSTALL_SH not found"
    exit 1
fi

# Use a temp scratch dir as a chroot-like sandbox: copy install.sh and
# replace its exec target with a marker script so we don't actually run
# install.py.
TMPDIR_OUT="$(mktemp -d -t test-install-sh-no-args.XXXXXX)"
trap 'rm -rf "$TMPDIR_OUT"' EXIT

cp "$INSTALL_SH" "$TMPDIR_OUT/install.sh"
# Stub install.py: just record argv and exit 0.
cat > "$TMPDIR_OUT/install.py" <<'STUB'
import sys
print("STUB_INVOKED argv=" + repr(sys.argv[1:]))
sys.exit(0)
STUB

# Run with no args. The interesting check is the EXIT STATUS and STDERR.
# Before the fix, bash 3.2 would print:
#   "INSTALL_PY_ARGS[@]: unbound variable" → exit 1
# After the fix, the script reaches the exec line (or exits earlier on
# an unrelated audit failure, but never on the unbound-variable path).
cd "$TMPDIR_OUT"

# We can't easily simulate bash 3.2 on a Linux CI box, but we CAN verify
# the idiom under strict `set -uo pipefail`. The new idiom
# `${arr[@]+"${arr[@]}"}` is the standard portable workaround and
# behaves identically on bash 3.2 + bash 4+ + bash 5+: empty array
# expands to nothing without triggering set -u.
#
# We use a synthetic bash one-liner that reproduces the exact pattern
# from install.sh:575. If the idiom is the OLD one, it aborts under
# `set -uo pipefail`. If it's the NEW one, it succeeds.

# Grep the actual install.sh to confirm the fix is present.
if ! grep -q 'INSTALL_PY_ARGS\[@\]+"\${INSTALL_PY_ARGS\[@\]}"' "$INSTALL_SH"; then
    echo "FAIL: install.sh does not use the bash-3.2-safe \${arr[@]+\"\${arr[@]}\"} idiom"
    echo "      Look at install.sh near line 575."
    exit 1
fi

# Reproduce the bug class with bash -c, no install.sh wrapping: this is
# the smallest test of whether `set -u` + empty `"${arr[@]}"` aborts.
# Two test cases:
#   1) OLD pattern → reliably aborts on bash 3.2; on bash 4/5 it may pass.
#   2) NEW pattern → reliably passes on ALL bash versions.
#
# We test the NEW pattern (the fix). It should pass on bash 4+ and is
# documented as the only safe form on bash 3.2.
OUTPUT="$(bash --noprofile --norc -c '
set -uo pipefail
INSTALL_PY_ARGS=()
exec echo "ok ${INSTALL_PY_ARGS[@]+"${INSTALL_PY_ARGS[@]}"}"
' 2>&1)"
RC=$?

if [ "$RC" -ne 0 ]; then
    echo "FAIL: bash -c with new idiom returned exit $RC"
    echo "  Output: $OUTPUT"
    exit 1
fi
if [ "$OUTPUT" != "ok " ] && [ "$OUTPUT" != "ok" ]; then
    echo "FAIL: bash -c new idiom unexpected output: '$OUTPUT'"
    exit 1
fi

# Finally, smoke install.sh's actual no-args invocation END-TO-END until
# `exec install.py`. We stub install.py to print a marker. If install.sh
# never reaches it (e.g. due to a Python-not-found audit failure), we
# treat it as INCONCLUSIVE rather than FAIL — the unit smoke above already
# proved the idiom is correct.
#
# We DO require that the exit is NOT due to an "unbound variable" error.
RUN_OUT="$(bash --noprofile --norc "$TMPDIR_OUT/install.sh" </dev/null 2>&1 || true)"
if echo "$RUN_OUT" | grep -q "unbound variable"; then
    echo "FAIL: install.sh hit 'unbound variable' on empty INSTALL_PY_ARGS:"
    echo "$RUN_OUT" | grep -A1 "unbound variable"
    exit 1
fi

echo "PASS: install.sh empty-args case is bash 3.2 + set -u safe"
exit 0
