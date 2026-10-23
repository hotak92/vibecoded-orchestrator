#!/usr/bin/env bash
# Parity note (v0.2.54 Track G G-6): the .ps1 sibling now resolves its
# child-spawn PowerShell binary via _lib/resolve-powershell.ps1 (pwsh ->
# powershell fallback for PS 5.1-only machines). No bash-side logic
# change is needed - bash hooks never spawn PowerShell.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# subagent-stop-reconcile.sh — SubagentStop hook that reconciles a
# subagent's filesystem changes back into the project's KG / code-graph
# / credential-scan / nudge-counter state. Also writes a JSONL audit
# row to .claude/logs/subagent-reconciliation.jsonl (the V52-L.2
# logging-only baseline — preserved exactly).
#
# V52-L.1 (v0.2.52): belt-and-suspenders reconciler. Side effects:
#   1. JSONL audit log (V52-L.2 baseline; format unchanged). FOREGROUND.
#   1b. Reap the agent's worktree when it is provably unchanged.
#   2. KG/docs sync — every changed `knowledge/**` / `docs/**.md` file goes
#      through `_lib/route-touched-path.sh`, the ONE home for "a file was
#      touched" (access-matrix gate + per-file debounce + detached runner
#      with no timeout). v0.2.102 W6-C2 (census L1): it ran
#      `timeout 30 kg-sync` (and a `sleep 30 && kill -9` fallback) inline,
#      killing slow-but-working syncs and bypassing the access matrix.
#   3. Code-graph drain enqueue — for any modified code file, apply the
#      FIX-A' worktree gate (drop edits in ephemeral/unregistered
#      worktrees) and append the gated-IN paths to the SESSION-AGNOSTIC
#      drain queue `.claude/state/codegraph_drain_shared.txt` that the
#      Stop-hook batched drain (stop-codegraph-drain.{sh,ps1}) consumes.
#      We never run analyze synchronously and we never write the old orphan
#      code-graph-queue.jsonl (nothing ever drained it — v0.2.73 HIGH-2).
#   4. Credential scan — run the shared `_lib/credscan.sh` scanner on
#      every modified file. On hit, append an entry to
#      `.claude/logs/credential_alerts.jsonl` (post-tool-security's schema)
#      AND file it for the PARENT (census F8, below).
#   5. Subagent work record — bump `subagent_work_units` (+ count/ids) on
#      the parent session's row in the shared `kg_update_tokens.jsonl`
#      (metrics dir resolved by `_lib/metrics-dir.sh`; v0.2.92 W7 moved it
#      out of ~/.claude). The field records each subagent's work for the
#      per-subagent KG nudge the owner scheduled for v0.2.103 (each
#      subagent nudged on its own work). The main-chat kg-update-nudge
#      deliberately does NOT read it (owner ruling 2026-10-13: the
#      main-chat nudge ignores subagent work). Work-unit estimate =
#      file_count * 50 + total_diff_size_bytes/4 (rough proxy for output
#      tokens). v0.2.102 W6-C2 (census F5): the read-modify-write holds the
#      SAME sidecar lock (`kg_update_tokens.jsonl.lock`, flock) the nudge
#      takes, so it can no longer drop a concurrent nudge update.
#
# DETACHED (v0.2.102 W6-C2, census F6/L1; owner rulings 2026-10-13: never
# kill slow-but-working jobs — detach them). The registered SubagentStop
# timeout was 2 s, while the snapshot diff alone measured ~1.4 s, plus the
# reap's `git status --ignored` / `git worktree remove` and the kg-sync. A
# kill mid-run lost Steps 3-5 and could leave a half-removed worktree. Now the
# FOREGROUND only parses the payload, writes the audit row and hands the
# payload (via a 0600 job file under .claude/state) to ONE detached run of
# this same script (`--job <file>`; setsid → nohup → `&`, the
# stop-codegraph-drain `_drain_spawn_detached` discipline, data as argv
# only). That job does steps 1b-5 and Layer 3b with no harness deadline.
# `VCO_SUBAGENT_RECONCILE_INLINE=1` runs the job in the foreground instead
# (diagnostics; the test suite pins it for determinism).
#
# ATTRIBUTION (v0.2.102 W6-C2, census F7). An agent that ran in its OWN
# worktree (a worktree-guard record with mode head|snapshot|reservation*)
# is reconciled from THAT worktree's git state (`wtrec_changed_files`):
# exact attribution, and the files were missed entirely before (the shared-
# tree snapshot never walks `.claude/worktrees/`). Those files are only
# credential-scanned: their KG/code-graph indexing happens when the work is
# merged into the main tree. Every other agent is reconciled from the
# shared-tree snapshot diff, which ALSO sees edits the parent or a sibling
# lane made meanwhile — so that diff is recorded as
# `attribution: unattributed_shared_tree` (the per-call PostToolUse hooks
# already attribute every Edit/Write the agent made as it happened).
#
# PARENT ALERTS (v0.2.102 W6-C2, census F8). A kept worktree, a worktree
# violation and a credential hit are also filed for the PARENT under
# .claude/state/agent_alerts_<sid>__<agent>.jsonl (vco_lib/agent_alerts.py);
# agent-alerts-deliver surfaces them on the parent's PostToolUse(Agent) or
# next prompt. Never via SubagentStop context: that keeps the subagent
# running. (The stderr lines below remain for an inline/diagnostic run.)
#
# File-modification discovery (V52-L.1 spec, Option 2):
#   - SubagentStart hook (subagent-start-kg-inject.sh) writes a
#     SHA-256 snapshot of watched paths to
#     `.claude/state/subagent-snapshot-<agent_id>.json` — for a NON-isolated
#     agent only (an isolated one is reconciled from its worktree).
#   - This hook diffs against the snapshot to identify modified files.
#   - When the snapshot is missing (SubagentStart hook not installed,
#     state dir wiped, etc.), the reconciler degrades to logging-only.
#
# Soft-fail contract: every step is wrapped to never raise. The hook
# MUST exit 0 even when every side effect fails — the subagent has
# already terminated, so blocking on this hook would only hurt the
# user.

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0

