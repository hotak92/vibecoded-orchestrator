#!/usr/bin/env bash
# Parity: kept in step with post-git-commit-kg-sync.ps1 (its UTF-8 BOM serves Windows
# PowerShell 5.1 only; every behaviour must match the .ps1 sibling).
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: spawns a background `claude` CLI subprocess (PR #171 / 0.1.7).
#   The reviewer invokes the weaviate-kg MCP tools (hybrid_search,
#   store_knowledge_node) — those go through claude_mcp_servers/weaviate_mcp/
#   server.py which is access-aware via VCT_KG_ACCESS_LIST. Hybrid_search
#   reads from self+shared+peers; store_knowledge_node writes to the
#   project's own collection (writes are not multi-source). This hook
#   itself does NOT query Weaviate. Env propagation: nohup/Start-Process
#   inherits the current process env, so VCT_KG_ACCESS_LIST flows into
#   the spawned `claude` process and from there into the MCP server.

# Post-git-commit hook: spawn the background KG reviewer for the commit.
#
# Triggered by PostToolUse on Bash(git commit *) — only fires on successful
# commits. Non-blocking: the reviewer runs detached.
#
# v0.2.103 (owner rulings 2026-10-15): this is a THIN wrapper. Everything
# else lives in ONE home, .claude/scripts/kg_reviewer_backend.py `run`
# (with vco_lib.kg_reviewer_prompt / _model / _guard): the commit capture
# and research-first prompt, the model choice (one machine-wide setting,
# default Sonnet; non-flash gateway fallback or skip), the knowledge/-only
# tool scope, the post-run validator that reverts forbidden edits, the
# isolated `kg-sync: <hash> <subject>` commit of the reviewer's own files,
# and the recursion guard (a `kg-sync:` commit is never reviewed).

# Don't run for orchestrator agent subprocesses
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

[ -n "$CLAUDE_CODE_DISABLE_AUTO_MEMORY" ] && exit 0

set -euo pipefail

# No `claude`-on-PATH check here (v0.2.103 R7 F6): whether a reviewer can
# run is the backend's decision, in ONE home. Without the CLI its chain
# logs every claude rung as unavailable and can still reach the
# consent-gated OpenAI rung; an early silent exit here made that rung dead
# and left no log line or ledger row.

# Auto-detect project root from cwd (the project being committed to)
PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
LOG_DIR="$PROJECT_ROOT/.claude/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/kg-commit-review.log"

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

REVIEWER_CHAIN="$PROJECT_ROOT/.claude/scripts/kg_reviewer_backend.py"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
# Loud-fail rule: a missing python or a missing shipped reviewer script is a
# BROKEN install. It is named on stderr, in kg-commit-review.log and in this
# commit's kg-commit-reviews.jsonl row (`broken_install`) — this hook runs
# async, so the log/ledger lines are the ones a user can find. v0.2.103: the
# commit is then NOT reviewed. The guard that keeps the reviewer inside
# knowledge/ and reverts forbidden edits is Python, so an unguarded spawn
# would bypass the very boundaries the owner requires; the pre-v0.2.103
# direct `claude` spawn (and its turn-budget mirror) is retired for that
# reason, superseded by the guarded run.
BROKEN_INSTALL=""
if [ -z "${PY:-}" ]; then
    if [ -f "$SCRIPT_DIR/_lib/find-python.sh" ]; then
        BROKEN_INSTALL="no Python interpreter found (_lib/find-python.sh resolved none)"
    else
        BROKEN_INSTALL="$SCRIPT_DIR/_lib/find-python.sh is missing, so no Python interpreter was resolved"
    fi
fi
if [ ! -f "$REVIEWER_CHAIN" ]; then
    BROKEN_INSTALL="${BROKEN_INSTALL:+$BROKEN_INSTALL; }$REVIEWER_CHAIN is missing"
fi

REVIEWER_PID=null
if [ -n "$BROKEN_INSTALL" ]; then
    BROKEN_LINE="post-git-commit-kg-sync: broken install: $BROKEN_INSTALL - the KG reviewer did NOT review this commit (its guarded run needs both); repair it with the project's bundle update (launcher: project Settings -> Update bundle)"
    printf '%s\n' "$BROKEN_LINE" >&2
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BROKEN_LINE" \
        >> "$LOG_FILE" 2>/dev/null || true
else
    # No --max-turns / --model here: each is ONE knob owned by the chain
    # (VCO_KG_REVIEW_MAX_TURNS; VCO_KG_REVIEW_MODEL / launcher Preferences).
    nohup "$PY" "$REVIEWER_CHAIN" run \
        --project-root "$PROJECT_ROOT" \
        --commit "$COMMIT_HASH" \
        --log "$LOG_FILE" \
        >> "$LOG_FILE" 2>&1 &
    REVIEWER_PID=$!
fi

# Log the event
BROKEN_FIELD=""
if [ -n "$BROKEN_INSTALL" ]; then
    # JSON-escape the reason (paths may carry `\` or `"`); no python here.
    BROKEN_ESC="$(printf '%s' "$BROKEN_INSTALL" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
    BROKEN_FIELD=",\"broken_install\":\"${BROKEN_ESC}\""
fi
MSG_ESC="$(printf '%s' "$COMMIT_MSG" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"commit\":\"${COMMIT_HASH}\",\"message\":\"${MSG_ESC}\",\"pid\":${REVIEWER_PID}${BROKEN_FIELD}}" \
    >> "$LOG_DIR/kg-commit-reviews.jsonl" 2>/dev/null || true

# No stdout: PostToolUse plain stdout is dropped per the v2.1.x
# contract, AND this hook runs `async: true` (fire-and-forget). Any
# emit here would be doubly discarded — leave it silent so future
# audits do not flag it as a candidate for envelope routing.
