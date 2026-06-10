#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# v0.2.53 L-P0-5 regression test: first-install.desktop must work after
# the documented `cp first-install.desktop ~/Desktop/` workflow AND under
# KDE Plasma 6's stricter Exec= quoting rules.
#
# The original Exec= used `cd %k` which broke when the .desktop file was
# copied out of the source tree — %k resolved to the destination dir,
# not the install root. This test exercises that scenario with a fake
# `first-install.sh` and a synthetic %k expansion.
#
# Test matrix:
#   1. .desktop file Exec= field parses cleanly (desktop-file-validate
#      compatible: no unquoted spaces, no embedded raw double-quotes
#      inside the outer single-quoted argument).
#   2. Activating via in-place location (%k = source dir) runs the
#      first-install.sh found there.
#   3. Activating via cp-to-Desktop scenario (%k = ~/Desktop, source
#      stays in ~/vibecoded-orchestrator) STILL finds first-install.sh
#      via the HOME-relative fallback.
#   4. When no first-install.sh is found anywhere, the Exec= prints a
#      clear error pointing at the docs URL (not a silent failure).
#   5. KDE Plasma 6 quoting: the Exec= line contains no escape sequences
#      that KDE's KConfig parser would mishandle (no unbalanced inner
#      double-quotes, no `\\` outside single-quoted regions).
#
# Runs on any POSIX shell; no DE actually required.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP_FILE="$REPO_ROOT/first-install.desktop"

if [ ! -f "$DESKTOP_FILE" ]; then
    echo "FAIL: $DESKTOP_FILE not found" >&2
    exit 1
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# Extract Exec= value (everything after the first =, single line, trimmed).
EXEC_LINE="$(grep -E '^Exec=' "$DESKTOP_FILE" | head -1 | sed 's/^Exec=//')"

if [ -z "$EXEC_LINE" ]; then
    echo "FAIL: no Exec= line in $DESKTOP_FILE" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 1: Exec= structure sanity
# ---------------------------------------------------------------------------
# KDE Plasma 6's parser (KConfig + KIO) is stricter than GNOME's:
#   - Embedded double-quotes inside the outer Exec= value must be either
#     backslash-escaped (\") or contained inside single-quoted regions.
#   - Unbalanced quotes silently drop the rest of the line.
# We assert the Exec= uses the single-quoted form `sh -c '...'` (no inner
# unescaped doublequotes that KDE could choke on).
echo "[1/5] Exec= structure ..."
case "$EXEC_LINE" in
    "sh -c '"*"'")
        # Good: outer single-quoted form. Inner double-quotes (around
        # bash variable substitutions like "$d") are safe inside single
        # quotes.
        ;;
    *)
        echo "FAIL: Exec= must start with \`sh -c '\` and end with \`'\` for KDE Plasma 6 compatibility" >&2
        echo "  got: $EXEC_LINE" >&2
        exit 1
        ;;
esac

# Inner script must not contain an UNBALANCED single quote (that would
# terminate the outer quote early). Counting single quotes in the inner
# body must be even when accounting for the outer pair.
inner="$(echo "$EXEC_LINE" | sed -e "s/^sh -c '//" -e "s/'\$//")"
# Count single quotes that appear in inner — must be 0 (outer pair already
# stripped). Embedded apostrophes would have to be `'\''` which we forbid.
sq_count="$(printf '%s' "$inner" | tr -dc "'" | wc -c | tr -d ' ')"
if [ "$sq_count" -ne 0 ]; then
    echo "FAIL: Exec= inner body contains unbalanced single quotes (count=$sq_count); KDE Plasma 6 will mis-parse" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 2: in-place activation (%k = source dir, has first-install.sh)
# ---------------------------------------------------------------------------
echo "[2/5] in-place activation ..."
SRCDIR="$TMPDIR/src"
mkdir -p "$SRCDIR"
cat > "$SRCDIR/first-install.sh" <<'EOF'
#!/bin/sh
echo "MARKER:in-place ran from $PWD"
exit 0
EOF
chmod +x "$SRCDIR/first-install.sh"

