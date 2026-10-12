#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Post-tool credential scanning hook
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans the text the call INTRODUCED for credentials and surfaces the
# warning to the model via the PostToolUse JSON envelope so the
# next assistant turn can see + react to the alert (plain stdout
# from PostToolUse hooks is silently dropped per the v2.1.x
# contract — see `.claude/context/hook-audit-2026-05-10.md` §2.1).

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
[ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/emit-context.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# v0.2.52 V52-L.2: prefer canonical $CLAUDE_PROJECT_DIR (active workspace
# handed in by the launcher / Claude Code) over the SCRIPT_DIR/../..
# fallback. Aligns with pre-tool-use.sh and post-file-edit.sh, and is
# required for tests that invoke the hook from an out-of-tree directory.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Resolve a Python interpreter portably (python3 → python → py). Must run
# BEFORE the stdin-parsing step below because Windows ships python.exe / py
# but not python3 — bare `python3` would silently fail on Windows.
# See audit finding F6, 2026-04-30. _lib/find-python.sh sets $PY.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op (security log skipped)

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($1) are EMPTY because $CLAUDE_TOOL_ARG_FILE_PATH and
# similar env vars don't exist — settings.json substitutes to "". Verified
# 2026-05-08 via stdin-capture diagnostic.
HOOK_STDIN=$(cat 2>/dev/null || echo "")
# v0.2.76 P5 (hook-latency): parse the stdin payload with EXACTLY ONE Python
# interpreter (was FOUR — file_path, agent_id, agent_type, session_id each
# re-read + re-decoded the same JSON; each interpreter cold-start cost ~15ms).
# This hook fires on every Edit|Write, so the redundant parse was ~45ms of the
# measured ~66ms synchronous cost. Same NUL-delimited single-decode pattern as
# post-file-edit.sh (HK-1, v0.2.73): one decoder emits all four fields
# NUL-terminated (a trailing NUL after EACH field, incl. the last), read back
# with a single loop so an embedded newline in file_path survives. Malformed
# stdin → all-empty, preserving the exit-0 soft-fail contract. Values are
# byte-identical to the four-spawn form — no behaviour change.
# V52-L.2 Fix 2a: agent_id + agent_type + session_id are parsed so
# credential_alerts.jsonl rows are attributable to the subagent that
# triggered the write (pre-V52-L.2 every alert row looked parent-sourced).
EDITED_FILE=""
AGENT_ID=""
AGENT_TYPE=""
SESSION_ID=""
_PTS_IDX=0
while IFS= read -r -d '' _PTS_VAL; do
    case "$_PTS_IDX" in
        0) EDITED_FILE="$_PTS_VAL" ;;
        1) AGENT_ID="$_PTS_VAL" ;;
        2) AGENT_TYPE="$_PTS_VAL" ;;
        3) SESSION_ID="$_PTS_VAL" ;;
    esac
    _PTS_IDX=$((_PTS_IDX + 1))
done < <(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {}) or {}
    fields = [
        ti.get('file_path', '') or '',
        d.get('agent_id', '') or '',
        d.get('agent_type', '') or '',
        d.get('session_id', '') or '',
    ]
except Exception:
    fields = ['', '', '', '']
# Trailing NUL after EACH field so the reader loop terminates cleanly.
sys.stdout.write(''.join(str(f) + '\0' for f in fields))
" 2>/dev/null)

ALERT_LOG="$PROJECT_ROOT/.claude/logs/credential_alerts.jsonl"
mkdir -p "$(dirname "$ALERT_LOG")"

[[ -z "$EDITED_FILE" ]] && exit 0
[[ ! -f "$EDITED_FILE" ]] && exit 0

