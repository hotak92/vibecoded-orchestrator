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

# v0.2.70 Streams C+E: shared helpers for canonical session-id, the unified
# seen-store dedup, and the code-graph retrieval used by the NEW Read(code) +
# Grep(symbol) injection branches below. Sourced only if present (partial-install
# tolerance); the new branches no-op gracefully when a helper is missing.
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# shellcheck source=_lib/seen-store.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/seen-store.sh" ] && . "$SCRIPT_DIR/_lib/seen-store.sh"
# shellcheck source=_lib/codegraph-query.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/codegraph-query.sh" ] && . "$SCRIPT_DIR/_lib/codegraph-query.sh"
# v0.2.77 Part 9 task 2: shared TTL result-cache used by codegraph_query_block.
# Sourced only if present (partial-install tolerance).
# shellcheck source=_lib/query-cache.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/query-cache.sh" ] && . "$SCRIPT_DIR/_lib/query-cache.sh"
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
# v0.2.76 P5 (hook-latency): parse the stdin payload with EXACTLY ONE Python
# interpreter (was SIX — tool_name, tool_input, user_message, session_id,
# agent_id, agent_type each re-read + re-decoded the same JSON). This hook
# fires on the `*` matcher — EVERY tool call — so it was the single biggest
# turn-blocking hook (measured ~137ms p50, ~90ms of it redundant interpreter
# cold-starts). Same NUL-delimited single-decode pattern proven in
# post-file-edit.sh (HK-1, v0.2.73): one decoder emits all six fields
# NUL-terminated (a trailing NUL after EACH field, incl. the last), read back
# with a single loop so an embedded newline in any field survives. Malformed
# stdin → all fields default to "" (or "{}" for tool_input), preserving the
# soft-fail contract. This is a PRELUDE consolidation, NOT a retrieval or
# behaviour change: the parsed values are byte-identical to the six-spawn form.
TOOL_NAME=""
TOOL_ARGS="{}"
USER_MESSAGE=""
SESSION_ID_FROM_STDIN=""
# V52-L.2 Fix 1: subagent identity (agent_id + agent_type). Per A5 audit
# (knowledge/research/claude-code-leak-agent-architecture.md + 2026-06-09
# official docs review), PreToolUse hooks DO fire for subagent tool calls;
# the JSON payload carries agent_id + agent_type so handlers can tell
# parent activity apart from subagent activity. Empty string when the field
# is absent (parent context) which is what TOUCAN consumers expect.
AGENT_ID=""
AGENT_TYPE=""
_PTU_IDX=0
while IFS= read -r -d '' _PTU_VAL; do
    case "$_PTU_IDX" in
        0) TOOL_NAME="$_PTU_VAL" ;;
        1) TOOL_ARGS="$_PTU_VAL" ;;
        2) USER_MESSAGE="$_PTU_VAL" ;;
        3) SESSION_ID_FROM_STDIN="$_PTU_VAL" ;;
        4) AGENT_ID="$_PTU_VAL" ;;
        5) AGENT_TYPE="$_PTU_VAL" ;;
    esac
    _PTU_IDX=$((_PTU_IDX + 1))
