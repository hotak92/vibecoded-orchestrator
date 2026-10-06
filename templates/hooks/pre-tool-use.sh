#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# VCO-CENTRALIZED-KG: NOT a KG consumer (v0.2.101 wave-3). The former §5
#   KG-suggestion path was retired (double emission with the pre-edit/pre-
#   write router wrappers — review nit-6); every remaining branch (SSRF
#   guard, shell-injection scan, Build Anchor, file backup, tool logging)
#   is KG/codegraph-free. Marker kept (the centralization audit classifies
#   every hook); classification: no KG access.

# Pre-tool-use hook — Security enforcement (KG suggestion RETIRED, v0.2.101)
# Triggers: Before all tool uses
# Actions:
#   1. SSRF guard (WebFetch / fetch_page to private IPs)
#   2. Shell injection scan (network-fetch-to-shell patterns)
#   3. Build Anchor Protocol: track reads, block unread Write/Edit
#   4. File backup before Write/Edit on existing files
#   5. (retired — the pre-edit/pre-write router wrappers own edit-time KG)
#
# v0.2.77 9-bis: the per-tool-call TOUCAN dataset writer
# (.claude/logs/toucan_dataset.jsonl) was RETIRED here — it was a
# write-only collector with zero consumers (never wired to any RL
# training path; RL training telemetry lives in launcher.db rl_events +
# the citation drain, untouched by this removal). The stdin parse below
# still decodes the full payload for the security + Build-Anchor branches.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Source emit-context.sh ONLY if the file exists. If the helper is
# missing (partial install or just-after-clone before _lib/ is fully
# populated), the hook still runs its other branches (logging,
# security guards). The broken-install notices check
# `command -v emit_additional_context` before calling it.
# We deliberately do NOT trail with `|| true`: a syntax error inside
# an existing helper is a real bug we want surfaced.
if [ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ]; then
    . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"
fi
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
if [ -z "${PY:-}" ]; then
    # No Python: every branch below needs it to read the payload, so the hook
    # is a silent no-op — EXCEPT for WebFetch (review R18F-08). The SSRF guard
    # is Python (vco_lib.ssrf_url), so without an interpreter it cannot judge
    # any URL, and a security guard that cannot run fails CLOSED: the tool
    # name is read from the payload by pattern (no JSON parser needed) and
    # every WebFetch is blocked with the fix named.
    _NOPY_STDIN="$(cat 2>/dev/null || true)"
    if printf '%s' "$_NOPY_STDIN" | grep -Eq '"tool_name"[[:space:]]*:[[:space:]]*"WebFetch"'; then
        {
            echo "🔒 SSRF guard: no Python interpreter was found, so WebFetch cannot be checked and is blocked."
            echo "   This is a broken VCO install: put Python 3 on PATH (or re-run the orchestrator's install / update,"
            echo "   which provides the VCO venv), then retry."
        } >&2
        exit 2
    fi
    # v0.2.100 WP-17: the Bash security scans (the injection regexes read the
    # parsed command; .claude/scripts/bash_security.py IS Python) cannot run
    # either — say so instead of skipping in silence. stderr every time (for
    # the human); once per session (sentinel under .claude/state) a static
    # additionalContext envelope so the MODEL can tell the user (PreToolUse
    # stderr on exit 0 never reaches it). Non-blocking: blocking every Bash
    # call would make a Python-less machine unusable, and the notice names the
    # fix. MUST MATCH pre-tool-use.ps1's Write-VcoNoPythonNotice.
    if printf '%s' "$_NOPY_STDIN" | grep -Eq '"tool_name"[[:space:]]*:[[:space:]]*"Bash"'; then
        _NOPY_MSG="[VCO broken install] The Bash security scans (shell-injection guard + .claude/scripts/bash_security.py) did NOT run: no Python interpreter was found (python3 / python / py on PATH). Put Python 3 on PATH or re-run the orchestrator's install / update (python install.py --update in the orchestrator root, or the launcher's Update), then retry."
        echo "$_NOPY_MSG" >&2
        _NOPY_SID="$(printf '%s' "$_NOPY_STDIN" | grep -Eo '"session_id"[[:space:]]*:[[:space:]]*"[A-Za-z0-9_-]+"' | head -n 1 | sed 's/.*"\([A-Za-z0-9_-]*\)"$/\1/')"
        [ -n "$_NOPY_SID" ] || _NOPY_SID="default"
        _NOPY_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
        _NOPY_SENTINEL="$_NOPY_ROOT/.claude/state/no_python_notice_$_NOPY_SID"
        if [ ! -e "$_NOPY_SENTINEL" ]; then
            mkdir -p "$_NOPY_ROOT/.claude/state" 2>/dev/null && : > "$_NOPY_SENTINEL" 2>/dev/null
            # The message is a fixed ASCII literal with no quote or backslash,
            # so it is embedded in the JSON as-is (no encoder available here).
            printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "%s"}}\n' "$_NOPY_MSG"
        fi
    fi
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# v0.2.70 Streams C+E: shared helpers for canonical session-id and the
# unified seen-store dedup (the reads ledgers written in the Read branch
# below; v0.2.101 §C2/§C6 retired this hook's code-graph INJECTION branches
# — read-context-inject / grep-context-inject are their homes now). Sourced
# only if present (partial-install tolerance).
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"
# shellcheck source=_lib/seen-store.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/seen-store.sh" ] && . "$SCRIPT_DIR/_lib/seen-store.sh"
# (v0.2.101 wave-3: the query-cache.sh sourcing was retired with §5 — its
# last caller. The lib itself was deleted; the router owns caching now.)
# v0.2.29: prefer Claude Code's canonical $CLAUDE_PROJECT_DIR (the active
# workspace the launcher hands us — source of truth for per-project hooks).
# Fall back to SCRIPT_DIR/../.. for ad-hoc invocations (manual runs, tests)
# that don't set the env var. This matches the canonical hook contract
# while staying robust for non-Claude-Code callers.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1/$2/$3) are EMPTY because $CLAUDE_TOOL_NAME etc.
# don't exist as env vars — the SSRF guard, shell-injection scan, and
# Build-Anchor branches all need the parsed payload, so without this
# every one of them would see empty tool_name / tool_input.
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
# the JSON payload carries agent_id + agent_type. These fields are parsed
# as part of the single-decode prelude (v0.2.76 P5) and kept for parity
# with the post-tool-security / post-file-edit siblings that still consume
# them; this hook's own former consumer (the TOUCAN row) was retired in
# v0.2.77 9-bis. Empty string when the field is absent (parent context).
AGENT_ID=""
AGENT_TYPE=""
# WP-E (v0.2.92): transcript_path + prompt_id ride the shared single-decode
# prelude. v0.2.101 wave-3: this hook no longer CONSUMES them (§5 retired —
# the router wrappers resolve both from the payload themselves); the fields
# stay in the decoder so its NUL protocol is byte-identical across siblings.
# transcript_path remains a PATH ONLY everywhere (R31). MUST MATCH the
# sibling hooks' parses.
TRANSCRIPT_PATH=""
PROMPT_ID=""
_PTU_IDX=0
while IFS= read -r -d '' _PTU_VAL; do
    case "$_PTU_IDX" in
        0) TOOL_NAME="$_PTU_VAL" ;;
        1) TOOL_ARGS="$_PTU_VAL" ;;
        2) USER_MESSAGE="$_PTU_VAL" ;;
        3) SESSION_ID_FROM_STDIN="$_PTU_VAL" ;;
        4) AGENT_ID="$_PTU_VAL" ;;
        5) AGENT_TYPE="$_PTU_VAL" ;;
        6) TRANSCRIPT_PATH="$_PTU_VAL" ;;
        7) PROMPT_ID="$_PTU_VAL" ;;
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
        d.get('transcript_path', '') or '',
        d.get('prompt_id', '') or '',
    ]