# WHAT IS SCANNED (v0.2.102 W6-N, owner ruling 2026-10-12): only what THIS
# call introduced -- Edit `new_string` (+ MultiEdit `edits`), Write
# `content` -- never the whole file. Scanning the whole file re-alerted on
# every unrelated edit of a file that already held a test placeholder (the
# `ghp_...` fixtures in secrets.rs). A line carrying the explicit marker
# `vco-secret-scan: test-placeholder` is a declared placeholder; nothing is
# inferred from how a value looks. Findings name the project-relative path,
# the line(s) and the shape label -- never the value. Bash-written files are
# not covered: this hook is registered for Edit|Write only.
#
# ONE implementation: .claude/scripts/credscan_introduced.py (resolved from
# this hook's own bundle, ../scripts, then $PROJECT_ROOT/.claude/scripts),
# shared with the .ps1 sibling. It owns the scope, the marker test and the
# PEM body floor (>= 120 base64 chars; control flow over a multi-line
# window, so it cannot live in the vocabulary).
#
# PATTERN SOURCE (read this before adding a regex): the shapes are NOT
# defined here. They come from the credential-shape vocabulary SSOT
# vco_lib/credential_shapes.py via _lib/credshapes.sh, `content_scan`
# context -- the SAME vocabulary _lib/credscan.sh consumes -- and are handed
# to the scan helper on argv. The test-placeholder marker comes from the
# same mirror (CREDSHAPES_TEST_PLACEHOLDER_MARKER). Add shapes to the SSOT,
# never here.
ALERTS=()
FINDINGS_JSON="[]"
SUMMARY=""
REL_FILE="$EDITED_FILE"

SCAN_HELPER=""
for _PTS_CAND in "$SCRIPT_DIR/../scripts/credscan_introduced.py" \
                 "$PROJECT_ROOT/.claude/scripts/credscan_introduced.py"; do
    if [ -f "$_PTS_CAND" ]; then SCAN_HELPER="$_PTS_CAND"; break; fi
done

# shellcheck source=_lib/credshapes.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/credshapes.sh" ] && . "$SCRIPT_DIR/_lib/credshapes.sh"
if ! command -v credshapes_for_context >/dev/null 2>&1 \
   || ! credshapes_for_context content_scan \
   || [ "${#CREDSHAPES_PATTERNS[@]}" -eq 0 ] \
   || [ -z "${CREDSHAPES_TEST_PLACEHOLDER_MARKER:-}" ]; then
    # A MISSING vocabulary must not look like a clean file. Downstream treats an
    # empty ALERTS array as "nothing found", so degrading silently here would
    # turn a broken install into a permanent all-clear. Raise it as a real
    # alert so it travels the same notification + JSONL path as any finding.
    ALERTS+=("credential scanner UNAVAILABLE (_lib/credshapes.sh missing)")
elif [ -z "$SCAN_HELPER" ]; then
    ALERTS+=("credential scanner UNAVAILABLE (scripts/credscan_introduced.py missing)")
else
    _PTS_SHAPE_ARGS=()
    for _PTS_SHAPE_I in "${!CREDSHAPES_PATTERNS[@]}"; do
        _PTS_SHAPE_ARGS+=("${CREDSHAPES_IDS[$_PTS_SHAPE_I]}"
                          "${CREDSHAPES_LABELS[$_PTS_SHAPE_I]}"
                          "${CREDSHAPES_PATTERNS[$_PTS_SHAPE_I]}")
    done
    _PTS_COUNT=""
    _PTS_IDX=0
    while IFS= read -r -d '' _PTS_VAL; do
        case "$_PTS_IDX" in
            0) _PTS_COUNT="$_PTS_VAL" ;;
            1) [ -n "$_PTS_VAL" ] && REL_FILE="$_PTS_VAL" ;;
            2) SUMMARY="$_PTS_VAL" ;;
            3) _PTS_PATTERNS="$_PTS_VAL" ;;
            4) FINDINGS_JSON="$_PTS_VAL" ;;
        esac
        _PTS_IDX=$((_PTS_IDX + 1))
    done < <(printf '%s' "$HOOK_STDIN" | "$PY" "$SCAN_HELPER" \
                --root "$PROJECT_ROOT" \
                --marker "$CREDSHAPES_TEST_PLACEHOLDER_MARKER" \
                --format nul -- "${_PTS_SHAPE_ARGS[@]}" 2>/dev/null)
    case "$_PTS_COUNT" in
        '') ALERTS+=("credential scanner UNAVAILABLE (scripts/credscan_introduced.py failed)") ;;
        0) ;;
        *) ALERTS+=("$_PTS_PATTERNS") ;;
    esac
fi