done < <(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    fields = [
        d.get('tool_name', '') or '',
        json.dumps(d.get('tool_input', {}) or {}),
        d.get('user_message', '') or '',
        d.get('session_id', '') or '',
        d.get('agent_id', '') or '',
        d.get('agent_type', '') or '',
    ]
except Exception:
    fields = ['', '{}', '', '', '', '']
# Trailing NUL after EACH field so the reader loop terminates cleanly.
sys.stdout.write(''.join(str(f) + '\0' for f in fields))
" 2>/dev/null)
# Defensive: a truncated decode (0 iterations) leaves the defaults above,
# but re-coerce an emptied tool_input to a valid JSON object.
[ -z "$TOOL_ARGS" ] && TOOL_ARGS="{}"

# v0.2.70 Stream E: unify session-id resolution with the other hooks via
# vco_hook_session_id (parse + path-safety sanitise). SESSION_ID_RAW preserves
# the trustworthy-vs-untrustworthy distinction for the unified reads ledger the
# injectors consult ("" / "default" → no shared reads store). SESSION_ID keeps
# the legacy date fallback so the Build Anchor protocol's own reads_*.txt still
# has a stable per-hour key even without a clean session_id.
if command -v vco_hook_session_id >/dev/null 2>&1; then
    SESSION_ID_RAW="$(vco_hook_session_id "$HOOK_STDIN")"
else
    SESSION_ID_RAW="$(printf '%s' "$SESSION_ID_FROM_STDIN" | tr -cd 'A-Za-z0-9_-')"
fi
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
# HK-4 (v0.2.75) accepted-scatter: one of 4 per-hook GC sweeps (uniform 14d);
# a shared sweeper is optional and deliberately SKIPPED to keep hooks
# single-file. See pre-edit-context-inject.sh for the full rationale.
find "$SESSION_STATE_DIR" -maxdepth 1 -name 'reads_*.txt' -mtime +14 -delete 2>/dev/null || true
# v0.2.70 Stream E (SF-1): same 14-day GC for the INJECTOR reads store
# (seen_reads_*.txt — distinct from the Build-Anchor reads_*.txt above).
find "$SESSION_STATE_DIR" -maxdepth 1 -name 'seen_reads_*.txt' -mtime +14 -delete 2>/dev/null || true

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
#
# D-14 (v0.2.75): the TOUCAN log lives at .claude/logs/toucan_dataset.jsonl
# — gitignored (`.claude/logs/*.jsonl`), so it never reaches a commit and
# is NOT on the check-no-secrets scan surface. It still needs bounding
# because it durably duplicated whole Write `content` / whole Bash
# `command` lines with no truncation, redaction, or size cap: any secret
# that ever transited a tool call outlived the scrubbed original here.
# Two mitigations below (mirrored in pre-tool-use.ps1, must-match):
#   1. Truncate the known content-bearing fields (content / new_string /
#      old_string / command) to _TOUCAN_FIELD_CAP chars before serialize.
#   2. Size-cap + rotate the JSONL (oldest rows dropped) at
#      _TOUCAN_MAX_BYTES so it can't grow unbounded.
# We do NOT add the log to a scanner surface: post-fix it holds only
# truncated fragments, and it's gitignored — an ignore-with-rationale, not
# a scan target.
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
# D-14: cap each known content-bearing field at this many chars. ~2000 is
# enough to keep the tool-selection signal TOUCAN is for while making the
# log useless as a secret-exfil target. MUST MATCH pre-tool-use.ps1.
_TOUCAN_FIELD_CAP = 2000
_TOUCAN_TRUNC_FIELDS = ("content", "new_string", "old_string", "command")

def _truncate_known_fields(val):
    """Truncate the known large content fields in a tool_input dict.
    Non-dict inputs (or non-str field values) pass through unchanged."""
    if not isinstance(val, dict):
        return val
    out = {}
    for k, v in val.items():
        if k in _TOUCAN_TRUNC_FIELDS and isinstance(v, str) and len(v) > _TOUCAN_FIELD_CAP:
            out[k] = v[:_TOUCAN_FIELD_CAP] + "…[truncated by D-14]"
        else:
            out[k] = v
    return out

tool_args_raw = os.environ.get("TOOL_ARGS_FOR_PY", "")
try:
    tool_args_val = json.loads(tool_args_raw) if tool_args_raw else None
except (json.JSONDecodeError, TypeError):
    tool_args_val = tool_args_raw
tool_args_val = _truncate_known_fields(tool_args_val)
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
    # D-14: size-cap + rotate. When the log exceeds _TOUCAN_MAX_BYTES
    # (~5 MB), keep only the newest _TOUCAN_KEEP_LINES rows (oldest
    # dropped). Best-effort: any failure leaves the file as-is (a hook
    # must never crash the tool call over housekeeping). MUST MATCH
    # pre-tool-use.ps1.
    _TOUCAN_MAX_BYTES=5242880
    _TOUCAN_KEEP_LINES=2000
    _tsize=""
    if _tsize=$(stat -c '%s' "$TOUCAN_LOG" 2>/dev/null); then :
    elif _tsize=$(stat -f '%z' "$TOUCAN_LOG" 2>/dev/null); then :
    else _tsize=""; fi
    if [ -n "$_tsize" ] && [ "$_tsize" -gt "$_TOUCAN_MAX_BYTES" ] 2>/dev/null; then
        _trot="$TOUCAN_LOG.rot.tmp"
        if tail -n "$_TOUCAN_KEEP_LINES" "$TOUCAN_LOG" > "$_trot" 2>/dev/null; then
            mv -f "$_trot" "$TOUCAN_LOG" 2>/dev/null || rm -f "$_trot" 2>/dev/null
        else
            rm -f "$_trot" 2>/dev/null || true
        fi
    fi
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

# === v0.2.70 Stream C: shared code-graph injection for Read(code)/Grep(symbol).
# One concern, one home — both surfaces call this. Queries the shared
# _lib/codegraph-query.sh helper, dedups through the shared _lib/seen-store.sh,
# emits via the PreToolUse JSON envelope. Soft-fails to nothing.
#   $1 query        — the codegraph search query (module name / symbol)
#   $2 exclude_path — path to grep -v out (self-reference); "" if none
#   $3 label        — header label for the injected block
#   $4 anchor       — optional file path / symbol forwarded as --anchor so the
#                     CLI's shared pipeline biases the rerank toward
#                     call-linked / same-module / shared-type code (v0.2.72 P2)
_cg_inject() {
    local _q="$1" _excl="$2" _label="$3" _anchor="${4:-}"
    command -v codegraph_query_block >/dev/null 2>&1 || return 0
    [ -n "$_q" ] || return 0

    # v0.2.72 P6: per-session inject VOLUME cap. The seen-store dedups by
    # IDENTITY (same entity is not re-injected) but a long session navigating
    # many DISTINCT entities still injects unboundedly. Bound the TOTAL number
    # of EMITTED injections per session_id (VCO_CG_INJECT_CAP, default 40).
    #
    # Two-part contract so the cap counts REAL injections (not query attempts
    # that dedup to nothing): (1) a read-only capped-check short-circuits BEFORE
    # the heavier codegraph subprocess once the cap is hit — emitting a ONE-LINE
    # note EXACTLY ONCE; (2) the counter is incremented ONLY on an actual emit
    # (bottom of the function). Soft-fail OPEN throughout: an unkeyable session
    # (untrustworthy id) or any counter error runs UNCAPPED — a broken cap must
    # never break injection.
    local _cnt=""
    if command -v vco_cg_inject_count_path >/dev/null 2>&1; then
        _cnt="$(vco_cg_inject_count_path "$SESSION_ID_RAW" "$PROJECT_ROOT")"
    fi
    if [ -n "$_cnt" ] && command -v vco_cg_inject_capped >/dev/null 2>&1 \
        && vco_cg_inject_capped "$_cnt"; then
        # Cap reached — stop injecting. Emit the one-line note once per session.
        if command -v vco_cg_inject_note_once >/dev/null 2>&1 \
            && command -v emit_additional_context >/dev/null 2>&1 \
            && vco_cg_inject_note_once "$SESSION_ID_RAW" "$PROJECT_ROOT"; then
            emit_additional_context "[codegraph injection cap reached for this session]" PreToolUse
        fi
        return 0
    fi

    local _raw
    _raw="$(codegraph_query_block "$_q" "" 2 "$_excl" "$_anchor" 2>/dev/null || true)"
    [ -n "$_raw" ] || return 0
    local _inj="" _rd=""
    if command -v vco_seen_store_path >/dev/null 2>&1; then
        _inj="$(vco_seen_store_path inject "$SESSION_ID_RAW" "$PROJECT_ROOT")"
        _rd="$(vco_seen_store_path reads "$SESSION_ID_RAW" "$PROJECT_ROOT")"
    fi
    if command -v vco_filter_seen_blocks >/dev/null 2>&1; then
        _raw="$(vco_filter_seen_blocks "$_raw" "$_inj" "$_rd")"
    fi
    case "$_raw" in
        *[![:space:]]*)
            if command -v emit_additional_context >/dev/null 2>&1; then
                emit_additional_context "[${_label}]:"$'\n'$'\n'"$_raw" PreToolUse
                # Count this REAL injection toward the per-session cap. Only
                # reached on a non-empty post-dedup block that actually emits.
                if [ -n "$_cnt" ] && command -v vco_cg_inject_record >/dev/null 2>&1; then
                    vco_cg_inject_record "$_cnt"
                fi
            fi
            ;;
    esac
}

