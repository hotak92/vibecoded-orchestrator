#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# pre-compact-save.sh
# Fires BEFORE every context compaction (PreCompact event, matcher: "*" —
# v0.2.102 W6-D widened it from "auto": the vendor hooks doc gives PreCompact
# exactly two matcher values, manual and auto, and a manual /compact under
# matcher "auto" saved NOTHING, so the reinject hook replayed a stale
# snapshot after every manual compaction).
# Saves a snapshot of current working state to disk so compact-context-reinject.sh
# can restore it after compaction. Does NOT write to stdout (not added to context).
#
# v0.2.102 W6-K (owner 2026-10-11): the "Recently Changed Files (last 5 min)"
# section (which actually listed files newer than CONTEXT_STATE.md — a
# mislabelled heuristic) is replaced by "the last N modified files since the
# last compaction (or the chat's start when none)": the boundary comes from
# existing state (.claude/context/last-compact-marker, else the transcript's
# first-entry timestamp), files are listed newest-first, and N is the
# VCO_SNAPSHOT_FILELIST_N knob (default 30). The precompact_prune.py
# invocation was REMOVED the same round (owner-approved removal) — the
# script no longer ships and the bundle update orphan-deletes the installed
# copy; compact-context-reinject no longer emits a pruned-summary section.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

# Project root: the ONE shell rule (_lib/project-root.sh, must match
# vco_lib/project_root.py): CLAUDE_PROJECT_DIR, else the nearest marked VCO
# project above the cwd. None -> every write below (snapshot, marker, force-flush flag) is skipped.
# shellcheck source=_lib/project-root.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/project-root.sh"
PROJECT_DIR="$(vco_project_root)" || PROJECT_DIR=""
if [ -z "$PROJECT_DIR" ]; then
    echo "pre-compact-save: no VCO project encloses $PWD; snapshot not saved" >&2
    exit 0
fi
SNAPSHOT_FILE="$PROJECT_DIR/.claude/context/pre-compact-snapshot.md"

mkdir -p "$PROJECT_DIR/.claude/context"

# Read stdin once, up front: the file list below needs transcript_path (for
# the chat-start boundary) from the SAME payload the later steps parse. The
# hook contract delivers the full payload (including session_id and
# transcript_path) via stdin JSON; CLAUDE_* env vars are NOT populated by
# Claude Code.
HOOK_STDIN=$(cat 2>/dev/null || echo "{}")
[ -z "$HOOK_STDIN" ] && HOOK_STDIN="{}"

# Resolve python interpreter portably via the shared helper (Windows ships
# python.exe / py.exe, not python3 — see audit finding F6, 2026-04-30).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