if [ ${#ALERTS[@]} -gt 0 ]; then
    # Path + line(s) + shape label; NEVER the value (owner ruling 2026-10-12).
    MSG="Possible credential in ${REL_FILE}: ${SUMMARY:-${ALERTS[*]}}"
    # Build the JSONL line via Python so the path / labels / findings are
    # properly JSON-escaped. Audit fix 2026-05-07.
    JSONL=$(EDITED_FILE_FOR_PY="$REL_FILE" \
        PATTERNS_FOR_PY="${ALERTS[*]}" \
        FINDINGS_FOR_PY="$FINDINGS_JSON" \
        AGENT_ID_FOR_PY="$AGENT_ID" \
        AGENT_TYPE_FOR_PY="$AGENT_TYPE" \
        SESSION_ID_FOR_PY="$SESSION_ID" \
        "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
try:
    findings = json.loads(os.environ.get("FINDINGS_FOR_PY") or "[]")
except ValueError:
    findings = []
# V52-L.2 Fix 2a: include session_id + agent_id + agent_type so post-hoc
# forensics can attribute the credential alert back to the agent that
# triggered the write. `findings` = [{line, id, label}] -- never a value.
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "file": os.environ.get("EDITED_FILE_FOR_PY", ""),
    "patterns": os.environ.get("PATTERNS_FOR_PY", ""),
    "findings": findings,
    "session_id": os.environ.get("SESSION_ID_FOR_PY", ""),
    "agent_id": os.environ.get("AGENT_ID_FOR_PY", ""),
    "agent_type": os.environ.get("AGENT_TYPE_FOR_PY", ""),
}))
' 2>/dev/null)
    if [ -n "$JSONL" ]; then
        printf '%s\n' "$JSONL" >> "$ALERT_LOG" 2>/dev/null || true
    fi
    # Cross-platform notification (Linux/macOS/Windows). See audit F2.
    # v0.2.82: desktop-toast DEDUP — the SAME (file, findings) alert notifies
    # at most once per 6h window. Forensics are NOT rate-limited: the JSONL
    # log above and the model envelope below stay per-event — only the human
    # toast is deduped. MUST MATCH the .ps1 sibling's key derivation.
    NOTIFY_DEDUP_FILE="$PROJECT_ROOT/.claude/logs/.cred_alert_notify_dedup"
    NOTIFY_TTL_SECS=21600
    DEDUP_KEY=$(printf '%s|%s' "$REL_FILE" "$MSG" | "$PY" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null)
    NOW_EPOCH=$(date +%s)
    SHOULD_NOTIFY=1
    if [ -n "$DEDUP_KEY" ] && [ -f "$NOTIFY_DEDUP_FILE" ]; then
        LAST_TS=$(grep "^$DEDUP_KEY " "$NOTIFY_DEDUP_FILE" 2>/dev/null | tail -1 | cut -d' ' -f2)
        if [ -n "$LAST_TS" ] && [ $((NOW_EPOCH - LAST_TS)) -lt "$NOTIFY_TTL_SECS" ]; then
            SHOULD_NOTIFY=0
        fi
    fi
    if [ "$SHOULD_NOTIFY" = "1" ] && [ -n "${PY:-}" ] && [ -f "$PROJECT_ROOT/.claude/scripts/notify.py" ]; then
        "$PY" "$PROJECT_ROOT/.claude/scripts/notify.py" \
            "Claude Code Security Alert" "$MSG" \
            --urgency critical --icon dialog-warning 2>/dev/null || true
        if [ -n "$DEDUP_KEY" ]; then
            printf '%s %s\n' "$DEDUP_KEY" "$NOW_EPOCH" >> "$NOTIFY_DEDUP_FILE" 2>/dev/null || true
        fi
    fi
    # Surface the alert to the model via the PostToolUse JSON envelope
    # (`hookSpecificOutput.additionalContext`). Plain stdout from
    # PostToolUse hooks is silently dropped — the desktop notification
    # above reaches the user, but without this envelope path the model
    # never saw credential alerts on files it had just written.
    REMINDER="[Security alert] ${MSG}
Review the lines named above before continuing: the credential pattern is in
text this edit introduced (or on a line it changed) in ${REL_FILE}. If it is
a deliberate test placeholder, mark its line with the comment
\`vco-secret-scan: test-placeholder\`; if it leaked from real input, redact
it before any further git operations or external sharing."
    if command -v emit_additional_context >/dev/null 2>&1; then
        emit_additional_context "$REMINDER" PostToolUse
    fi
fi

exit 0
