#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# SessionStart Hook: Knowledge Graph Reference Provider
#
# Purpose: Display paths to relevant KG resources (no auto-loading)
# Date: 2026-01-29

set -e

# Configuration
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

# Auto-launch rl_server for this project (port 11439, state/ in this repo)
export RL_SERVER_PORT=11439
export RL_PROJECT_ROOT="${RL_PROJECT_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
# RL server (Pro tier only - not included in base orchestrator)

# Display available resources (paths only, no content loading)
cat << EOF

📚 Knowledge Graph Resources Available:

   Scripts:
   - .claude/scripts/kg-search search "query" [--type TYPE] [--tags TAGS]
   - .claude/scripts/kg-info info "Node Title"
   - .claude/scripts/kg-info connections "Node Title"

   Recent Work:
   - .claude/scripts/kg-search recent --days 7

   Project Context:
   - .claude/CONTEXT_STATE.md (current task state)
   - .claude/context/plans/ (active plans - reference when needed)
   - .claude/context/plans/archive/ (completed plans)

💡 Use kg-search to find relevant nodes when needed

EOF

# Re-inject enabled /loop jobs from cron-jobs.json
CRON_FILE="$PROJECT_DIR/.claude/cron-jobs.json"
if [ -f "$CRON_FILE" ]; then
    ACTIVE_JOBS=$(python3 - <<'PYEOF' "$CRON_FILE"
import sys, json
path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
    active = [j for j in data.get('jobs', []) if j.get('enabled')]
    if active:
        print("⚡ Active /loop jobs (re-run these to restore recurring tasks):")
        for j in active:
            print(f"  {j['command']}")
        print("")
except Exception:
    pass
PYEOF
)
    if [ -n "$ACTIVE_JOBS" ]; then
        echo "$ACTIVE_JOBS"
    fi
fi

exit 0
