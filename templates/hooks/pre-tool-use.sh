#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: read-side delegator on the KG-suggestion path (PR #171 / 0.1.7).
#   The "KG search suggestion" branch (Edit/Write only, see section 5
#   below) calls .claude/scripts/kg-search; that wrapper invokes
#   search_knowledge.py which honors VCT_KG_ACCESS_LIST through the
#   shared helper. Other branches (SSRF guard, shell-injection scan,
#   Build Anchor, file backup, tool logging) do not touch KG/codegraph.
#   Env propagation: $(...) subshells inherit env. No centralization
#   needed in this hook itself.

# Pre-tool-use hook — Security enforcement + tool logging + KG suggestion
# Triggers: Before all tool uses
# Actions:
#   1. SSRF guard (WebFetch / fetch_page to private IPs)
#   2. Shell injection scan (network-fetch-to-shell patterns)
#   3. Tool call logging (TOUCAN dataset)
#   4. Build Anchor Protocol: track reads, block unread Write/Edit
#   5. File backup before Write/Edit on existing files
#   6. KG search suggestion before Edit/Write

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Source emit-context.sh ONLY if the file exists. If the helper is
# missing (partial install or just-after-clone before _lib/ is fully
# populated), the hook still runs its other branches (logging,
# security guards). The KG-suggestion branch below checks
# `command -v emit_additional_context` before calling it.
# We deliberately do NOT trail with `|| true`: a syntax error inside
# an existing helper is a real bug we want surfaced.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op (logging+guards skipped)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# v0.2.29: prefer Claude Code's canonical $CLAUDE_PROJECT_DIR (the active
# workspace the launcher hands us — source of truth for per-project hooks).
# Fall back to SCRIPT_DIR/../.. for ad-hoc invocations (manual runs, tests)
# that don't set the env var. This matches the canonical hook contract
# while staying robust for non-Claude-Code callers.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1/$2/$3) are EMPTY because $CLAUDE_TOOL_NAME etc.
# don't exist as env vars. Without this, every toucan log entry is
# {"query":"","chosen_tool":"","tool_args":null} — silently broken.
# Verified empirically 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
TOOL_NAME=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('tool_name', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
TOOL_ARGS=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(json.dumps(d.get('tool_input', {})))
except Exception:
    print('{}')
" 2>/dev/null || echo "{}")
USER_MESSAGE=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('user_message', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
SESSION_ID_FROM_STDIN=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
# V52-L.2 Fix 1: parse subagent identity from stdin payload. Per A5 audit
# (knowledge/research/claude-code-leak-agent-architecture.md + 2026-06-09
# official docs review), PreToolUse hooks DO fire for subagent tool calls;
# the JSON payload carries agent_id + agent_type so handlers can tell
# parent activity apart from subagent activity. Pre-V52-L.2 we ignored
# both fields, so every TOUCAN row looked like it came from the same
# session_id regardless of which agent ran the tool — A3's measurement
# artifact (26-vs-83 gap) was just this confusion. Empty string when the
# field is absent (parent context) which is what TOUCAN consumers expect.
AGENT_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('agent_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")
AGENT_TYPE=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('agent_type', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

SESSION_ID="${SESSION_ID_FROM_STDIN:-${CLAUDE_SESSION_ID:-$(date +%Y%m%d_%H)}}"
# Per-session dedup state lives under the project's .claude/state/ rather
# than $TMPDIR so it survives reboots + launcher restarts (Claude Code
# persists session_id across restarts via the resume feature). $TMPDIR may
# be cleared on reboot, breaking dedup mid-session. .claude/state/ is
# gitignored and wiped only by PostCompact (correct semantic — context
# truly resets at compaction).
SESSION_STATE_DIR="$PROJECT_ROOT/.claude/state"
SESSION_READS_FILE="$SESSION_STATE_DIR/reads_${SESSION_ID}.txt"
BACKUP_DIR="$SESSION_STATE_DIR/tool_backups"
SECURITY_LOG="$PROJECT_ROOT/.claude/logs/security_events.jsonl"

mkdir -p "$PROJECT_ROOT/.claude/logs"
mkdir -p "$SESSION_STATE_DIR" 2>/dev/null || true
mkdir -p "$BACKUP_DIR" 2>/dev/null || true

# Best-effort 14-day GC of stale per-session reads files. Sessions that
# haven't been touched in two weeks are almost certainly abandoned;
# keeping them around just wastes inodes. Errors suppressed: this is a
# housekeeping pass, not a correctness step.
find "$SESSION_STATE_DIR" -maxdepth 1 -name 'reads_*.txt' -mtime +14 -delete 2>/dev/null || true

# === HELPER: safe JSON field extraction ===
_get_field() {
    "$PY" -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(d.get('$1', ''))
except Exception:
    print('')
" <<< "$TOOL_ARGS" 2>/dev/null || echo ""
}

# === TOOL CALL LOGGING ===
# USER_MESSAGE may contain newlines, quotes, JSON metacharacters; TOOL_ARGS
# is JSON but isn't trustworthy when concatenated into another JSON string.
# Build the JSONL line through Python so every field is properly escaped.
# Audit fix 2026-05-07.
TOUCAN_LOG="$PROJECT_ROOT/.claude/logs/toucan_dataset.jsonl"
TOUCAN_JSONL=$(USER_MESSAGE_FOR_PY="$USER_MESSAGE" \
    TOOL_NAME_FOR_PY="$TOOL_NAME" \
    TOOL_ARGS_FOR_PY="$TOOL_ARGS" \
    AGENT_ID_FOR_PY="$AGENT_ID" \
    AGENT_TYPE_FOR_PY="$AGENT_TYPE" \
    SESSION_ID_FOR_PY="$SESSION_ID" \
    "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
tool_args_raw = os.environ.get("TOOL_ARGS_FOR_PY", "")
try:
    tool_args_val = json.loads(tool_args_raw) if tool_args_raw else None
except (json.JSONDecodeError, TypeError):
    tool_args_val = tool_args_raw
# V52-L.2 Fix 1: include agent_id / agent_type so TOUCAN consumers can
# differentiate parent vs subagent rows. Empty string when the field is
# absent (parent context). session_id is repeated here for the same
# reason — TOUCAN rows currently lack it, making per-session analysis
# require a separate join against the hook contract.
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "query": os.environ.get("USER_MESSAGE_FOR_PY", ""),
    "chosen_tool": os.environ.get("TOOL_NAME_FOR_PY", ""),
    "tool_args": tool_args_val,
    "session_id": os.environ.get("SESSION_ID_FOR_PY", ""),
    "agent_id": os.environ.get("AGENT_ID_FOR_PY", ""),
    "agent_type": os.environ.get("AGENT_TYPE_FOR_PY", ""),
}))
' 2>/dev/null)
if [ -n "$TOUCAN_JSONL" ]; then
    printf '%s\n' "$TOUCAN_JSONL" >> "$TOUCAN_LOG" 2>/dev/null || true
fi

# === 1. SSRF GUARD ===
if [[ "$TOOL_NAME" == "WebFetch" ]]; then
    URL=$(_get_field "url")
    if [[ -n "$URL" ]]; then
        # Whitelisted local services (Weaviate, Ollama, code-embed, Gradio).
        # SearXNG (:8888) and the mcp__search__fetch_page tool both
        # removed in v0.2.11 (see PR-14a). Search MCP now exposes only
        # `search_papers` which uses OpenAlex+arXiv HTTP directly — its
        # outbound HTTP doesn't go through this WebFetch SSRF guard.
        if echo "$URL" | grep -qE "(localhost:(8081|8082|11435|11440|7860)|127\.0\.0\.1:(8081|8082|11435|11440|7860))" 2>/dev/null; then
            : # whitelisted — fall through
        elif echo "$URL" | grep -qE "(localhost|127\.|10\.[0-9]+\.[0-9]+\.[0-9]+|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.|192\.168\.[0-9]+\.|169\.254\.[0-9]+\.|0\.0\.0\.0|::1)" 2>/dev/null; then
            # Block messages route to stderr — see comment in bash-
            # security branch below for why (Claude Code drops plain
            # stdout from PreToolUse hooks).
            {
                echo "🔒 SSRF guard: '$URL' targets a private/internal network address."
                echo "   Whitelisted localhost services: Weaviate (:8081), Ollama (:11435), code-embed (:11440), Gradio (:7860)"
                echo "   To allow additional services, add to whitelist in .claude/hooks/pre-tool-use.sh"
            } >&2
            echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"ssrf_blocked\",\"url\":\"$URL\"}" >> "$SECURITY_LOG" 2>/dev/null || true
            exit 2
        fi
    fi
fi

# === 2. SHELL INJECTION SCAN (Bash tool) ===
if [[ "$TOOL_NAME" == "Bash" ]]; then
    CMD=$(_get_field "command")
    INJECTION_FOUND=""

    # Pattern A: network fetch piped directly to a shell interpreter
    if echo "$CMD" | grep -qiE "(curl|wget)\s[^|]+\|\s*(ba)?sh\b" 2>/dev/null; then
        INJECTION_FOUND="network fetch piped to shell"
    fi

    # Pattern B: eval + network fetch (remote code execution)
    if [[ -z "$INJECTION_FOUND" ]] && echo "$CMD" | grep -qiE "eval\s+[\"\$\(]*(curl|wget)" 2>/dev/null; then
        INJECTION_FOUND="eval + network fetch"
    fi

    # Pattern C: base64-decoded pipe to shell
    if [[ -z "$INJECTION_FOUND" ]] && echo "$CMD" | grep -qiE "base64\s+-d.*\|\s*(ba)?sh\b" 2>/dev/null; then
        INJECTION_FOUND="base64-decoded pipe to shell"
    fi

    if [[ -n "$INJECTION_FOUND" ]]; then
        # Block messages route to stderr — see bash-security branch.
        {
            echo "🚨 Shell injection guard: detected '$INJECTION_FOUND' in Bash command."
            echo "   Blocked command preview: ${CMD:0:120}"
            echo "   If this is intentional, run the command manually in a terminal."
        } >&2
        echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"shell_injection_blocked\",\"pattern\":\"$INJECTION_FOUND\",\"cmd_preview\":\"${CMD:0:80}\"}" >> "$SECURITY_LOG" 2>/dev/null || true
        exit 2
    fi

    # Extended security scan via bash_security.py
    SECURITY_SCRIPT="$PROJECT_ROOT/.claude/scripts/bash_security.py"
    if [[ -f "$SECURITY_SCRIPT" ]]; then
        SECURITY_RESULT=$(echo "$CMD" | "$PY" "$SECURITY_SCRIPT" 2>&1)
        SECURITY_EXIT=$?
        if [[ "$SECURITY_EXIT" -eq 2 ]]; then
            # Emit the block message to STDERR (not stdout). Claude
            # Code's PreToolUse hook runner discards plain stdout —
            # only JSON-shaped stdout under `hookSpecificOutput.
            # additionalContext` is surfaced (see PR #168, the same
            # fix class applied to the KG-suggestion branch below).
            # An exit-2 hook with no stderr renders as "hook error:
            # No stderr output" on the user side, which is what the
            # 2026-05-20 hook-spam report described. Route the human-
            # readable message to stderr so the harness displays it.
            {
                echo "🚨 Bash security scanner blocked this command:"
                echo "   $SECURITY_RESULT"
            } >&2
            echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"bash_security_blocked\",\"detail\":\"${SECURITY_RESULT:0:200}\",\"cmd_preview\":\"${CMD:0:80}\"}" >> "$SECURITY_LOG" 2>/dev/null || true
            exit 2
        fi
    fi
fi

# === 3. BUILD ANCHOR PROTOCOL: Track reads ===
if [[ "$TOOL_NAME" == "Read" ]]; then
    FILE_PATH=$(_get_field "file_path")
    if [[ -n "$FILE_PATH" ]]; then
        echo "$FILE_PATH" >> "$SESSION_READS_FILE" 2>/dev/null || true
    fi
    exit 0
fi

# === 4. BUILD ANCHOR PROTOCOL + FILE BACKUP: Write/Edit checks ===
if [[ "$TOOL_NAME" == "Write" ]] || [[ "$TOOL_NAME" == "Edit" ]]; then
    FILE_PATH=$(_get_field "file_path")

    if [[ -n "$FILE_PATH" ]]; then
        if [[ -f "$FILE_PATH" ]]; then
            # Existing file: check Build Anchor — WRITE ONLY.
            #
            # The anchor gate (block a modification of an existing file that
            # wasn't Read this session) is enforced for `Write` but NOT for
            # `Edit`. Rationale:
            #   * `Write` blind-overwrites the whole file and can be issued
            #     without ever reading it — the genuinely dangerous case the
            #     anchor protects against (clobbering an unseen file).
            #   * `Edit` is already gated by Claude Code's built-in
            #     read-before-edit rule (an Edit needs an exact old_string
            #     match, unobtainable without reading). Re-enforcing it here
            #     was redundant AND a false-positive source: this hook's own
            #     session-reads ledger (exact path match) can diverge from the
            #     harness's internal file-state tracking and spuriously block a
            #     legitimate Edit. So we defer Edit's read-before-edit to the
            #     harness and only anchor `Write`.
            if [[ "$TOOL_NAME" == "Write" ]]; then
                ALREADY_READ=0
                if [[ -f "$SESSION_READS_FILE" ]]; then
                    grep -qxF "$FILE_PATH" "$SESSION_READS_FILE" 2>/dev/null && ALREADY_READ=1 || true
                fi

                if [[ "$ALREADY_READ" -eq 0 ]]; then
                    BASENAME=$(basename "$FILE_PATH")
                    # Block messages route to stderr — see bash-security
                    # branch comment.
                    {
                        echo "⚠️  Build Anchor Protocol: '$BASENAME' has not been Read this session."
                        echo "    Use the Read tool on this file before overwriting it with Write."
                    } >&2
                    echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"anchor_blocked\",\"file\":\"$FILE_PATH\",\"tool\":\"Write\"}" >> "$SECURITY_LOG" 2>/dev/null || true
                    exit 2
                fi
            fi

            # Backup existing file before modification
            mkdir -p "$BACKUP_DIR"
            TIMESTAMP=$(date +%Y%m%d_%H%M%S)
            ENCODED=$(echo "$FILE_PATH" | tr '/' '__' | tr ' ' '_')
            cp "$FILE_PATH" "$BACKUP_DIR/${TIMESTAMP}__${ENCODED}" 2>/dev/null || true
            # Cleanup backups older than 24h
            find "$BACKUP_DIR" -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null || true
        fi

        # Track this file so subsequent writes don't need another Read
        echo "$FILE_PATH" >> "$SESSION_READS_FILE" 2>/dev/null || true
    fi
fi

# === 5. KG SEARCH SUGGESTION (Edit/Write only) ===
if [[ "$TOOL_NAME" != "Edit" ]] && [[ "$TOOL_NAME" != "Write" ]]; then
    exit 0
fi

CONCEPTS=$(echo "$USER_MESSAGE" | grep -oE "(caching|authentication|database|API|search|optimization|validation|testing|deployment|VRAM|quantization|inference|embedding|MCP|agent|workflow|pattern)" | head -3 | tr '\n' ' ')

if [ -n "$CONCEPTS" ]; then
    # V52-J (v0.2.52): switched from kg-search → rl_kg_search.py so this
    # hook shares the canonical chokepoint with the pre-edit-context-
    # inject hook + the MCP hybrid_search tool. Same Weaviate fan-out,
    # same RL rerank, same v3 retrieval-event emit. Pre-V52-J this branch
    # called kg-search (search_knowledge.py CLI), which until Edit B
    # produced zero telemetry — switching here closes the redundancy at
    # the same time as Edit B closes the silent hole.
    #
    # rl_kg_search.py --hook-format emits headers of the shape
    #   "KG: <title> | <node_type> | score=<n.nn> | <body...>"
    # Title (not file_path) is what we surface to the user since it's
    # the human-readable identifier; the pre-edit hook's dedup logic
    # also keys on title.
    #
    # Venv resolution mirrors pre-edit-context-inject.sh — uses the
    # shared _lib/resolve-vco-venv.sh helper so we never accidentally
    # activate the USER's project venv (which lacks weaviate-client).
    # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
    resolve_vco_venv_python "$SCRIPT_DIR"
    VENV="${VCO_VENV_PYTHON:-}"
    RL_SCRIPT="$PROJECT_ROOT/claude_mcp_servers/scripts/rl_kg_search.py"

    MATCHES=""
    MATCH_COUNT=0
    if [ -n "$VENV" ] && [ -f "$RL_SCRIPT" ]; then
        # Extract only the per-result HEADER lines (start with "KG: " and
        # carry the " | " separator) — strips body chunks that would
        # otherwise inflate the suggestion. Filter out the "no-results"
        # sentinel rl_kg_search emits when nothing matched.
        MATCHES=$(VCT_SESSION_ID="$SESSION_ID" "$VENV" "$RL_SCRIPT" "$CONCEPTS" --limit 3 --hook-format 2>/dev/null \
            | grep "^KG: " \
            | grep -v "^KG: no-results" \
            | head -3 || echo "")
        MATCH_COUNT=$(printf '%s\n' "$MATCHES" | grep -c "^KG: " 2>/dev/null || echo "0")
    fi

    if [ "$MATCH_COUNT" -ge 2 ]; then
        # PreToolUse hooks must wrap LLM-bound stdout in
        # `hookSpecificOutput.additionalContext` — plain stdout is silently
        # discarded by Claude Code's hook runner. Pre-fork-sweep this
        # branch printed plaintext that never reached the LLM. Same fix
        # class as pre-edit-context-inject (PR #168). The shared helper
        # in _lib/emit-context.sh handles the JSON envelope, the 10k char
        # cap, and (defense-in-depth) the whitespace-only-content guard.
        SUGGESTION_TEXT=$(printf '\n💡 Found %s related patterns for: %s\n%s\n\n   Search more: '\''Search knowledge graph for [concept]'\''\n' "$MATCH_COUNT" "$CONCEPTS" "$(echo "$MATCHES" | sed 's/^/   /')")
        # Defense: if the helper failed to load, skip emission rather
        # than crash. Other branches of this hook are unaffected.
        if command -v emit_additional_context >/dev/null 2>&1; then
            emit_additional_context "$SUGGESTION_TEXT" PreToolUse
        fi
    fi
fi
