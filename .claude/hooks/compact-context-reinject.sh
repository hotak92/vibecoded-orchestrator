#!/bin/bash
# compact-context-reinject.sh
# Fires on SessionStart with matcher "compact"
# Re-injects critical session context that compaction may have dropped.
# Stdout is added to Claude's context automatically by the CLI.
#
# LITM ordering (Lost-In-The-Middle): Claude weights start and end of context
# most heavily. Place most critical info at start AND end.
#
# Total budget: ~250 lines max. CONTEXT_STATE gets full allocation, others capped.

# Scrub sensitive env vars (this hook doesn't need credentials)
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN 2>/dev/null

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

# 1. Current task state -- START position (most critical, uncapped)
if [ -f "$PROJECT_DIR/.claude/CONTEXT_STATE.md" ]; then
    echo "## Current Task State (re-injected after compaction)"
    cat "$PROJECT_DIR/.claude/CONTEXT_STATE.md"
    echo ""
fi

# 2. Active plan summary -- MIDDLE position (cap: 30 lines)
LATEST_PLAN=$(ls -t "$PROJECT_DIR/.claude/context/plans/"*.md 2>/dev/null | grep -v archive | head -1)
if [ -n "$LATEST_PLAN" ]; then
    echo "## Active Plan: $(basename "$LATEST_PLAN")"
    head -30 "$LATEST_PLAN"
    echo ""
fi

# 3. Recent git activity -- MIDDLE position (cap: 8 commits)
echo "## Recent Commits"
git -C "$PROJECT_DIR" log --oneline -8 2>/dev/null || true
echo ""

# 4. Pre-compaction snapshot -- END position (cap: 50 lines)
#    Contains git status + recently changed files, saved by pre-compact-save.sh
SNAPSHOT="$PROJECT_DIR/.claude/context/pre-compact-snapshot.md"
if [ -f "$SNAPSHOT" ]; then
    echo "## Pre-Compaction Snapshot"
    head -50 "$SNAPSHOT"
    echo ""
fi
