#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# pre-compact-save.sh
# Fires BEFORE auto context compaction (PreCompact event, matcher: "auto").
# Saves a snapshot of current working state to disk so compact-context-reinject.sh
# can restore it after compaction. Does NOT write to stdout (not added to context).

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

# Generate pruned context summary (gentle — only large stale results)
python3 "$PROJECT_DIR/.claude/scripts/precompact_prune.py" 2>/dev/null <<< "${CLAUDE_HOOK_STDIN:-{}}" || true

# Set compact flag for diff-context-inject.sh to reset its baseline
SNAPSHOT_DIR="${TMPDIR:-/tmp}/claude_ctx_snapshots"
mkdir -p "$SNAPSHOT_DIR"
touch "$SNAPSHOT_DIR/compact_flag_${CLAUDE_SESSION_ID:-default}"