except Exception:
    fields = ['', '{}', '', '', '', '', '', '']
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

# === 1. SSRF GUARD ===
if [[ "$TOOL_NAME" == "WebFetch" ]]; then
    URL=$(_get_field "url")
    if [[ -n "$URL" ]]; then
        # Allowed local services (Weaviate, Ollama, code-embed, vct-hub, :8082,
        # Gradio). SearXNG (:8888) and the mcp__search__fetch_page tool both
        # removed in v0.2.11 (see PR-14a); the search MCP itself was deleted
        # in v0.2.101, so nothing else reaches the network outside WebFetch.
        # v0.2.100: the decision is `python -m vco_lib.ssrf_url` (one
        # implementation for every OS; its docstring is the contract), run
        # once through _lib/ssrf-allowlist.sh. The allowed pairs are DERIVED
        # from the projected env, so a moved service_endpoints port is
        # allowed and nothing asks the user to hand-edit this hook.
        # FAIL CLOSED: only the exact words `allow` / `pass` let the call
        # through. The lib missing (partial install), no interpreter, vco_lib
        # not importable, or any other output blocks, and says why.
        _SSRF_VERDICT=""
        _SSRF_PAIRS=""
        _SSRF_WHY="hooks/_lib/ssrf-allowlist.sh is missing — run the bundle update to restore it"
        _SSRF_LIB="$SCRIPT_DIR/_lib/ssrf-allowlist.sh"
        if [[ -f "$_SSRF_LIB" ]]; then
            # shellcheck source=_lib/ssrf-allowlist.sh disable=SC1091
            . "$_SSRF_LIB"
            vco_ssrf_run "$URL" "$SCRIPT_DIR"
            _SSRF_VERDICT="$_vco_ssrf_verdict"
            _SSRF_PAIRS="$_vco_ssrf_pairs"
            _SSRF_WHY="the guard could not run (${_vco_ssrf_err:-unrecognised answer '$_vco_ssrf_verdict'}) — a broken VCO install: re-run the orchestrator's update (\`python install.py --update\` in the orchestrator root, or the launcher's Update), which reinstalls vco_lib into the VCO venv"
        fi
        if [[ "$_SSRF_VERDICT" != allow && "$_SSRF_VERDICT" != pass ]]; then
            # Block messages route to stderr — see comment in bash-
            # security branch below for why (Claude Code drops plain
            # stdout from PreToolUse hooks).
            {
                if [[ "$_SSRF_VERDICT" == block ]]; then
                    echo "🔒 SSRF guard: '$URL' targets a private/internal network address (or one the guard cannot read)."
                    echo "   Allowed local services on this machine: ${_SSRF_PAIRS:-none}"
                    echo "   They follow WEAVIATE_URL / OLLAMA_URL / CODE_EMBED_SERVICE_URL and the hub port — a moved service is"
                    echo "   changed with the launcher's Services page or \`python -m vco_lib.service_endpoints move\`, never by editing this hook."
                else
                    echo "🔒 SSRF guard: '$URL' was blocked because $_SSRF_WHY."
                fi
            } >&2
            # JSON-escape `\` and `"` (the backslash URLs above are exactly
            # what this line logs), as the .ps1 sibling does.
            _SSRF_URL_ESC="${URL//\\/\\\\}"
            _SSRF_URL_ESC="${_SSRF_URL_ESC//\"/\\\"}"
            echo "{\"timestamp\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"event\":\"ssrf_blocked\",\"url\":\"$_SSRF_URL_ESC\"}" >> "$SECURITY_LOG" 2>/dev/null || true
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