# === 3. BUILD ANCHOR PROTOCOL: Track reads + v0.2.70 code-file inject ===
if [[ "$TOOL_NAME" == "Read" ]]; then
    FILE_PATH=$(_get_field "file_path")
    if [[ -n "$FILE_PATH" ]]; then
        # Build Anchor ledger (unchanged path/shape for back-compat — the
        # harness exact-match gate at section 4 compares against this).
        echo "$FILE_PATH" >> "$SESSION_READS_FILE" 2>/dev/null || true
        # v0.2.70 Stream E (SF-1 fix): record into the INJECTOR reads store
        # (seen_reads_<sid>.txt — DISTINCT from the Build-Anchor reads_<sid>.txt)
        # so a source the model Read explicitly isn't re-injected. The producers
        # emit a REPO-RELATIVE `| src=<path>` trailer (KG entry.file_path =
        # "knowledge/..."; CODE file_path = repo-relative POSIX), so the ledger
        # MUST store the same repo-relative shape or the exact `grep -Fxq`
        # suppression in vco_filter_seen_blocks NEVER matches. The abs->relative
        # conversion is the ONE shared vco_to_repo_relative helper (no inline
        # copy). Skipped when the session id is untrustworthy (helper returns "").
        _REL_FP="$FILE_PATH"
        if command -v vco_to_repo_relative >/dev/null 2>&1; then
            _REL_FP="$(vco_to_repo_relative "$FILE_PATH" "$PROJECT_ROOT")"
        fi
        if command -v vco_seen_store_path >/dev/null 2>&1; then
            _UNIFIED_READS="$(vco_seen_store_path reads "$SESSION_ID_RAW" "$PROJECT_ROOT")"
            if [ -n "$_UNIFIED_READS" ]; then
                printf '%s\n' "$_REL_FP" >> "$_UNIFIED_READS" 2>/dev/null || true
            fi
        fi

        # v0.2.70 Stream C Surface 1 (Read): for a CODE file, inject its
        # entity/callers/deps summary so opening a source file surfaces the
        # code-graph context (was previously injected only on Edit). Gated on
        # the SAME IS_CODE regex as pre-edit:283 / post-file-edit:440 (MUST
        # MATCH those siblings). Self-exclude uses the repo-relative path so it
        # matches the producer's repo-relative CODE: src shape.
        if [[ "$FILE_PATH" =~ \.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$ ]]; then
            _RD_Q="$(basename "$FILE_PATH")"; _RD_Q="${_RD_Q%.*}"
            _cg_inject "$_RD_Q" "$_REL_FP" "Code-graph context for $(basename "$FILE_PATH")" "$_REL_FP"
        fi
    fi
    exit 0
