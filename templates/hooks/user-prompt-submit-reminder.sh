#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Context preservation reminder - triggers on output volume (work units).
#
# v0.2.102 W6-F: the counter COUNTS again. Pre-W6-F this hook hardwired
# WORDS_THIS_TURN=0 (UserPromptSubmit payloads carry no tool_name and the
# runtime never populates CLAUDE_TOOL_NAME — the "defensive default" comment
# below it admitted "today it always evaluates 0"), so the checkpoint and
# staleness nudge could never fire on any session. The counter is now the
# session's cumulative WORK UNITS from the live transcript, scanned
# INCREMENTALLY (per-session byte offset) by the same
# .claude/scripts/claude_token_counter.py the KG-update nudge uses — one
# home, no second counting implementation. Thresholds keep the retired
# word-scale's documented token equivalents: 300_000 work units for the
# checkpoint (was "200K words (~300K tokens)"), a 180_000-unit delta for
# the CONTEXT_STATE-staleness window (was 120K words).
set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"

# Hook input contract (v2.1.x): JSON on stdin, NOT $CLAUDE_TOOL_NAME or
# $CLAUDE_SESSION_ID env vars. Reading those env vars under `set -u` aborts
# the hook with "unbound variable" before the staleness check ever runs —
# that was a long-standing latent bug (confirmed empirically 2026-05-08).
# We drain stdin once, parse session_id from the JSON payload, and use
# `${VAR:-}` defaults so the script can run on any event without aborting.
# session_id is the canonical per-conversation key; falling back to
# "default" only on malformed JSON keeps concurrent sessions from sharing
# state files (PR #176 cross-OS sweep: hooks read session_id from the
# stdin JSON payload, never the CLAUDE_SESSION_ID env var — the hook
# runner does not populate it, so an env-var read silently collapses
# every session onto "default").
HOOK_STDIN=$(cat 2>/dev/null || echo "")
if [ -n "${PY:-}" ]; then
    SESSION_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', '') or 'default')
except Exception:
    print('default')
" 2>/dev/null || echo "default")
else
    SESSION_ID="default"
fi
[ -z "$SESSION_ID" ] && SESSION_ID="default"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")
CONTEXT_FILE="$PROJECT_DIR/.claude/CONTEXT_STATE.md"
SESSION_LOG="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/claude-session-${PROJECT_NAME}-${SESSION_ID}"
OFFSET_FILE="${SESSION_LOG}.scan_offset"

# Initialize session tracking. SESSION_LOG holds the session's cumulative
# WORK UNITS (see the header); OFFSET_FILE holds the transcript byte offset
# the incremental scanner consumed up to.
if [ ! -f "$SESSION_LOG" ]; then
    echo "0" > "$SESSION_LOG"
fi

# Accumulate the work units the transcript gained since the last prompt.
# The counter module resolves from the HOOK'S OWN bundle (../scripts) or
# $CLAUDE_PROJECT_DIR/.claude/scripts — never cwd (W6-F, same ladder as
# kg-update-nudge.sh). A transcript to count with no importable counter is
# a broken install: the embedded Python prints a loud stderr line and exits
# 1, which `set -e` propagates (non-blocking for UserPromptSubmit; only
# exit 2 blocks a prompt). No Python at all is environmental — the counter
# silently keeps its last value, as before.
TOTAL_WORDS=$(cat "$SESSION_LOG")
if [ -n "${PY:-}" ]; then
    HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    TOTAL_WORDS=$(printf '%s' "$HOOK_STDIN" | \
        _UPSR_HOOK_DIR="$HOOK_DIR" \
        _UPSR_STATE="$SESSION_LOG" _UPSR_OFFSET="$OFFSET_FILE" \
        "$PY" -c '
import json, os, sys

state_file = os.environ.get("_UPSR_STATE", "")
offset_file = os.environ.get("_UPSR_OFFSET", "")

def _read_num(path):
    try:
        with open(path, "r") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0

def _write_num(path, value):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(value))
        os.replace(tmp, path)
    except OSError:
        pass

total = _read_num(state_file)
try:
    payload = json.loads(sys.stdin.read() or "{}")
except Exception:
    payload = {}
transcript = ""
if isinstance(payload, dict):
    transcript = payload.get("transcript_path") or ""
if not transcript or not os.path.exists(transcript):
    print(total)  # nothing to count: keep the accumulated value
    sys.exit(0)

_counter_dir = ""
for _d in (
    os.path.normpath(os.path.join(os.environ.get("_UPSR_HOOK_DIR", "x"),
                                  os.pardir, "scripts")),
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "x"),
                 ".claude", "scripts"),
):
    if os.path.isfile(os.path.join(_d, "claude_token_counter.py")):
        _counter_dir = _d
        break