# === v0.2.70 Stream C — RETIRED (v0.2.101 §C2/§C6) ============================
# The shared code-graph injection helper and its two call
# surfaces — Read(code) and Grep(symbol) — were REMOVED. Their home is the
# router-backed hooks read-context-inject.{sh,ps1} (PostToolUse Read) and
# grep-context-inject.{sh,ps1} (PreToolUse Grep): exact-symbol lookups,
# §2.1 gates, seen-store dedupe and the per-turn budget all live in
# claude_mcp_servers/scripts/hook_context_router.py + vco_lib/inject_intent.
# This hook keeps ONLY the PreToolUse concerns that must stay here: the
# security guards, the Build-Anchor reads ledger and the unified
# seen_reads ledger (both written above), and file backup. (The §5
# Edit/Write KG suggestion was retired in wave-3 — review nit-6.)


# === 3. BUILD ANCHOR PROTOCOL: Track reads (the Read(code) code-graph inject
# moved to read-context-inject.sh, v0.2.101 §C2) ===
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

        # v0.2.101 injection redesign (§C2): the Read(code) code-graph
        # injection branch that used to live here was REMOVED — its home is
        # now the PostToolUse(Read) hook read-context-inject.sh (one
        # concern, one home; the kickoff probe measured the old branch as
        # always-killed by its 3 s settings timeout, so it never injected).
        # The ledger writes ABOVE stay: they must happen PreToolUse so
        # same-turn Write-anchor checks and seen-store suppression see them.
    fi
    exit 0
fi

# === v0.2.70 Stream C Surface 4 (Grep) — RETIRED (v0.2.101 §C6) ============
# The Grep(symbol) code-graph injection branch was REMOVED: grep-context-
# inject.{sh,ps1} (PreToolUse(Grep), router surface `grep`) is its one home
# now — identifier gating in vco_lib.inject_intent, EXACT structure def+
# callers lookup, no KG leg. A Grep call falls through to the Write/Edit
# gate below (no-op for Grep) and exits 0 at the end of the hook.

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

# === 5. KG SEARCH SUGGESTION — RETIRED (v0.2.101 wave-3, review nit-6) =====
# This branch emitted a "💡 Found N related patterns" KG suggestion on every
# Edit/Write — the SAME tool calls pre-edit-context-inject /
# pre-write-context-inject inject gated, deduped, budgeted KG + code-graph
# context for: two context emissions per edit, and the suggestion path had
# no score floor at all (the survey's precision complaint). The injection
# redesign made those wrappers (via hook_context_router.py) the ONE home for
# edit-time KG context — PLAN-V02101 §2.1 Edit/Write rows: floor 0.65,
# titles-only below 0.85, exact def+callers on the code-graph leg, seen-store
# dedupe + per-turn budget. Retiring this branch also retired the last shell
# caller of _lib/query-cache.sh, deleted with it (the router keeps its own
# Python cache in the same state dir under the disjoint kgi/cgi namespaces;
# the §9 never-cache-empty contract lives there and is pinned by
# tests/test_v02101_query_cache_poison_fix.py). The pre_tool_use_kg_search
# task type stays registered in rl_kg_search's KNOWN_TASK_TYPES — the
# historical RL corpus keeps its partition label.
exit 0