# V52-L.1 helpers. Optional sourcing — when missing (partial install),
# the new reconciler steps no-op silently and we fall through to the
# V52-L.2 logging-only behaviour.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/_lib/snapshot.sh" ]; then
    # shellcheck source=_lib/snapshot.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/snapshot.sh"
fi
if [ -f "$SCRIPT_DIR/_lib/credscan.sh" ]; then
    # shellcheck source=_lib/credscan.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/credscan.sh"
fi
# v0.2.73 HIGH-2: the canonical-root resolver + FIX-A' worktree gate — the SAME
# shared libs the per-edit code-graph hook and the Stop drain use (mirror-don't-
# fork). Optional sourcing; when absent the Step-3 enqueue falls back to
# CONSERVATIVE (enqueue-all — never silently lose a real project's edits).
if [ -f "$SCRIPT_DIR/_lib/canonical-repo-root.sh" ]; then
    # shellcheck source=_lib/canonical-repo-root.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/canonical-repo-root.sh"
fi
if [ -f "$SCRIPT_DIR/_lib/worktree-gate.sh" ]; then
    # shellcheck source=_lib/worktree-gate.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/worktree-gate.sh"
fi

PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
LOG_DIR="$PROJECT_ROOT/.claude/logs"
STATE_DIR="$PROJECT_ROOT/.claude/state"
LOG_FILE="$LOG_DIR/subagent-reconciliation.jsonl"
ALERT_LOG="$LOG_DIR/credential_alerts.jsonl"
# v0.2.73 HIGH-2: the session-agnostic drain queue the Stop-hook batched drain
# consumes (replaces the orphan code-graph-queue.jsonl that nothing drained).
CG_DRAIN_SHARED="$STATE_DIR/codegraph_drain_shared.txt"

mkdir -p "$LOG_DIR" 2>/dev/null || exit 0
mkdir -p "$STATE_DIR" 2>/dev/null || true

# --job <file>: the DETACHED half (spawned below). The payload rides a 0600
# job file — never argv (it is the harness payload; R31 discipline).
JOB_FILE=""
if [ "${1:-}" = "--job" ] && [ -n "${2:-}" ]; then
    JOB_FILE="$2"