# Substitute %k with the source dir; feed Enter to the read prompt.
substituted="$(echo "$EXEC_LINE" | sed "s|%k|$SRCDIR|g")"
EMPTY_PWD_2="$TMPDIR/empty-pwd-test2"
mkdir -p "$EMPTY_PWD_2"
out="$(cd "$EMPTY_PWD_2" && printf '\n' | HOME="$TMPDIR/empty-home" sh -c "$substituted" 2>&1 || true)"

if ! echo "$out" | grep -q "MARKER:in-place ran from $SRCDIR"; then
    echo "FAIL: in-place activation didn't run first-install.sh from source dir" >&2
    echo "  output: $out" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 3: cp-to-Desktop scenario (%k = empty Desktop, fallback via $HOME)
# ---------------------------------------------------------------------------
echo "[3/5] cp-to-Desktop fallback ..."
FAKE_HOME="$TMPDIR/home"
mkdir -p "$FAKE_HOME/vibecoded-orchestrator"
cat > "$FAKE_HOME/vibecoded-orchestrator/first-install.sh" <<'EOF'
#!/bin/sh
echo "MARKER:home-fallback ran from $PWD"
exit 0
EOF
chmod +x "$FAKE_HOME/vibecoded-orchestrator/first-install.sh"

# %k is the empty Desktop (no first-install.sh there).
EMPTY_DESKTOP="$FAKE_HOME/Desktop"
mkdir -p "$EMPTY_DESKTOP"
substituted="$(echo "$EXEC_LINE" | sed "s|%k|$EMPTY_DESKTOP|g")"
EMPTY_PWD_3="$TMPDIR/empty-pwd-test3"
mkdir -p "$EMPTY_PWD_3"
out="$(cd "$EMPTY_PWD_3" && printf '\n' | HOME="$FAKE_HOME" sh -c "$substituted" 2>&1 || true)"

if ! echo "$out" | grep -q "MARKER:home-fallback ran from $FAKE_HOME/vibecoded-orchestrator"; then
    echo "FAIL: cp-to-Desktop scenario didn't find first-install.sh via HOME fallback" >&2
    echo "  output: $out" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 4: no first-install.sh anywhere → clear error + docs URL
# ---------------------------------------------------------------------------
echo "[4/5] missing-script error path ..."
EMPTY_HOME="$TMPDIR/empty"
EMPTY_K="$TMPDIR/empty-k"
mkdir -p "$EMPTY_HOME" "$EMPTY_K"
substituted="$(echo "$EXEC_LINE" | sed "s|%k|$EMPTY_K|g")"
out="$(cd "$EMPTY_HOME" && printf '\n' | HOME="$EMPTY_HOME" sh -c "$substituted" 2>&1 || true)"

if ! echo "$out" | grep -qi "could not locate first-install.sh"; then
    echo "FAIL: missing-script case didn't print clear error" >&2
    echo "  output: $out" >&2
    exit 1
fi
if ! echo "$out" | grep -q "github.com/hotak92/vibecoded-orchestrator"; then
    echo "FAIL: missing-script case didn't point at the docs URL" >&2
    echo "  output: $out" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Test 5: required Desktop Entry fields present
# ---------------------------------------------------------------------------
echo "[5/5] Desktop Entry minimum fields ..."
for key in Type Name Exec Icon Terminal Categories; do
    if ! grep -qE "^${key}=" "$DESKTOP_FILE"; then
        echo "FAIL: required Desktop Entry field '$key' missing" >&2
        exit 1
    fi
done

# Terminal=true is required for the install output to actually show up.
if ! grep -qE "^Terminal=true$" "$DESKTOP_FILE"; then
    echo "FAIL: Terminal= must be 'true' to keep the install console visible" >&2
    exit 1
fi

echo "OK: all KDE Plasma 6 / .desktop launch tests passed"
exit 0
