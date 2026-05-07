# shellcheck shell=bash
# stderr-cap.sh — structural defense against runaway hook stderr.
#
# Background (2026-05-07): a bug in diff-context-inject.sh leaked content
# of removed lines into a `for cl in $changed_lines` loop, causing the
# inner integer test `[ "$line_num" -eq "$cl" ]` to error
# "integer expression expected" thousands of times per hook fire. The
# stderr was captured uncapped into the JSONL `attachment.stderr` field,
# producing 14-23 MB single lines that triggered Claude Code's
# main-thread streaming stall (anthropics/claude-code#23053 / #51560)
# and froze the GUI.
#
# The specific bug is fixed, but no hook should be *able* to emit
# unbounded stderr. This helper enforces a 1 MB cap structurally.
#
# Usage (one line at the top of every hook, after the shebang and any
# `set -e` / env-scrub block):
#   . "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
#
# Behaviour:
# - First STDERR_CAP_BYTES (default 1048576 = 1 MB) of stderr is passed
#   through to the real stderr. Anything beyond is silently drained
#   (writer never receives SIGPIPE, so hooks keep running).
# - Hooks emitting <1 MB are unaffected.
# - Override per-hook: `STDERR_CAP_BYTES=262144 . _lib/stderr-cap.sh`
#   (set the env var BEFORE sourcing).
# - Bypass entirely: `STDERR_CAP_DISABLE=1` — useful for debugging a hook
#   that is *supposed* to emit a lot.
#
# Why this is safe:
# - The drain pattern (`head -c N; cat >/dev/null`) prevents SIGPIPE on
#   the writing side, so legitimate hooks that briefly exceed the cap
#   keep running cleanly.
# - Process substitution `>(…)` requires bash; we don't support sh hooks.
# - The cap applies for the lifetime of the bash process. Background
#   children inherit the redirected fd 2 and are also capped.

# Allow opt-out for debugging.
if [ "${STDERR_CAP_DISABLE:-0}" = "1" ]; then
    return 0 2>/dev/null || exit 0
fi

# Default 1 MB. Real legitimate hook stderr is < 10 KB; 1 MB leaves 100x
# headroom for unusual cases (verbose pytest, podman pulls) while being
# 14-23x below the observed freeze threshold.
: "${STDERR_CAP_BYTES:=1048576}"

# Apply the cap. The trailing `cat >/dev/null` drains the rest of the
# pipe so the writer never sees EPIPE / SIGPIPE.
exec 2> >({ head -c "$STDERR_CAP_BYTES" >&2; cat >/dev/null; })