fi
if [ -n "$JOB_FILE" ]; then
    # Only a job file this hook writes is read (and then deleted): a plain
    # file named subagent-reconcile-job.*.json directly in $STATE_DIR. Any
    # other path on argv (a manual run, a diagnostic) is refused, so --job
    # can never read or delete an arbitrary file (review R4c-9).
    case "$JOB_FILE" in
        "$STATE_DIR"/subagent-reconcile-job.*.json) ;;
        *) echo "subagent-stop-reconcile: --job accepts only $STATE_DIR/subagent-reconcile-job.*.json; ignored: $JOB_FILE" >&2
           exit 0 ;;
    esac
    case "${JOB_FILE#"$STATE_DIR"/}" in
        */*) echo "subagent-stop-reconcile: --job path not directly in $STATE_DIR; ignored" >&2; exit 0 ;;
    esac
    if [ -L "$JOB_FILE" ] || [ ! -f "$JOB_FILE" ]; then
        exit 0
    fi
    HOOK_STDIN=$(cat "$JOB_FILE" 2>/dev/null || echo "")
    rm -f "$JOB_FILE" 2>/dev/null || true
else
    HOOK_STDIN=$(cat 2>/dev/null || echo "")
fi
[ -z "$HOOK_STDIN" ] && exit 0

# Parse the SubagentStop payload — we need agent_id + session_id +
# transcript_path + agent_type + stop_reason for the audit row, plus
# agent_id and session_id for the new reconciliation steps. Field-
# synonym tolerance for transcript_path mirrors how the docs describe
# it (agent_transcript_path in some payloads, plain transcript_path in
# others). We emit whichever is present; both missing → empty string.
#
# v0.2.71 Track T-WT (Layer 3b): also parse the isolation flag + the
# subagent's cwd/worktree path when the payload exposes them. These drive
# the post-hoc worktree-violation alert below (the last-chance detector when
# WorktreeCreate + SubagentStart both missed). Both are optional — when
# absent the violation check no-ops gracefully.
PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)

def truthy(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('worktree', 'true', '1', 'yes', 'isolated')
    return False

iso = 0
val = d.get('isolation')
if isinstance(val, str) and val.strip().lower() == 'worktree':
    iso = 1
elif truthy(val):
    iso = 1
if not iso:
    for f in ('isolation_mode', 'worktree', 'isolated', 'worktree_isolation'):
        if truthy(d.get(f)):
            iso = 1
            break
cwd = (d.get('cwd') or d.get('worktree_path') or d.get('working_directory')
       or d.get('working_dir') or d.get('dir') or '')

sys.stdout.write(str(d.get('session_id', '') or '') + '\n')
sys.stdout.write(str(d.get('agent_id', '') or '') + '\n')
sys.stdout.write(str(d.get('agent_type', '') or '') + '\n')
sys.stdout.write(str(d.get('agent_transcript_path') or d.get('transcript_path') or '') + '\n')
sys.stdout.write(str(d.get('finish_reason') or d.get('stop_reason') or '') + '\n')
sys.stdout.write(str(iso) + '\n')
sys.stdout.write(str(cwd))
" 2>/dev/null || printf '\n\n\n\n\n0\n')

SESSION_ID="$(printf '%s' "$PARSED" | sed -n '1p')"
AGENT_ID="$(printf '%s' "$PARSED" | sed -n '2p')"
AGENT_TYPE="$(printf '%s' "$PARSED" | sed -n '3p')"
TRANSCRIPT_PATH="$(printf '%s' "$PARSED" | sed -n '4p')"
STOP_REASON="$(printf '%s' "$PARSED" | sed -n '5p')"
ISO_FLAG="$(printf '%s' "$PARSED" | sed -n '6p')"
ISO_CWD="$(printf '%s' "$PARSED" | sed -n '7p')"

# Step 1 (preserved from V52-L.2): emit the audit row — FOREGROUND only
# (the detached job re-parses the payload but never writes a second row).
# Build the JSONL line in Python so every field is properly escaped.
JSONL=$(SESSION_ID_FOR_PY="$SESSION_ID" \
        AGENT_ID_FOR_PY="$AGENT_ID" \
        AGENT_TYPE_FOR_PY="$AGENT_TYPE" \
        TRANSCRIPT_FOR_PY="$TRANSCRIPT_PATH" \
        STOP_REASON_FOR_PY="$STOP_REASON" \
        "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "session_id": os.environ.get("SESSION_ID_FOR_PY", ""),
    "agent_id": os.environ.get("AGENT_ID_FOR_PY", ""),
    "agent_type": os.environ.get("AGENT_TYPE_FOR_PY", ""),
    "transcript_path": os.environ.get("TRANSCRIPT_FOR_PY", ""),
    "stop_reason": os.environ.get("STOP_REASON_FOR_PY", ""),
}))
' 2>/dev/null)
if [ -n "$JSONL" ] && [ -z "$JOB_FILE" ]; then
    printf '%s\n' "$JSONL" >> "$LOG_FILE" 2>/dev/null || true
fi


# ---------------------------------------------------------------------------
# DETACH (v0.2.102 W6-C2, census F6/L1): everything below runs in ONE
# detached job — no harness deadline can kill the diff, the reap or a sync.
# ---------------------------------------------------------------------------
_vco_reconcile_inline() {
    case "${VCO_SUBAGENT_RECONCILE_INLINE:-}" in
        1|true|TRUE|True|yes|YES|Yes|on|ON|On) return 0 ;;
    esac
    return 1
}
if [ -z "$JOB_FILE" ] && ! _vco_reconcile_inline; then
    # Nothing below can act without an agent id (reap, snapshot, alerts).
    [ -n "$AGENT_ID" ] || exit 0
    # A job file whose job never started (spawn failed, machine went down)
    # is garbage after a day; a younger one may still be about to be read.
    find "$STATE_DIR" -maxdepth 1 -type f -name 'subagent-reconcile-job.*.json' \
        -mtime +0 -delete 2>/dev/null || true
    _SAFE_JOB_ID="$(printf '%s' "$AGENT_ID" | tr -c 'a-zA-Z0-9_-' '_' | head -c 64)"
    JOB="$STATE_DIR/subagent-reconcile-job.${_SAFE_JOB_ID}.$$.json"
    # The job's own output goes to a per-job log (never the hook's stdout /
    # stderr: a child holding those keeps the harness's pipe open until the
    # job ends, which is not detached at all). Per job, so concurrent jobs
    # never share a file; swept after a week. A failure the job reports on
    # stderr (an alert it could not file) is kept there.
    JOB_LOG_DIR="$LOG_DIR/subagent-reconcile-jobs"
    mkdir -p "$JOB_LOG_DIR" 2>/dev/null || JOB_LOG_DIR=""
    JOB_OUT=/dev/null
    JOB_ERR=/dev/null
    if [ -n "$JOB_LOG_DIR" ]; then
        find "$JOB_LOG_DIR" -maxdepth 1 -type f -name 'job.*' -mtime +6 -delete 2>/dev/null || true
        JOB_OUT="$JOB_LOG_DIR/job.${_SAFE_JOB_ID}.$$.out"
        JOB_ERR="$JOB_LOG_DIR/job.${_SAFE_JOB_ID}.$$.err"
    fi
    if ( umask 077; printf '%s' "$HOOK_STDIN" > "$JOB" ) 2>/dev/null; then
        if command -v setsid >/dev/null 2>&1; then
            CLAUDE_PROJECT_DIR="$PROJECT_ROOT" setsid bash "$SCRIPT_DIR/subagent-stop-reconcile.sh" \
                --job "$JOB" >"$JOB_OUT" 2>"$JOB_ERR" < /dev/null &
        elif command -v nohup >/dev/null 2>&1; then
            CLAUDE_PROJECT_DIR="$PROJECT_ROOT" nohup bash "$SCRIPT_DIR/subagent-stop-reconcile.sh" \
                --job "$JOB" >"$JOB_OUT" 2>"$JOB_ERR" < /dev/null &
            disown 2>/dev/null || true
        else
            ( CLAUDE_PROJECT_DIR="$PROJECT_ROOT" bash "$SCRIPT_DIR/subagent-stop-reconcile.sh" \
                --job "$JOB" ) >"$JOB_OUT" 2>"$JOB_ERR" < /dev/null &
        fi
    fi
    exit 0
fi

# ---- the job (detached; inline under VCO_SUBAGENT_RECONCILE_INLINE) ------
# shellcheck source=_lib/session-id.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/session-id.sh" ] && . "$SCRIPT_DIR/_lib/session-id.sh"

# File one PARENT-facing alert (census F8). $1 kind, $2 message, $3 optional
# file. One writer: _lib/agent-alerts.sh -> python -m vco_lib.agent_alerts
# append (row format + locking live there; review R4c-2/R4c-3). No-op for no
# agent. When the alert cannot be filed (broken install: no VCO venv) it is
# never dropped silently: stderr, plus a desktop notification carrying the
# alert itself, since a detached job's stderr reaches nobody.
# shellcheck source=_lib/agent-alerts.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/agent-alerts.sh" ] && . "$SCRIPT_DIR/_lib/agent-alerts.sh"
_reconcile_parent_alert() {
    [ -n "$AGENT_ID" ] || return 0
    if command -v vco_agent_alert_append >/dev/null 2>&1 && \
        vco_agent_alert_append "$SCRIPT_DIR" "$PROJECT_ROOT" "$SESSION_ID" "$AGENT_ID" \
            "$AGENT_TYPE" "$1" "$2" "${3:-}"; then
        return 0
    fi
    echo "subagent-stop-reconcile: alert NOT delivered to the parent: $2" >&2
    if [ -n "${PY:-}" ] && [ -f "$PROJECT_ROOT/.claude/scripts/notify.py" ]; then
        "$PY" "$PROJECT_ROOT/.claude/scripts/notify.py" \
            "VCO: subagent alert NOT delivered (broken install)" "$2" \
            --urgency critical --icon dialog-warning 2>/dev/null || true
    fi
    return 1
}

# v0.2.102 W6-R: the live SubagentStop payload carries none of the isolation
# fields parsed above, so on its own ISO_FLAG never becomes 1 and Layer 3b
# could never fire. Derive it the way subagent-start-isolation-check does,
# from worktree-guard's per-launch record and the agent's definition
# (_lib/worktree-records.sh — one home for both hooks):
#   * a record with mode head|snapshot|reservation|reservation_existing → the
#     guard gave this agent its own worktree: isolation happened, so stay
#     silent (ISO_FLAG=0);
#   * no record, guard registered, definition declares `isolation: worktree`
#     → isolation was skipped: ISO_FLAG=1 and the working directory is the
#     parent checkout (ISO_CWD cleared, so the cwd exemption cannot apply);
#   * anything else (in_place record, unknown definition, plugin type, guard
#     parked) → the payload-derived value stands.
# Read BEFORE Step 1b's reap, which rewrites the record's status.
if [ -n "$AGENT_ID" ] && [ -f "$SCRIPT_DIR/_lib/worktree-records.sh" ]; then
    # shellcheck source=_lib/worktree-records.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/worktree-records.sh"
    if ISO_REC="$(wtrec_find "$PROJECT_ROOT" "$AGENT_ID")"; then
        case "$(wtrec_get "$ISO_REC" mode)" in
            # reservation*: a lane-reservation worktree in the target repo
            # (v0.2.102 W6-AA) — isolated too.
            head|snapshot|reservation|reservation_existing)
                ISO_FLAG=0
                # W6-C2 F7: this agent worked in its OWN worktree.
                AGENT_WT_ISOLATED=1 ;;
        esac
    elif wtrec_isolation_skipped "$PROJECT_ROOT" "$AGENT_ID" "$AGENT_TYPE"; then
        ISO_FLAG=1
        ISO_CWD=""
    fi
fi

# Step 1b (v0.2.102 W6-Q follow-up): reap the agent's worktree when it is
# provably unchanged. worktree-guard creates every isolated agent's worktree,
# and for a hook-created worktree the harness neither removes it nor runs
# WorktreeRemove when a subagent finishes
# (https://code.claude.com/docs/en/hooks#worktreeremove) — so without this
# step every launch left one behind. The decision (and every condition that
# keeps a worktree) lives in _lib/worktree-records.sh :: wtrec_reap. A kept
# worktree is logged with its path and reason to
# .claude/logs/worktree-guard.jsonl and named on stderr. Runs before the
# snapshot reconciler's early exits so it never depends on them; never
# blocks (SubagentStop context would keep the subagent running).
if [ -n "$AGENT_ID" ] && [ -f "$SCRIPT_DIR/_lib/worktree-records.sh" ]; then
    # shellcheck source=_lib/worktree-records.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/worktree-records.sh"
    REAP="$(wtrec_reap "$PROJECT_ROOT" "$AGENT_ID" 2>/dev/null || true)"
    if [ -n "$REAP" ]; then
        REAP_DECISION="$(printf '%s' "$REAP" | cut -f1)"
        REAP_REASON="$(printf '%s' "$REAP" | cut -f2)"
        REAP_PATH="$(printf '%s' "$REAP" | cut -f3-)"
        AGENT_ID_FOR_PY="$AGENT_ID" DEC_FOR_PY="$REAP_DECISION" REASON_FOR_PY="$REAP_REASON" \
        PATH_FOR_PY="$REAP_PATH" OUT_FOR_PY="$LOG_DIR/worktree-guard.jsonl" "$PY" -c '
import json, os
from datetime import datetime, timezone
e = os.environ
row = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
       "hook": "subagent-stop-reconcile", "decision": "reap_" + e["DEC_FOR_PY"],
       "reason": e["REASON_FOR_PY"], "agent_id": e["AGENT_ID_FOR_PY"],
       "resolved_path": e["PATH_FOR_PY"]}
with open(e["OUT_FOR_PY"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(row) + "\n")
' 2>/dev/null || true
        if [ "$REAP_DECISION" = "kept" ]; then
            printf 'subagent-stop-reconcile: worktree of agent %s KEPT at %s (%s) - review, merge or remove it\n' \
                "$AGENT_ID" "$REAP_PATH" "$REAP_REASON" >&2
            # census F8: SubagentStop stderr on exit 0 reaches no model —
            # file it for the PARENT, which must review/merge/remove it.
            _reconcile_parent_alert worktree_kept \
                "worktree KEPT at $REAP_PATH ($REAP_REASON): review its changes, then merge or remove it" \
                "$REAP_PATH"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Steps 2-5 (V52-L.1 reconciler): only with a usable AGENT_ID. W6-C2 F7: the
# changed-file set comes from the agent's OWN worktree when it had one,
# else from the shared-tree snapshot diff (unattributed).
# ---------------------------------------------------------------------------
[ -n "$AGENT_ID" ] || exit 0

SNAP_FILE="$STATE_DIR/subagent-snapshot-$(printf '%s' "$AGENT_ID" | tr -c 'a-zA-Z0-9_-' '_' | head -c 64).json"
# Cap to MAX_DIFF_FILES to protect against runaway scans on a subagent
# that touched thousands of files (rare but possible — e.g. an agent
# that did a tree-wide formatter pass). Default 500 files; tunable via
# VCT_SUBAGENT_MAX_DIFF env var.
MAX_DIFF_FILES="${VCT_SUBAGENT_MAX_DIFF:-500}"

ATTRIBUTION=""
CHANGED_FILES=""    # project-relative (shared tree) — Layer 3b lists these
CHANGED_ABS=""      # absolute, one per line — every step below reads these
if [ "${AGENT_WT_ISOLATED:-0}" = "1" ] && command -v wtrec_changed_files >/dev/null 2>&1; then
    ATTRIBUTION="agent_worktree"
    CHANGED_ABS="$(wtrec_changed_files "$PROJECT_ROOT" "$AGENT_ID" 2>/dev/null | head -n "$MAX_DIFF_FILES")"
    # A snapshot from an older SubagentStart is not this agent's evidence.
    if command -v cleanup_snapshot >/dev/null 2>&1; then
        cleanup_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null || true
    fi
else
    if ! command -v diff_snapshot >/dev/null 2>&1 || [ ! -f "$SNAP_FILE" ]; then
        exit 0
    fi
    ATTRIBUTION="unattributed_shared_tree"
    CHANGED_FILES=$(diff_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null \
        | head -n "$MAX_DIFF_FILES")
    CHANGED_ABS="$(printf '%s\n' "$CHANGED_FILES" | sed '/^$/d' | while IFS= read -r _rel; do
        printf '%s/%s\n' "$PROJECT_ROOT" "$_rel"; done)"
fi

# Quick exit when nothing changed — happy path. Cleanup + exit.
if [ -z "$CHANGED_ABS" ]; then
    if command -v cleanup_snapshot >/dev/null 2>&1; then
        cleanup_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null || true
    fi
    exit 0
fi

# Tally diff stats for the nudge counter (computed before we mutate
# the file set). file_count + total_bytes are the two inputs to the
# work-unit estimate written into the shared kg_update_tokens.jsonl at
# step 5.
FILE_COUNT=0
TOTAL_BYTES=0
KG_FILES=()     # absolute; shared tree only (knowledge/** and docs/**.md)
CODE_FILES=()   # project-relative; shared tree only
ALL_FILES=()    # absolute; every changed file (credential scan)

# Code-file extension set (mirrors snapshot.sh's _SNAPSHOT_CODE_EXTS_DEFAULT).
# Used here to classify which queue/sync path each modified file lands in.
_CODE_EXT_RE='\.(py|rs|ts|tsx|js|jsx|go|java|cs|c|cpp|h|hpp|rb|php|swift|kt|scala|sh|ps1|sql)$'

while IFS= read -r abs; do
    [ -z "$abs" ] && continue
    FILE_COUNT=$((FILE_COUNT + 1))
    # Compute file size for the nudge weight; missing/deleted files
    # contribute 0 bytes (which is the correct accounting — deletion is
    # also a work signal, but a small one).
    if [ -f "$abs" ]; then
        sz=$(stat -c '%s' "$abs" 2>/dev/null || stat -f '%z' "$abs" 2>/dev/null || echo "0")
        TOTAL_BYTES=$((TOTAL_BYTES + ${sz:-0}))
    fi
    ALL_FILES+=("$abs")
    # Indexing happens for SHARED-TREE files only: a worktree agent's files
    # reach the KG / code graph when its work is merged (and the FIX-A' gate
    # drops ephemeral-worktree edits anyway).
    [ "$ATTRIBUTION" = "agent_worktree" ] && continue
    rel="${abs#"$PROJECT_ROOT"/}"
    # Classify: knowledge/** or docs/**.md → KG/docs sync, code-extension →
    # code-graph queue. Regex rather than case-glob so the rule is consistent
    # regardless of bash globstar setting.
    if printf '%s' "$rel" | grep -qE '^knowledge/.*\.md$|^docs/.*\.md$'; then
        KG_FILES+=("$abs")
    fi
    if printf '%s' "$rel" | grep -qE "$_CODE_EXT_RE"; then
        CODE_FILES+=("$rel")
    fi
done <<CHANGEDEOF
$CHANGED_ABS
CHANGEDEOF

# Reconciliation row: what was found and HOW it is attributed (census F7).
# Its own log, so the V52-L.2 audit log keeps exactly one row per stop.
FILES_LOG="$LOG_DIR/subagent-reconcile-files.jsonl"
FILES_FOR_PY="$CHANGED_ABS" AGENT_ID_FOR_PY="$AGENT_ID" SESSION_ID_FOR_PY="$SESSION_ID" \
AGENT_TYPE_FOR_PY="$AGENT_TYPE" ATTR_FOR_PY="$ATTRIBUTION" "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
e = os.environ
files = [f for f in e.get("FILES_FOR_PY", "").splitlines() if f.strip()]
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "event": "reconciled", "session_id": e.get("SESSION_ID_FOR_PY", ""),
    "agent_id": e.get("AGENT_ID_FOR_PY", ""), "agent_type": e.get("AGENT_TYPE_FOR_PY", ""),
    "attribution": e.get("ATTR_FOR_PY", ""), "file_count": len(files),
    "changed_files": files[:50]}) + "\n")
' >> "$FILES_LOG" 2>/dev/null || true

# -------------------- Step 2: KG / docs sync --------------------
# Every changed knowledge/** / docs/**.md file goes through the ONE routing
# home (_lib/route-touched-path.sh): the access-matrix gate, the per-file
# debounce and the detached runner (no timeout — a slow sync finishes).
# The subagent's identity is exported so a failed sync's once-per-session
# sentinel and its failure row are attributed to it (census F15/F16).
if [ "${#KG_FILES[@]}" -gt 0 ]; then
    if [ -f "$SCRIPT_DIR/_lib/route-touched-path.sh" ]; then
        # shellcheck source=_lib/route-touched-path.sh disable=SC1091
        . "$SCRIPT_DIR/_lib/route-touched-path.sh"
        export VCT_AGENT_ID="$AGENT_ID"
        [ -n "$AGENT_TYPE" ] && export VCT_AGENT_TYPE="$AGENT_TYPE"
        [ -n "$SESSION_ID" ] && export VCT_SESSION_ID="$SESSION_ID"
        vco_route_init "$SCRIPT_DIR" "$PROJECT_ROOT" "$PY"
        for kf in "${KG_FILES[@]}"; do
            vco_route_touched_path "$kf" "$SESSION_ID" "$AGENT_ID"
        done
        cd "$PROJECT_ROOT" 2>/dev/null || true
    else
        # A missing routing lib is a BROKEN install, not "nothing to sync":
        # record it where the reconciliation is recorded.
        printf '{"event":"kg_sync_skipped","reason":"_lib/route-touched-path.sh missing","agent_id":"%s"}\n' \
            "$(printf '%s' "$AGENT_ID" | tr -c 'a-zA-Z0-9_-' '_')" >> "$FILES_LOG" 2>/dev/null || true
    fi
fi

# -------------------- Step 3: Code-graph drain enqueue --------------------
# Route modified code files into the SAME batched, worktree-gated path the
# main-session Stop drain (stop-codegraph-drain.{sh,ps1}) consumes — NOT the
# old orphan code-graph-queue.jsonl (nothing ever drained it, so subagent code
# edits never reached the code graph AND that file grew unbounded — v0.2.73
# I/O-audit HIGH-2).
#
# We apply the FIX-A' worktree gate here so an EPHEMERAL/unregistered worktree
# edit (the exact multi-agent-worktree churn v0.2.73 kills) is DROPPED — the
# main session re-indexes the merged result. A gated-IN file is appended
# (newline-delimited, ABSOLUTE path — matching the drain's format + how
# post-file-edit.sh stores paths) to the session-agnostic shared drain queue.
#
# Performance: the gate's registered-project probe shells out to
# vct_project_config.sh once per DISTINCT canonical root (not per file), so a
# subagent touching many files under one repo pays ONE probe. We NEVER run the
# analyzer synchronously (embedding-model init is 5-30s, over the <10s
# budget); we only ENQUEUE. Soft-fail throughout.
if [ "${#CODE_FILES[@]}" -gt 0 ]; then
    # Per-canonical-root gate cache: canon-root -> "SKIP" | "INDEX". Resolve +
    # gate ONCE per distinct canonical root; reuse the verdict for every file
    # sharing that root. (Associative arrays need bash 4+; fall back to a
    # linear pair-list scan for bash 3.2 / macOS system bash.)
    _CG_GATE_ROOTS=()   # parallel arrays: root[i] -> verdict[i]
    _CG_GATE_VERDICTS=()
    _cg_gate_lookup() {
        # $1 = canonical root; echoes cached verdict or empty if unseen.
        _i=0
        while [ "$_i" -lt "${#_CG_GATE_ROOTS[@]}" ]; do
            if [ "${_CG_GATE_ROOTS[$_i]}" = "$1" ]; then
                printf '%s' "${_CG_GATE_VERDICTS[$_i]}"; return 0
            fi
            _i=$((_i + 1))
        done
        printf ''
    }

    _CG_ENQUEUE=()
    for _rel in "${CODE_FILES[@]}"; do
        [ -n "$_rel" ] || continue
        _abs="$PROJECT_ROOT/$_rel"

        # Resolve the canonical MAIN root for this file. When the resolver lib
        # is absent or resolution fails, _canon stays empty → the gate treats
        # it as "not a worktree" (INDEX) — conservative (never drop on doubt).
        _canon=""
        if command -v _canonical_repo_root >/dev/null 2>&1; then
            _canon="$(_canonical_repo_root "$_abs" 2>/dev/null)" || _canon=""
        fi

        # Cached gate verdict for this canonical root?
        _verdict="$(_cg_gate_lookup "$_canon")"
        if [ -z "$_verdict" ]; then
            # Compute once for this root. REPO_PATH = PROJECT_ROOT (the on-disk
            # session root); the gate compares it against the canonical root.
            # SKIP only on a definitive "ephemeral/unregistered worktree";
            # INDEX otherwise (incl. main tree, registered worktree, no gate).
            if command -v _worktree_gate_should_skip >/dev/null 2>&1 \
                && _worktree_gate_should_skip "$_abs" "$PROJECT_ROOT" "$_canon"; then
                _verdict="SKIP"
            else
                _verdict="INDEX"
            fi
            _CG_GATE_ROOTS+=("$_canon")
            _CG_GATE_VERDICTS+=("$_verdict")
        fi

        [ "$_verdict" = "INDEX" ] && _CG_ENQUEUE+=("$_abs")
    done

    # Append the gated-IN absolute paths to the session-agnostic shared drain
    # queue. Plain newline-delimited append (the drain reads paths, one per
    # line) — matches post-file-edit.sh's convention. Soft-fail on any error.
    if [ "${#_CG_ENQUEUE[@]}" -gt 0 ]; then
        printf '%s\n' "${_CG_ENQUEUE[@]}" >> "$CG_DRAIN_SHARED" 2>/dev/null || true
    fi
fi

# -------------------- Step 4: Credential scan --------------------
# Run the shared scanner against every modified file. On hit, append
# to credential_alerts.jsonl using the same schema as post-tool-
# security.sh so existing consumers (the launcher Security tab) see
# the alerts without needing schema-aware code.
if command -v scan_file_for_credentials >/dev/null 2>&1 && [ "${#ALL_FILES[@]}" -gt 0 ]; then
    for abs in "${ALL_FILES[@]}"; do
        # Skip deleted files (they cannot have credentials anymore;
        # the file-existence check inside scan_file_for_credentials
        # already short-circuits, but skipping early saves the call).
        [ -f "$abs" ] || continue
        hits=$(scan_file_for_credentials "$abs" 2>/dev/null | tr '\n' '|' | sed 's/|$//')
        if [ -n "$hits" ]; then
            JSONL_ALERT=$(EDITED_FILE_FOR_PY="$abs" \
                          PATTERNS_FOR_PY="${hits//|/ }" \
                          SESSION_ID_FOR_PY="$SESSION_ID" \
                          AGENT_ID_FOR_PY="$AGENT_ID" \
                          AGENT_TYPE_FOR_PY="$AGENT_TYPE" \
                          SOURCE_FOR_PY="subagent_stop_reconciler" \
                          ATTR_FOR_PY="$ATTRIBUTION" \
                          "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
sys.stdout.write(json.dumps({
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "file": os.environ.get("EDITED_FILE_FOR_PY", ""),
    "patterns": os.environ.get("PATTERNS_FOR_PY", ""),
    "session_id": os.environ.get("SESSION_ID_FOR_PY", ""),
    "agent_id": os.environ.get("AGENT_ID_FOR_PY", ""),
    "agent_type": os.environ.get("AGENT_TYPE_FOR_PY", ""),
    "source": os.environ.get("SOURCE_FOR_PY", "subagent_stop_reconciler"),
    "attribution": os.environ.get("ATTR_FOR_PY", ""),
}))
' 2>/dev/null)
            if [ -n "$JSONL_ALERT" ]; then
                printf '%s\n' "$JSONL_ALERT" >> "$ALERT_LOG" 2>/dev/null || true
            fi
            # census F8: the PARENT must hear about it (pattern NAMES only).
            _reconcile_parent_alert credential \
                "possible credential in ${abs#"$PROJECT_ROOT"/} (patterns: ${hits//|/ }; found by the SubagentStop scan, $ATTRIBUTION): review it before committing" \
                "$abs"
        fi
    done
fi

# -------------------- Step 5: Subagent work record --------------------
# Record subagent work on the parent session's row in the kg-update-nudge
# counter file, as `subagent_work_units` (+ subagent_count / subagent_ids).
# The field is for the per-subagent KG nudge the owner scheduled for
# v0.2.103 (each subagent nudged on its own work); the main-chat
# kg-update-nudge deliberately does NOT read it (owner ruling 2026-10-13:
# the main-chat nudge ignores subagent work), so today it changes no
# nudge. The counter file lives under the machine-wide metrics dir
# (not the project's .claude/) because nudge state is per-session, not
# per-project; kg-update-nudge.sh resolves the same path through the same
# `_lib/metrics-dir.sh` helper. v0.2.92 W7 moved that home out of
# ~/.claude — both hooks moved together, which is why they still agree.
#
# Work-unit estimate proxy: file_count * 50 + total_bytes / 4. 50 per
# file approximates the "agent had to think about it" overhead;
# bytes/4 approximates output tokens (~4 chars/token English).
# This is a rough, order-of-magnitude metric.
_SSR_LIB="$(dirname "${BASH_SOURCE[0]}")/_lib/metrics-dir.sh"
if [ -n "$SESSION_ID" ] && [ "$FILE_COUNT" -gt 0 ] && [ -f "$_SSR_LIB" ]; then
    # shellcheck source=_lib/metrics-dir.sh disable=SC1091
    . "$_SSR_LIB"
    NUDGE_DIR="$(vco_metrics_dir 2>/dev/null || printf '')"
    NUDGE_FILE="${NUDGE_DIR:+$NUDGE_DIR/kg_update_tokens.jsonl}"
    # Counter continuity across the W7 move — seed from the frozen archive
    # when the new file does not exist yet, exactly as kg-update-nudge does,
    # so a subagent's work is added to the session's real baseline instead of
    # to a fresh zero. Read-only against the archive.
    if [ -n "$NUDGE_FILE" ] && [ ! -f "$NUDGE_FILE" ]; then
        _SSR_LEGACY="$(vco_metrics_read_file "kg_update_tokens.jsonl" 2>/dev/null || printf '')"
        if [ -n "$_SSR_LEGACY" ] && [ "$_SSR_LEGACY" != "$NUDGE_FILE" ]; then
            cp "$_SSR_LEGACY" "$NUDGE_FILE" 2>/dev/null || true
        fi
    fi
    WORK_UNITS=$(( FILE_COUNT * 50 + TOTAL_BYTES / 4 ))
    SESSION_ID_FOR_PY="$SESSION_ID" \
    WORK_UNITS_FOR_PY="$WORK_UNITS" \
    AGENT_ID_FOR_PY="$AGENT_ID" \
    NUDGE_FILE_FOR_PY="$NUDGE_FILE" \
    "$PY" -c '
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # no flock on this platform: proceed unlocked
    fcntl = None

session_id = os.environ.get("SESSION_ID_FOR_PY", "")
nudge_path = os.environ.get("NUDGE_FILE_FOR_PY", "")
try:
    work_units = int(os.environ.get("WORK_UNITS_FOR_PY", "0"))
except ValueError:
    work_units = 0
agent_id = os.environ.get("AGENT_ID_FOR_PY", "")
if not session_id or not nudge_path or work_units <= 0:
    sys.exit(0)

# kg-update-nudge.sh uses a one-row-per-session JSONL with last-line-
# wins semantics (state[session_id] = parsed_last_row). The simplest
# correct path is: read all rows, find the one matching our session_id
# (if any), bump its subagent_work_units, and rewrite the file
# atomically. We do NOT modify other fields — the nudge hook owns
# baseline / last_seen_total / fired_once / etc.
#
# When no row exists for our session yet, we synthesize a minimal row
# carrying just the subagent contribution. The main-chat nudge does not
# read the subagent_* fields (owner ruling 2026-10-13); they wait for the
# per-subagent nudge scheduled for v0.2.103.
# W6-C2 (census F5): hold the SAME stable sidecar lock kg-update-nudge.sh
# takes (flock on kg_update_tokens.jsonl.lock) for the whole read-modify-
# write — unlocked, this rewrite could drop a concurrent nudge update of
# baseline / fired_once (a background completion fires UserPromptSubmit right
# around SubagentStop). Lock unavailable → best-effort unlocked, like the nudge.
_lock_fd = None
if fcntl is not None:
    try:
        _lock_fd = os.open(nudge_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX)
    except OSError:
        _lock_fd = None

rows = []
existing = None
try:
    with open(nudge_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("session_id") == session_id:
                existing = entry
            rows.append(entry)
except OSError:
    pass

if existing is None:
    existing = {
        "session_id": session_id,
        "subagent_work_units": 0,
        "subagent_last_at": "",
        "subagent_count": 0,
    }
    rows.append(existing)

existing["subagent_work_units"] = (
    int(existing.get("subagent_work_units") or 0) + work_units
)
existing["subagent_last_at"] = datetime.now(timezone.utc).strftime(
    "%Y-%m-%dT%H:%M:%SZ"
)
existing["subagent_count"] = int(existing.get("subagent_count") or 0) + 1
if agent_id and "subagent_ids" in existing:
    if isinstance(existing["subagent_ids"], list):
        existing["subagent_ids"].append(agent_id)
elif agent_id:
    existing["subagent_ids"] = [agent_id]

# Deduplicate by session_id, keeping our updated row. Last-wins matches
# the nudge hook semantics.
seen = set()
final = []
for entry in rows:
    sid = entry.get("session_id")
    if sid == session_id:
        if sid in seen:
            continue
        seen.add(sid)
        final.append(existing)
    else:
        if sid in seen:
            continue
        seen.add(sid)
        final.append(entry)

# Atomic rewrite via tempfile + rename.
try:
    target_dir = os.path.dirname(nudge_path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".kg_update_tokens.", dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for entry in final:
                out.write(json.dumps(entry) + "\n")
        os.replace(tmp, nudge_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
except OSError:
    pass
finally:
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
' 2>/dev/null || true
fi

# -------------------- Layer 3b: worktree-violation alert --------------------
# v0.2.71 Track T-WT. Post-hoc detector: if this subagent requested
# `isolation: worktree` AND it modified files that the snapshot diff found
# under the PARENT checkout (diff_snapshot resolves paths relative to
# $PROJECT_ROOT — i.e. the parent's working tree), then the isolation
# silently fell back to the shared tree — exactly the 2026-06-30 incident.
# Write a `worktree_violation` row to .claude/logs/worktree-guard.jsonl
# (the same JSONL the WorktreeCreate guard writes) so the integrator has one
# place to diagnose. This is the last-chance detector when WorktreeCreate +
# SubagentStart both missed. Non-blocking, soft-fail.
#
# Guard conditions (all must hold to emit a violation):
#   - isolation was requested and skipped (ISO_FLAG == 1): derived after the
#     payload parse from worktree-guard's record + the agent's definition
#     (wtrec_isolation_skipped), or from a payload isolation flag when a
#     harness build exposes one; a guard record with mode head|snapshot
#     forces no-op (we don't guess non-isolation agents into violations),
#   - the diff found >0 changed files under the parent root (FILE_COUNT>0),
#   - the reported cwd (if any) was NOT a separate worktree (cwd == parent
#     toplevel OR cwd absent). When the build DOES expose a real worktree
#     cwd that differs from the toplevel, the changed files under the parent
#     are some OTHER write path (not the isolation fallback) and we stay
#     silent rather than false-alarm.
if [ "${ISO_FLAG:-0}" = "1" ] && [ "$FILE_COUNT" -gt 0 ]; then
    WT_LOG="$LOG_DIR/worktree-guard.jsonl"
    TOPLEVEL_WT=""
    if command -v git >/dev/null 2>&1; then
        TOPLEVEL_WT="$(git -C "$PROJECT_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
    fi
    # Decide whether the cwd indicates a genuine separate worktree.
    # Comparison base for "is the cwd the parent checkout?": prefer the git
    # toplevel, but when the project isn't a git repo (TOPLEVEL_WT empty) fall
    # back to PROJECT_ROOT so a reported separate-worktree cwd still exempts.
    _cmp_base="${TOPLEVEL_WT:-$PROJECT_ROOT}"
    CWD_IS_PARENT=1
    if [ -n "$ISO_CWD" ] && [ -n "$_cmp_base" ]; then
        _norm_cwd="$ISO_CWD"
        _norm_top="$_cmp_base"
        if command -v realpath >/dev/null 2>&1; then
            _norm_cwd="$(realpath -m "$ISO_CWD" 2>/dev/null || printf '%s' "$ISO_CWD")"
            _norm_top="$(realpath -m "$_cmp_base" 2>/dev/null || printf '%s' "$_cmp_base")"
        fi
        # cwd differs from the parent checkout root ⇒ a real separate worktree
        # was used; the parent-root writes came from somewhere else ⇒ no alert.
        [ "$_norm_cwd" != "$_norm_top" ] && CWD_IS_PARENT=0
    fi
    if [ "$CWD_IS_PARENT" -eq 1 ]; then
        REASON="isolation:worktree agent modified $FILE_COUNT file(s) under the parent checkout (${TOPLEVEL_WT:-$PROJECT_ROOT}) — silent shared-tree fallback detected post-hoc"
        printf '%s\n' "$CHANGED_FILES" \
            | AGENT_ID_FOR_PY="$AGENT_ID" \
              SESSION_ID_FOR_PY="$SESSION_ID" \
              AGENT_TYPE_FOR_PY="$AGENT_TYPE" \
              REASON_FOR_PY="$REASON" \
              TOPLEVEL_FOR_PY="${TOPLEVEL_WT:-$PROJECT_ROOT}" \
              CWD_FOR_PY="$ISO_CWD" \
              FILE_COUNT_FOR_PY="$FILE_COUNT" \
              WT_LOG_FOR_PY="$WT_LOG" \
              "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
out = os.environ.get("WT_LOG_FOR_PY", "")
if not out:
    sys.exit(0)
files = [l.rstrip("\r\n") for l in sys.stdin if l.strip()]
try:
    fc = int(os.environ.get("FILE_COUNT_FOR_PY", "0"))
except ValueError:
    fc = len(files)
row = {
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "hook": "subagent-stop-reconcile",
    "decision": "worktree_violation",
    "reason": os.environ.get("REASON_FOR_PY", ""),
    "session_id": os.environ.get("SESSION_ID_FOR_PY", ""),
    "agent_id": os.environ.get("AGENT_ID_FOR_PY", ""),
    "agent_type": os.environ.get("AGENT_TYPE_FOR_PY", ""),
    "parent_toplevel": os.environ.get("TOPLEVEL_FOR_PY", ""),
    "reported_cwd": os.environ.get("CWD_FOR_PY", ""),
    "file_count": fc,
    # Cap the file list so a tree-wide pass cannot bloat the row.
    "changed_files": files[:50],
}
try:
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
except OSError:
    pass
' 2>/dev/null || true
        printf 'subagent-stop-reconcile: WORKTREE VIOLATION — %s\n' "$REASON" >&2
        _reconcile_parent_alert worktree_violation "WORKTREE VIOLATION: $REASON"
    fi
fi

# Cleanup: delete the snapshot file (we are done with it). Soft-fail.
if command -v cleanup_snapshot >/dev/null 2>&1; then
    cleanup_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null || true
fi

exit 0
