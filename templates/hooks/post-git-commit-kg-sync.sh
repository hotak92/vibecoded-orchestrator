#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: spawns a background `claude` CLI subprocess (PR #171 / 0.1.7).
#   The Haiku agent invokes the weaviate-kg MCP tools (hybrid_search,
#   store_knowledge_node) — those go through claude_mcp_servers/weaviate_mcp/
#   server.py which is access-aware via VCT_KG_ACCESS_LIST. Hybrid_search
#   reads from self+shared+peers; store_knowledge_node writes to the
#   project's own collection (writes are not multi-source). This hook
#   itself does NOT query Weaviate. Env propagation: nohup/Start-Process
#   inherits the current process env, so VCT_KG_ACCESS_LIST flows into
#   the spawned `claude` process and from there into the MCP server.

# Post-git-commit hook: spawn a background Haiku agent to review the commit diff
# and update relevant KG nodes and documentation to keep everything in sync.
#
# Triggered by PostToolUse on Bash(git commit *) — only fires on successful commits.
# Non-blocking: agent runs in background.

# Don't run for orchestrator agent subprocesses
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

[ -n "$CLAUDE_CODE_DISABLE_AUTO_MEMORY" ] && exit 0

set -euo pipefail

# Silent fallback: if claude CLI isn't installed, exit clean.
# Audit F18 (2026-04-30): `command -v` is POSIX and works under any shell
# that runs this hook (bash on Linux/macOS, Git Bash on Windows). The hook
# is unreachable from cmd.exe / PowerShell because settings.json wires it
# with a `bash …` prefix; that's covered by audit F1, not here. The
# silent no-op below is correct behavior on hosts without `claude` on PATH.
command -v claude >/dev/null 2>&1 || exit 0

# Auto-detect project root from cwd (the project being committed to)
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LOG_DIR="$PROJECT_ROOT/.claude/logs"
mkdir -p "$LOG_DIR"

# Read hook input from stdin
INPUT=$(cat)

# Check if the commit actually succeeded (look for commit hash in output)
TOOL_RESPONSE=$(echo "$INPUT" | jq -r '.tool_response // ""' 2>/dev/null)
if echo "$TOOL_RESPONSE" | grep -qE '(nothing to commit|no changes added|error:|fatal:)'; then
    exit 0
fi

# Get the last commit's short hash and subject
cd "$PROJECT_ROOT"
COMMIT_HASH=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
COMMIT_MSG=$(git log -1 --format="%s" 2>/dev/null || echo "unknown")

# Debounce: skip if we already reviewed this commit
LAST_REVIEWED_FILE="$LOG_DIR/.last_reviewed_commit"
LAST_REVIEWED=$(cat "$LAST_REVIEWED_FILE" 2>/dev/null || echo "")
if [ "$COMMIT_HASH" = "$LAST_REVIEWED" ]; then
    exit 0
fi
echo "$COMMIT_HASH" > "$LAST_REVIEWED_FILE"

# Get the diff (limit to 8000 chars to keep Haiku context small)
DIFF=$(git diff HEAD~1..HEAD --stat --no-color 2>/dev/null | head -50)
DIFF_DETAIL=$(git diff HEAD~1..HEAD --no-color 2>/dev/null | head -300)

# Build the agent prompt — single-project mode (KG and docs are inside this repo)
PROMPT="Review the following git commit and update relevant knowledge graph nodes and documentation.

## Commit: ${COMMIT_HASH} — ${COMMIT_MSG}

### Files changed:
${DIFF}

### Diff (first 300 lines):
${DIFF_DETAIL}

## Instructions:
1. Read the diff carefully and identify which KG nodes (in ${PROJECT_ROOT}/knowledge/) or docs (in ${PROJECT_ROOT}/docs/) need updating
2. For each affected area:
   - If an existing KG node covers this topic: update it with the new information
   - If this introduces a NEW concept/pattern not yet documented: create a new KG node
   - If docs reference behavior that changed: update them
3. Use hybrid_search to find existing relevant nodes before creating new ones
4. Keep updates minimal and factual — just reflect what changed, don't add speculation
5. Skip trivial changes (typo fixes, formatting, test-only changes)
6. Use store_knowledge_node MCP tool to write KG nodes (they auto-sync to Weaviate)

Focus on architectural changes, new features, API changes, and pattern changes.
Skip if the commit is purely cosmetic or test-only."

# Spawn background Haiku agent via Claude CLI
# Uses --model haiku for cost efficiency, --max-turns 10 to cap effort
nohup claude -p "$PROMPT" \
    --model haiku \
    --max-turns 10 \
    --no-session-persistence \
    --allowedTools "Read,Glob,Grep,mcp__weaviate-kg__hybrid_search,mcp__weaviate-kg__store_knowledge_node,Write,Edit" \
    >> "$LOG_DIR/kg-commit-review.log" 2>&1 &

# Log the event
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"commit\":\"${COMMIT_HASH}\",\"message\":\"${COMMIT_MSG}\",\"pid\":$!}" \
    >> "$LOG_DIR/kg-commit-reviews.jsonl" 2>/dev/null || true

echo "KG sync agent spawned for commit ${COMMIT_HASH}"
