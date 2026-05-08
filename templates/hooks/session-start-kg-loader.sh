#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# SessionStart Hook: Knowledge Graph Reference Provider
#
# Purpose: Display paths to relevant KG resources (no auto-loading)
# Date: 2026-01-29

set -e

# Configuration
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
PROJECT_NAME=$(basename "$PROJECT_DIR")

# Resolve a Python interpreter portably (python3 → python → py).
# See audit findings F5 + F6, 2026-04-30.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

# Optional RL retrieval server (Pro tier). Auto-launches if installed,
# otherwise silently no-ops — free tier ships with KG + code graph only.
# Resolve $HOME via Python if the env var is unset (cmd.exe on Windows
# doesn't expose $HOME — see audit finding F5).
USER_HOME="${HOME:-}"
if [ -z "$USER_HOME" ] && [ -n "${PY:-}" ]; then
    USER_HOME=$("$PY" -c "from pathlib import Path; print(Path.home())" 2>/dev/null || echo "")
fi
RL_LAUNCHER="${RL_SERVER_LAUNCHER:-$USER_HOME/.claude/scripts/start-rl-server.sh}"
if [ -n "$USER_HOME" ] && [ -x "$RL_LAUNCHER" ]; then
    export RL_SERVER_PORT="${RL_SERVER_PORT:-11439}"
    export RL_PROJECT_ROOT="${RL_PROJECT_ROOT:-$PROJECT_DIR}"
    bash "$RL_LAUNCHER" 2>/dev/null || true
fi

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
if [ -f "$CRON_FILE" ] && [ -n "${PY:-}" ]; then
    ACTIVE_JOBS=$("$PY" - <<'PYEOF' "$CRON_FILE"
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