{
    echo "# Pre-Compaction Snapshot"
    echo "Saved: $(date -Iseconds)"
    echo ""

    echo "## Modified Files (git status, top 30)"
    git -C "$PROJECT_DIR" status --short 2>/dev/null | head -30 || true
    echo ""

    echo "## Recently Modified Files (newest first, since the last compaction)"
    if [ -n "${PY:-}" ]; then
        VCO_PRECOMPACT_HOOK_STDIN="$HOOK_STDIN" "$PY" - "$PROJECT_DIR" 2>/dev/null <<'PYEOF' || true
import json, os, sys

project = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()

# The N knob (owner 2026-10-11: N=30 to start). One threshold, one home:
# this embedded listing is the only reader; the .ps1 sibling reads the same
# env var. A non-positive/unparseable value falls back to 30.
def _n():
    raw = (os.environ.get("VCO_SNAPSHOT_FILELIST_N") or "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 30

# The boundary, from existing state (in this order):
#   1. .claude/context/last-compact-marker  (epoch seconds; written by this
#      hook after every compaction — still the PREVIOUS compact's time here)
#   2. the transcript's FIRST-ENTRY timestamp (the chat's start)
#   3. no boundary: list the N most recent files outright
marker = os.path.join(project, ".claude", "context", "last-compact-marker")
boundary = None
try:
    with open(marker, "r", encoding="utf-8") as fh:
        boundary = float(fh.read().strip())
except Exception:
    boundary = None
transcript = ""
try:
    payload = json.loads(os.environ.get("VCO_PRECOMPACT_HOOK_STDIN") or "{}")
    if isinstance(payload, dict):
        transcript = str(payload.get("transcript_path") or "")
except Exception:
    transcript = ""
if boundary is None and transcript and os.path.isfile(transcript):
    try:
        with open(transcript, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
        ts = (json.loads(first) or {}).get("timestamp")
        if ts:
            from datetime import datetime, timezone
            ts = str(ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            boundary = dt.timestamp()
    except Exception:
        boundary = None

EXCLUDE_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
EXCLUDE_PARTS = (os.path.join(".claude", "worktrees"), os.path.join(".claude", "logs"))
EXTS = (".py", ".md", ".json", ".yaml")
rows = []
for root, dirs, files in os.walk(project):
    dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
    if any(p in root for p in EXCLUDE_PARTS):
        continue
    for name in files:
        if not name.endswith(EXTS):
            continue
        path = os.path.join(root, name)
        if os.path.normpath(path) == os.path.normpath(os.path.join(project, ".claude", "context", "pre-compact-snapshot.md")):
            continue  # never list the snapshot being written
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if boundary is not None and mtime <= boundary:
            continue
        rows.append((mtime, path))
rows.sort(reverse=True)
for _, path in rows[:_n()]:
    print(path)
PYEOF
    else
        # W6-K review F3 (2026-10-11): a python-less machine must not get a
        # SILENTLY empty section under a heading that promised a file list —
        # say so explicitly, in the snapshot AND on stderr. The .ps1 sibling
        # needs no interpreter for its listing (native PowerShell), so this
        # arm is .sh-only by construction.
        echo "(file list unavailable: no Python interpreter resolved -- this listing needs one; the git status above is unaffected)"
        echo "pre-compact-save: no Python interpreter resolved; the Recently Modified Files listing was skipped" >&2
    fi
    echo ""
} > "$SNAPSHOT_FILE"

# Update the last-compact marker (plain epoch seconds) — written BEFORE the
# next invocation's file list reads it, i.e. exactly after this compact.
MARKER="$PROJECT_DIR/.claude/context/last-compact-marker"
date +%s > "$MARKER" 2>/dev/null || true

# Set compact flag for diff-context-inject.sh to reset its baseline.
# session_id from stdin JSON is the canonical per-conversation key — see
# diff-context-inject.sh for the same pattern. Falls back to "default"
# only if the payload is malformed.
if [ -n "${PY:-}" ]; then
    SESSION_ID=$(echo "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
else
    SESSION_ID=""
fi
[ -z "$SESSION_ID" ] && SESSION_ID="default"
# Project-local .claude/state/ — see diff-context-inject.sh for the
# rationale (resume-across-reboot, $TMPDIR wipe immunity). Filename is
# `ctx_compact_flag_*` to namespace within the shared dir and to stay
# in lockstep with diff-context-inject.sh's reader.
SNAPSHOT_DIR="$PROJECT_DIR/.claude/state"
mkdir -p "$SNAPSHOT_DIR"
touch "$SNAPSHOT_DIR/ctx_compact_flag_${SESSION_ID}"

# V52-N (2026-06-09): signal running RL citation monitors to flush their
# accumulated answer window before the context goes away. The MCP's
# _rl_answer_monitor polls every 2 s for this sentinel and, when present,
# fires with whatever's accumulated regardless of the natural 25k-token
# threshold. Without this, every PreCompact event silently drops in-flight
# citation events (the transcript-window-based extractor can't recover
# from a compacted-away mid-answer state).
SENTINEL="$PROJECT_DIR/.claude/state/rl_monitors_force_flush.flag"
mkdir -p "$(dirname "$SENTINEL")"
date +%s > "$SENTINEL"
