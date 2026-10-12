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

# Get the diff (limit to 8000 chars to keep Haiku context small).
# Root-commit safe (v0.2.102 W6-N F-3): a first commit has no HEAD~1, and
# `git diff HEAD~1..HEAD` printed NOTHING there -- the fresh-repo first
# build was reviewed with an empty diff. A root commit is shown against the
# empty tree by `git show`; every other commit keeps the diff against its
# FIRST parent (`git show` on a merge would print a combined diff instead).
#
# `|| true` on every capture (W6-N review MEDIUM-3): on a diff bigger than
# the pipe buffer `head` exits first and `git` dies of SIGPIPE (141); under
# `set -o pipefail` the capture then fails and `set -e` killed the hook
# before the reviewer was spawned. The truncated text is exactly what is
# wanted, so the capture's status is not an error.
if git rev-parse --verify --quiet HEAD~1 >/dev/null 2>&1; then
    DIFF=$(git diff HEAD~1 HEAD --stat --no-color 2>/dev/null | head -50) || true
    DIFF_DETAIL=$(git diff HEAD~1 HEAD --no-color 2>/dev/null | head -300) || true
else
    DIFF=$(git show --format= --stat --no-color HEAD 2>/dev/null | head -50) || true
    DIFF_DETAIL=$(git show --format= --no-color HEAD 2>/dev/null | head -300) || true
fi

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

# Spawn the background reviewer through the shared backend chain
# (v0.2.102 W6-K, owner ruling 2026-10-11): Haiku by default; when Haiku is
# unavailable (quota/429/auth — a REAL signal, latched in the summary
# breaker), fall back to the gateway flash models the local model gateway
# actually serves (glm-5.3-flash -> deepseek-v4.1-flash -> qwen3.8-flash),
# then to OpenAI when the user enabled OpenAI for summaries. One chain, one
# home: .claude/scripts/kg_reviewer_backend.py (it reuses summary_backends'
# classification/breaker). Every fallback is logged there.
REVIEWER_CHAIN="$PROJECT_ROOT/.claude/scripts/kg_reviewer_backend.py"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
# v0.2.102 W6-R (loud-fail rule): a missing python or a missing shipped chain
# script is a BROKEN install. It is named on stderr, in kg-commit-review.log
# and in this commit's kg-commit-reviews.jsonl row (`broken_install`) — this
# hook runs async, so the log/ledger lines are the ones a user can find.
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
if [ -n "$BROKEN_INSTALL" ]; then
    BROKEN_LINE="post-git-commit-kg-sync: broken install: $BROKEN_INSTALL - the KG reviewer runs the plain Haiku spawn without the backend fallback chain; repair it with the project's bundle update (launcher: project Settings -> Update bundle)"
    printf '%s\n' "$BROKEN_LINE" >&2
    printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$BROKEN_LINE" \
        >> "$LOG_DIR/kg-commit-review.log" 2>/dev/null || true
fi
if [ -z "$BROKEN_INSTALL" ]; then
    PROMPT_FILE="$(mktemp "${TMPDIR:-/tmp}/vco-kg-reviewer.XXXXXX")"
    printf '%s' "$PROMPT" > "$PROMPT_FILE"
    # No --max-turns here: the turn budget is ONE knob owned by the chain
    # (DEFAULT_MAX_TURNS / VCO_KG_REVIEW_MAX_TURNS, v0.2.102 W6-N F-4).
    nohup "$PY" "$REVIEWER_CHAIN" run \
        --prompt-file "$PROMPT_FILE" \
        --log "$LOG_DIR/kg-commit-review.log" \
        >> "$LOG_DIR/kg-commit-review.log" 2>&1 &
    REVIEWER_PID=$!
else
    # Broken/partial install (python or the chain script missing), named
    # above: the pre-W6-K direct Haiku spawn is KEPT so the commit is still
    # reviewed. It hides nothing — the breakage is reported on every commit
    # in three places before this spawn, and dropping the review would lose
    # the KG update for no gain in visibility. With no python this branch cannot ask
    # the chain for the turn budget, so it mirrors it: the default 30 must
    # match DEFAULT_MAX_TURNS in .claude/scripts/kg_reviewer_backend.py, and
    # VCO_KG_REVIEW_MAX_TURNS must parse as resolve_max_turns() there does:
    # plain ASCII digits only (no sign, blanks or `_`), positive, within
    # int64 (`-gt` refuses a larger value); anything else falls back to 30.
    # A parity test pins the three parsers to each other.
    MAX_TURNS="${VCO_KG_REVIEW_MAX_TURNS:-}"
    case "$MAX_TURNS" in
        ''|*[!0-9]*) MAX_TURNS=30 ;;
    esac
    [ "$MAX_TURNS" -gt 0 ] 2>/dev/null || MAX_TURNS=30
    nohup claude -p "$PROMPT" \
        --model haiku \
        --max-turns "$MAX_TURNS" \
        --no-session-persistence \
        --allowedTools "Read,Glob,Grep,mcp__weaviate-kg__hybrid_search,mcp__weaviate-kg__store_knowledge_node,Write,Edit" \
        >> "$LOG_DIR/kg-commit-review.log" 2>&1 &
    REVIEWER_PID=$!
fi

# Log the event
BROKEN_FIELD=""
if [ -n "$BROKEN_INSTALL" ]; then
    # JSON-escape the reason (paths may carry `\` or `"`); no python here.
    BROKEN_ESC="$(printf '%s' "$BROKEN_INSTALL" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
    BROKEN_FIELD=",\"broken_install\":\"${BROKEN_ESC}\""
fi
echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"commit\":\"${COMMIT_HASH}\",\"message\":\"${COMMIT_MSG}\",\"pid\":${REVIEWER_PID}${BROKEN_FIELD}}" \
    >> "$LOG_DIR/kg-commit-reviews.jsonl" 2>/dev/null || true

# No stdout: PostToolUse plain stdout is dropped per the v2.1.x
# contract, AND this hook runs `async: true` (fire-and-forget). Any
# emit here would be doubly discarded — leave it silent so future
# audits do not flag it as a candidate for envelope routing.