if not _counter_dir:
    print("[user-prompt-submit-reminder] BROKEN INSTALL: .claude/scripts/"
          "claude_token_counter.py is missing; the session work counter "
          "cannot run. Re-run the VCO bundle update to restore it.",
          file=sys.stderr)
    sys.exit(1)
sys.path.insert(0, _counter_dir)
try:
    from claude_token_counter import TranscriptScanner
except ImportError:
    print("[user-prompt-submit-reminder] BROKEN INSTALL: .claude/scripts/"
          "claude_token_counter.py failed to import; the session work "
          "counter cannot run. Re-run the VCO bundle update.",
          file=sys.stderr)
    sys.exit(1)

try:
    prev_off = _read_num(offset_file)
    res = TranscriptScanner().scan(transcript, start_offset=prev_off)
    # A scan that did not start at the persisted offset (rotated transcript
    # or the bounded tail window of a first scan over an oversized file) is
    # a fresh basis — its delta replaces the cumulative, never adds to it.
    base = total if res.start_offset_used == prev_off else 0
    total = base + res.work_units_total
    # W6-F fix-round: persist the end_offset UNCONDITIONALLY -- 0 IS
    # a valid offset (a transcript rotated to empty consumed nothing and
    # must reset the resume point to 0, else content regrown past the stale
    # offset is skipped mid-line). The old `or prev_off` kept the stale
    # offset after a rotate-to-empty -- the exact rotation defect class the
    # nudge fixed with its `_new_scan_offset is not None` shape; mirrored
    # here (the reminder has no "no scan ran" branch -- reaching this line
    # means a scan ran and end_offset is always an int).
    end = res.end_offset
    if end != prev_off:
        _write_num(offset_file, end)  # offset first: a kill between the two
        _write_num(state_file, total)  # writes under-counts (safe direction)
except Exception:
    # A scanning hiccup must never break the prompt: keep the old total.
    pass
print(total)
')
fi

# Trigger at 300K work units (the documented "~300K tokens" scale the retired
# 200K-word threshold approximated)
if [ "$TOTAL_WORDS" -ge 300000 ]; then
    MARKER="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/claude-ctx-warn-${PROJECT_NAME}-${SESSION_ID}"
    [ -f "$MARKER" ] && exit 0

    echo ""
    echo "💾 Session checkpoint (~300K work units this session)"
    echo "   • /refresh-context - Update KG if topic shifted"
    echo "   • Update CONTEXT_STATE.md with findings"
    echo "   • Consider new session before context compression"
    echo ""

    touch "$MARKER"
    exit 0
fi

# CONTEXT_STATE staleness — counter-based, one-shot per ~180K-work-unit window
# (the retired 120K-word window's token equivalent) since the LAST nudge or
# last actual edit (whichever is more recent).
#
# Replaces the prior time-based 30-min staleness check that fired 4-5x
# during long deep-work sessions. The new condition is "enough work has
# accumulated that a CONTEXT_STATE refresh is genuinely useful," not
# "the file is old."
#
# The staleness marker file is keyed by both PROJECT_NAME and SESSION_ID
# so concurrent Claude Code sessions on the same project don't stomp on
# each other's counter (the same concurrency fix as PR #176 applied to
# 11 other hooks: key per-session state by the session_id parsed from
# the stdin JSON payload, not by project alone).
STALENESS_MARKER="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/claude-ctx-staleness-${PROJECT_NAME}-${SESSION_ID}"
LAST_FIRE_WORDS=0
if [ -f "$STALENESS_MARKER" ]; then
    LAST_FIRE_WORDS=$(cat "$STALENESS_MARKER" 2>/dev/null || echo 0)
    if [ -f "$CONTEXT_FILE" ]; then
        CTX_MTIME=$(stat -c %Y "$CONTEXT_FILE" 2>/dev/null || stat -f %m "$CONTEXT_FILE" 2>/dev/null || echo 0)
        MARKER_MTIME=$(stat -c %Y "$STALENESS_MARKER" 2>/dev/null || stat -f %m "$STALENESS_MARKER" 2>/dev/null || echo 0)
        if [ "$CTX_MTIME" -gt "$MARKER_MTIME" ]; then
            echo "$TOTAL_WORDS" > "$STALENESS_MARKER"
            LAST_FIRE_WORDS="$TOTAL_WORDS"
        fi
    fi
fi
DELTA=$(( TOTAL_WORDS - LAST_FIRE_WORDS ))
if [ "$DELTA" -ge 180000 ]; then
    echo ""
    echo "💾 ~${DELTA} work units since last CONTEXT_STATE update."
    echo "   Append a 1-2 line progress note now (what shipped + what's next),"
    echo "   before context grows further or /compact lands."
    echo ""
    echo "$TOTAL_WORDS" > "$STALENESS_MARKER"
fi

exit 0
