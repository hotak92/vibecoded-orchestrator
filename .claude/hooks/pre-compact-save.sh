#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# pre-compact-save.sh
# Fires BEFORE auto context compaction (PreCompact event, matcher: "auto").
# Saves a snapshot of current working state to disk so compact-context-reinject.sh
# can restore it after compaction. Does NOT write to stdout (not added to context).

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
SNAPSHOT_FILE="$PROJECT_DIR/.claude/context/pre-compact-snapshot.md"

mkdir -p "$PROJECT_DIR/.claude/context"

{
    echo "# Pre-Compaction Snapshot"
    echo "Saved: $(date -Iseconds)"
    echo ""

    echo "## Modified Files (git status, top 30)"
    git -C "$PROJECT_DIR" status --short 2>/dev/null | head -30 || true
    echo ""

    echo "## Recently Changed Files (last 5 min)"
    find "$PROJECT_DIR" -newer "$PROJECT_DIR/.claude/CONTEXT_STATE.md" \
        \( -name "*.py" -o -name "*.md" -o -name "*.json" -o -name "*.yaml" \) \
        -not -path "*/.git/*" -not -path "*/__pycache__/*" \
        -not -path "*/.venv/*" -not -path "*/node_modules/*" \
        -not -path "*/.claude/worktrees/*" -not -path "*/.claude/logs/*" \
        2>/dev/null | head -15 || true
    echo ""
} > "$SNAPSHOT_FILE"

# Read stdin once and reuse. The hook contract delivers the full payload
# (including session_id and transcript_path) via stdin JSON; CLAUDE_*
# env vars are NOT populated by Claude Code.
HOOK_STDIN=$(cat 2>/dev/null || echo "{}")
[ -z "$HOOK_STDIN" ] && HOOK_STDIN="{}"

# Generate pruned activity summary scoped to "since last compact".
# precompact_prune.py reads transcript_path from the JSON stdin.
# Resolve python interpreter portably via the shared helper (Windows ships
# python.exe / py.exe, not python3 — see audit finding F6, 2026-04-30).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
if [ -n "${PY:-}" ] && [ -f "$PROJECT_DIR/.claude/scripts/precompact_prune.py" ]; then
    echo "$HOOK_STDIN" | "$PY" "$PROJECT_DIR/.claude/scripts/precompact_prune.py" 2>/dev/null || true
fi

# Update the last-compact marker AFTER the prune script ran (so the next
# invocation correctly scopes to messages emitted between this compact and
# the next one). Marker is plain epoch seconds.
MARKER="$PROJECT_DIR/.claude/context/last-compact-marker"
date +%s > "$MARKER" 2>/dev/null || true

# Set compact flag for diff-context-inject.sh to reset its baseline.
# session_id from stdin JSON is the canonical per-conversation key — see
# diff-context-inject.sh for the same pattern. Falls back to "default"
# only if the payload is malformed.
SESSION_ID=$(echo "$HOOK_STDIN" | python3 -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
[ -z "$SESSION_ID" ] && SESSION_ID="default"
SNAPSHOT_DIR="${TMPDIR:-/tmp}/claude_ctx_snapshots"
mkdir -p "$SNAPSHOT_DIR"
touch "$SNAPSHOT_DIR/compact_flag_${SESSION_ID}"
