#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Post-tool credential scanning hook
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans for accidentally committed credentials and surfaces the
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

# Collect any matching credential patterns
ALERTS=()

check_pattern() {
    local label="$1"; shift
    if grep -qE "$*" "$EDITED_FILE" 2>/dev/null; then
        ALERTS+=("$label")
    fi
}

check_pattern "Anthropic/OpenAI API key"  'sk-(ant-api03|[a-zA-Z0-9]{30,})-[a-zA-Z0-9]'
check_pattern "AWS access key"            'AKIA[A-Z0-9]{16}'
check_pattern "GitHub token"             'gh[pousr]_[a-zA-Z0-9]{36}'
# D-13 (v0.2.75): GitHub fine-grained PAT (github_pat_ + 22 alnum + _ +
# 59 alnum). The gh[pousr]_ shape above NEVER matches these — yet
# github_pat_* is exactly the shape VCO's own secrets flow provisions,
# so the scanner that exists to catch a leaked token stayed blind to the
# most likely one. MUST MATCH the canonical anchor in
# scripts/check-no-secrets.sh (TOKEN_SHAPES, the
# `github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}` entry) — one pattern home,
# never a fourth fork. The exact-format shape (not the looser
# github_pat_[A-Za-z0-9_]{60,}) is deliberate: the loose form
# false-positives on Rust release-binary rodata identifier soup; here it
# also keeps us from matching identifiers like `get_github_pat_preview`.
check_pattern "GitHub fine-grained PAT"  'github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}'
# v0.2.82: PEM detection requires a PLAUSIBLE key body, not just the BEGIN
# marker. Pattern-definition/test files legitimately contain the marker as a
# literal (vct-launcher-core/src/secrets.rs ships a 13-char stub PEM as the
# write-guard's leave-alone fixture) and were re-alerting on EVERY edit. A
# real private-key body is >=1600 base64 chars; requiring >=256 keeps every
# real leak detectable while ignoring obvious stubs. Needs a multi-line
# window that grep -E can't express, hence $PY.
check_pem_key() {
    if "$PY" - "$EDITED_FILE" <<'PYEOF' 2>/dev/null
import re, sys
try:
    text = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
except Exception:
    sys.exit(1)
for m in re.finditer(r"BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY", text):
    window = text[m.end():m.end() + 8192]
    end = window.find("-----END")
    body = window if end < 0 else window[:end]
    if len(re.sub(r"[^A-Za-z0-9+/=]", "", body)) >= 256:
        sys.exit(0)  # plausible real key -> alert
sys.exit(1)  # marker(s) without a plausible body -> stub/pattern, no alert
PYEOF
    then
        ALERTS+=("PEM private key")
    fi
}
check_pem_key
check_pattern "Generic secret"           '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["'"'"'][a-zA-Z0-9+/=_\-]{32,}'
# D-13 (v0.2.75): unquoted-assignment variant of the generic-secret
# pattern. The quoted form above requires an opening quote after the
# `=`, so a `.env`-style bare `API_KEY=abc123...` (no quotes — the common
# dotenv shape) escaped it. Anchor on a value that starts with a
# non-quote, non-space char and runs >=32 chars of secret-alphabet so a
# short `API_KEY=on` config line doesn't trip.
check_pattern "Generic secret (unquoted)" '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*[a-zA-Z0-9+/=_\-]{32,}'
# Smoke-test marker — used by hook tests to verify the scanner +
# alert-routing flow without leaving real-looking credentials in
# example fixtures. Tests use the literal string
# VCT_HOOK_LEAK_PROBE_a3f7c2 — VCT-prefixed so it's recognizable as
# ours, _PROBE suffix to signal testing intent, plus a 6-char random
# hex tail to make the token unique enough that it cannot
# accidentally appear in CHANGELOG prose or external docs.
# Previously this was LEAK_TEST_KEY; renamed 2026-05-18 because the
# bare-word pattern matched a legitimate CHANGELOG release-note
# entry describing the smoke-test infrastructure itself.
check_pattern "Hook leak-test marker"    'VCT_HOOK_LEAK_PROBE_a3f7c2'

if [ ${#ALERTS[@]} -gt 0 ]; then
    MSG="Possible credential in $(basename "$EDITED_FILE"): ${ALERTS[*]}"
    # Build the JSONL line via Python so EDITED_FILE / ALERTS[] / patterns
    # are properly JSON-escaped. Audit fix 2026-05-07.
    JSONL=$(EDITED_FILE_FOR_PY="$EDITED_FILE" \
        PATTERNS_FOR_PY="${ALERTS[*]}" \
        AGENT_ID_FOR_PY="$AGENT_ID" \
        AGENT_TYPE_FOR_PY="$AGENT_TYPE" \
        SESSION_ID_FOR_PY="$SESSION_ID" \
        "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
# V52-L.2 Fix 2a: include session_id + agent_id + agent_type so post-hoc
# forensics can attribute the credential alert back to the agent that
# triggered the write.
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "file": os.environ.get("EDITED_FILE_FOR_PY", ""),
    "patterns": os.environ.get("PATTERNS_FOR_PY", ""),
    "session_id": os.environ.get("SESSION_ID_FOR_PY", ""),
    "agent_id": os.environ.get("AGENT_ID_FOR_PY", ""),
    "agent_type": os.environ.get("AGENT_TYPE_FOR_PY", ""),
}))
' 2>/dev/null)
    if [ -n "$JSONL" ]; then
        printf '%s\n' "$JSONL" >> "$ALERT_LOG" 2>/dev/null || true
    fi
    # Cross-platform notification (Linux/macOS/Windows). See audit F2.
    # v0.2.82: desktop-toast DEDUP — the SAME (file, patterns) alert notifies
    # at most once per 6h window. Repeated edits of a file that legitimately
    # trips a pattern were emitting an identical toast PER EDIT (hundreds
    # during an agent session on secrets.rs). Forensics are NOT rate-limited:
    # the JSONL log above and the model envelope below stay per-event — only
    # the human toast is deduped.
    NOTIFY_DEDUP_FILE="$PROJECT_ROOT/.claude/logs/.cred_alert_notify_dedup"
    NOTIFY_TTL_SECS=21600
    DEDUP_KEY=$(printf '%s|%s' "$EDITED_FILE" "${ALERTS[*]}" | "$PY" -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])' 2>/dev/null)
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
Review the diff before continuing. The credential pattern was found in the
file you just edited (${EDITED_FILE}). If it was unintended (test fixture,
example doc), confirm it's safe; if it leaked from real input, redact it
before any further git operations or external sharing."
    if command -v emit_additional_context >/dev/null 2>&1; then
        emit_additional_context "$REMINDER" PostToolUse
    fi
fi

exit 0
