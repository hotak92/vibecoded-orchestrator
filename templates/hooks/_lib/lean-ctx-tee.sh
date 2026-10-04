#!/usr/bin/env bash
# lean-ctx-tee.sh — LOSSLESS compression wrapper for allow-listed commands.
#
# Invoked as the rewritten Bash-tool command built by
# .claude/hooks/lean-ctx-rewrite.sh (v0.2.101 allow-list + tee/pointer
# design). Usage:
#
#   lean-ctx-tee.sh <lean-ctx-bin> <raw-dir> <cmd-file> <ttl-hours>
#
# What it does, in order:
#   1. reads the original command text from <cmd-file> (written verbatim
#      by the hook — the command never travels through re-quoting),
#   2. sweeps <raw-dir> for *.log / *.cmd files older than <ttl-hours>
#      (0 = keep forever; invalid = 168),
#   3. runs the command under `bash -c`, tee-ing the FULL raw output
#      (stdout+stderr merged, as the Bash tool itself sees it) to
#      <raw-dir>/<utc-ts>-<pid>-<cksum>.log and preserving its exit code
#      (the dir is kept 0700 and the log born 0600 — SF-1: raw output of
#      allow-listed downloads/test runs can carry credentials),
#   4. prints the output compressed via `lean-ctx -c cat <rawfile>`
#      (stdin form — no path re-quoting; does NOT depend on lean-ctx's
#      own tee_mode, which varies by version), falling back to printing
#      the raw file when compression fails: output is NEVER lost,
#   5. ends with exactly one pointer line:
#      [lean-ctx-tee] N raw lines -> M shown; full output: <path> ...
#   6. exits with the ORIGINAL command's exit code.
#
# Conservative arms (output is never the loser):
#   - <raw-dir> not creatable  -> run the command directly, uncompressed,
#     no pointer (no tee, no compression — lossless by not compressing).
#   - <cmd-file> unreadable    -> loud stderr + exit 2 (the hook wrote it
#     moments ago; a miss here is a real anomaly, never silent).
#   - raw output > 32 MiB      -> pointer line only; grep/tail the file.
#   - empty output             -> pointer line only.
#
# This is an EXECUTION-time helper, not a hook: it runs inside the user's
# Bash tool call (after the permission flow evaluated the rewritten
# command), so it carries no VCT_DISABLE_HOOKS gate — the hook upstream is
# the gate, and it never emits this wrapper when disabled.
#
# WHY a per-OS shell pair and not one shared implementation: this runs in
# the Bash tool's own shell at command-execution time, where only the
# shell itself is guaranteed (a python interpreter is NOT guaranteed on
# the tool shell's PATH in user projects). The drift-prone DATA (the
# allow-list) lives in the single lean-ctx-allowlist.txt both hooks parse;
# this control-flow pair is locked by behavioural parity tests over the
# same cases (tests/test_v02101_lean_ctx_allowlist_tee.py).
#
# MUST MATCH templates/hooks/_lib/lean-ctx-tee.ps1.
set -u

LC_BIN="${1:-}"
RAW_DIR="${2:-}"
CMD_FILE="${3:-}"
TTL_HOURS="${4:-168}"

if [ -z "$LC_BIN" ] || [ -z "$RAW_DIR" ] || [ -z "$CMD_FILE" ]; then
    echo "lean-ctx-tee: usage: lean-ctx-tee.sh <lean-ctx-bin> <raw-dir> <cmd-file> <ttl-hours>" >&2
    exit 2
fi
if ! CMD="$(cat -- "$CMD_FILE" 2>/dev/null)"; then
    echo "lean-ctx-tee: cannot read command file: $CMD_FILE" >&2
    exit 2
fi
case "$TTL_HOURS" in
    ''|*[!0-9]*) TTL_HOURS=168 ;;
esac

if ! mkdir -p -- "$RAW_DIR" 2>/dev/null; then
    # No state dir -> no tee -> no compression. Run the command as-is.
    exec bash -c "$CMD"
fi
# SF-1 (v0.2.101 review): raw OUTPUT of allow-listed curl/wget/test runs can
# carry credentials (SEC-RAW guards the command text, not the output), so the
# tee dir is 0700 (also healing a pre-SF-1 0755 dir) and the .log is created
# 0600 BEFORE any output exists — no world-readable window. Git never sees
# the dir (bundle add/update writes /.claude/ to .git/info/exclude —
# vco_lib/git_exclude.py) and the code graph classifies .claude/state/ as
# transient (vco_lib/codegraph_row_classify.py TRANSIENT_STATE_MARKER).
chmod 700 "$RAW_DIR" 2>/dev/null || true

# TTL sweep (stale tee files + orphaned cmd files from denied rewrites).
if [ "$TTL_HOURS" -gt 0 ]; then
    find "$RAW_DIR" -maxdepth 1 -type f \( -name '*.log' -o -name '*.cmd' \) \
        -mmin +"$((TTL_HOURS * 60))" -delete 2>/dev/null || true
fi

TS="$(date -u +%Y%m%dT%H%M%SZ)"
CK="$(printf '%s' "$CMD" | cksum | cut -d' ' -f1)"
RAW="$RAW_DIR/${TS}-$$-${CK}.log"

# SF-1: create the tee file 0600 AT BIRTH (umask set in a subshell only —
# the user's command runs under its own umask, so files IT creates are
# untouched). The run below re-opens the existing file; its 0600 mode holds.
if ! ( umask 077; : >"$RAW" ) 2>/dev/null; then
    # Cannot tee -> run as-is, uncompressed (lossless by not compressing).
    exec bash -c "$CMD"
fi

bash -c "$CMD" >"$RAW" 2>&1
EC=$?

RAW_BYTES="$(wc -c <"$RAW" 2>/dev/null | tr -d '[:space:]')"
[ -n "$RAW_BYTES" ] || RAW_BYTES=0
RAW_LINES="$(wc -l <"$RAW" 2>/dev/null | tr -d '[:space:]')"
[ -n "$RAW_LINES" ] || RAW_LINES=0

if [ "$RAW_BYTES" -eq 0 ]; then
    printf '[lean-ctx-tee] 0 raw lines -> 0 shown; full output: %s (kept %sh)\n' "$RAW" "$TTL_HOURS"
    exit "$EC"
fi
if [ "$RAW_BYTES" -gt 33554432 ]; then
    printf '[lean-ctx-tee] output too large to echo (%s bytes); full output: %s (kept %sh) — grep/tail that file, never re-run\n' "$RAW_BYTES" "$RAW" "$TTL_HOURS"
    exit "$EC"
fi

if COMP="$("$LC_BIN" -c cat <"$RAW" 2>/dev/null)"; then
    printf '%s\n' "$COMP"
    SHOWN="$(printf '%s\n' "$COMP" | wc -l | tr -d '[:space:]')"
else
    # Compressor failed — print the raw output verbatim (never lose it).
    cat -- "$RAW"
    SHOWN="$RAW_LINES"
fi
printf '[lean-ctx-tee] %s raw lines -> %s shown; full output: %s (kept %sh)\n' "$RAW_LINES" "$SHOWN" "$RAW" "$TTL_HOURS"
exit "$EC"