fi

# === v0.2.70 Stream C Surface 4: Grep on a code SYMBOL → inject codegraph.
# A symbol-shaped Grep pattern is a strong "the model is navigating code" signal.
# Gated by the shared codegraph_pattern_gate (snake / CamelCase / name( / keyword
# id); a bare-word pattern like "TODO" does NOT fire. EXCLUDES nothing extra —
# Grep is already a code-navigation tool. diagram/web/secrets/Read-of-noncode/
# weaviate-kg never reach this branch (different tool names).
if [[ "$TOOL_NAME" == "Grep" ]]; then
    # Use codegraph_pattern_gate (identifier shape: snake/CamelCase/name(/keyword
    # id) — fires on `def authenticate`, `OrderManager`, `migrate_collections`;
    # NOT on bare `TODO` / `hello` / `foo.bar`). Same gate the pre-bash tool
    # branch uses (one home).
    if command -v codegraph_pattern_gate >/dev/null 2>&1; then
        GREP_PATTERN=$(_get_field "pattern")
        if [ -n "$GREP_PATTERN" ] && codegraph_pattern_gate "$GREP_PATTERN"; then
            _GREP_SYM="$GREP_PATTERN"
            if command -v codegraph_extract_symbol >/dev/null 2>&1; then
                _GREP_SYM="$(codegraph_extract_symbol "$GREP_PATTERN")"
            fi
            _cg_inject "$_GREP_SYM" "" "Code-graph context for symbol: ${_GREP_SYM}" "$_GREP_SYM"
        fi
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
