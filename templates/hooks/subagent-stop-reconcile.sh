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
# V52-L.1 (v0.2.52): belt-and-suspenders reconciler. Five side effects:
#   1. JSONL audit log (V52-L.2 baseline; format unchanged).
#   2. KG sync — for any modified `knowledge/**/*.md` file, queue an
#      incremental kg-sync run. Best-effort, 30s timeout per file.
#   3. Code-graph drain enqueue — for any modified code file, apply the
#      FIX-A' worktree gate (drop edits in ephemeral/unregistered
#      worktrees) and append the gated-IN paths to the SESSION-AGNOSTIC
#      drain queue `.claude/state/codegraph_drain_shared.txt` that the
#      Stop-hook batched drain (stop-codegraph-drain.{sh,ps1}) consumes.
#      We never run analyze synchronously (joern + sentence-transformer
#      init are too slow) and we never write the old orphan
#      code-graph-queue.jsonl (nothing ever drained it — v0.2.73 HIGH-2).
#   4. Credential scan — run the shared `_lib/credscan.sh` scanner on
#      every modified file. On hit, append an entry to
#      `.claude/logs/credential_alerts.jsonl` with the same schema
#      post-tool-security.sh uses.
#   5. Nudge counter — bump the parent session's row in
#      `~/.claude/metrics/kg_update_tokens.jsonl` so the
#      kg-update-nudge hook's threshold accounts for subagent work.
#      Work-unit estimate = file_count * 50 + total_diff_size_bytes/4
#      (rough proxy for output tokens).
#
# File-modification discovery (V52-L.1 spec, Option 2):
#   - SubagentStart hook (subagent-start-kg-inject.sh) writes a
#     SHA-256 snapshot of watched paths to
#     `.claude/state/subagent-snapshot-<agent_id>.json`.
#   - This hook diffs against the snapshot to identify modified files.
#   - When the snapshot is missing (SubagentStart hook not installed,
#     state dir wiped, etc.), the reconciler degrades to logging-only
#     and exits without doing any of the new side effects.
#
# Soft-fail contract: every step is wrapped to never raise. The hook
# MUST exit 0 even when every side effect fails — the subagent has
# already terminated, so blocking on this hook would only hurt the
# user. Performance budget: <2s in the happy path (no modifications),
# <10s with modifications.

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

HOOK_STDIN=$(cat 2>/dev/null || echo "")
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

# Step 1 (preserved from V52-L.2): emit the audit row.
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
if [ -n "$JSONL" ]; then
    printf '%s\n' "$JSONL" >> "$LOG_FILE" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Steps 2-5 (V52-L.1 reconciler): only when we have a usable AGENT_ID
# AND the snapshot helper sourced successfully. Otherwise short-circuit
# silently to logging-only mode.
# ---------------------------------------------------------------------------
if [ -z "$AGENT_ID" ] || ! command -v diff_snapshot >/dev/null 2>&1; then
    exit 0
fi

SNAP_FILE="$STATE_DIR/subagent-snapshot-$(printf '%s' "$AGENT_ID" | tr -c 'a-zA-Z0-9_-' '_' | head -c 64).json"
[ ! -f "$SNAP_FILE" ] && exit 0

# Compute the diff. The diff function emits one relative path per line.
# Cap to MAX_DIFF_FILES to protect against runaway scans on a subagent
# that touched thousands of files (rare but possible — e.g. an agent
# that did a tree-wide formatter pass). Default 500 files; tunable via
# VCT_SUBAGENT_MAX_DIFF env var.
MAX_DIFF_FILES="${VCT_SUBAGENT_MAX_DIFF:-500}"

CHANGED_FILES=$(diff_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null \
    | head -n "$MAX_DIFF_FILES")

# Quick exit when nothing changed — happy path. Cleanup + exit.
if [ -z "$CHANGED_FILES" ]; then
    if command -v cleanup_snapshot >/dev/null 2>&1; then
        cleanup_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null || true
    fi
    exit 0
fi

# Tally diff stats for the nudge counter (computed before we mutate
# the file set). file_count + total_bytes are the two inputs to the
# work-unit estimate written into ~/.claude/metrics/kg_update_tokens.jsonl
# at step 5.
FILE_COUNT=0
TOTAL_BYTES=0
KG_FILES=()
CODE_FILES=()
ALL_FILES=()

# Code-file extension set (mirrors snapshot.sh's _SNAPSHOT_CODE_EXTS_DEFAULT).
# Used here to classify which queue/sync path each modified file lands in.
_CODE_EXT_RE='\.(py|rs|ts|tsx|js|jsx|go|java|cs|c|cpp|h|hpp|rb|php|swift|kt|scala|sh|ps1|sql)$'

while IFS= read -r rel; do
    [ -z "$rel" ] && continue
    FILE_COUNT=$((FILE_COUNT + 1))
    abs="$PROJECT_ROOT/$rel"
    # Compute file size for the nudge weight; missing/deleted files
    # contribute 0 bytes (which is the correct accounting — deletion is
    # also a work signal, but a small one).
    if [ -f "$abs" ]; then
        sz=$(stat -c '%s' "$abs" 2>/dev/null || stat -f '%z' "$abs" 2>/dev/null || echo "0")
        TOTAL_BYTES=$((TOTAL_BYTES + ${sz:-0}))
    fi
    ALL_FILES+=("$rel")
    # Classify: knowledge/**/*.md → KG-sync, code-extension → code-
    # graph queue. A file can in principle land in both (rare); the
    # two queues are independent. Use regex rather than case-glob so
    # the rule is consistent regardless of bash globstar setting.
    if printf '%s' "$rel" | grep -qE '^knowledge/.*\.md$'; then
        KG_FILES+=("$rel")
    fi
    if printf '%s' "$rel" | grep -qE "$_CODE_EXT_RE"; then
        CODE_FILES+=("$rel")
    fi
done <<EOF
$CHANGED_FILES
EOF

# -------------------- Step 2: KG sync --------------------
# For each modified knowledge/**/*.md file: queue a kg-sync run. We
# use the per-project script at `.claude/scripts/kg-sync` when
# available (the canonical entry point that auto-activates the right
# venv) and fall back to the orchestrator's bundled wrapper otherwise.
# Each invocation gets a 30s timeout; if kg-sync doesn't exist we no-op.
KG_SYNC_SCRIPT=""
if [ -x "$PROJECT_ROOT/.claude/scripts/kg-sync" ]; then
    KG_SYNC_SCRIPT="$PROJECT_ROOT/.claude/scripts/kg-sync"
fi

if [ -n "$KG_SYNC_SCRIPT" ] && [ "${#KG_FILES[@]}" -gt 0 ]; then
    for kf in "${KG_FILES[@]}"; do
        kf_abs="$PROJECT_ROOT/$kf"
        # The kg-sync script accepts a file path or `--all`. We pass
        # the absolute path; soft-fail on timeout / error. Run in the
        # background with a wait+kill timeout so we don't accumulate
        # 30s-per-file if many files changed. The launcher's batch
        # sync will catch anything we miss.
        # `timeout` may not be on PATH on minimal busybox — fall back
        # to a backgrounded process with manual kill.
        if command -v timeout >/dev/null 2>&1; then
            timeout 30 "$KG_SYNC_SCRIPT" "$kf_abs" >/dev/null 2>&1 || true
        else
            ( "$KG_SYNC_SCRIPT" "$kf_abs" >/dev/null 2>&1 ) &
            local_pid=$!
            ( sleep 30 && kill -9 "$local_pid" 2>/dev/null ) &
            wait "$local_pid" 2>/dev/null || true
        fi
    done
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
# analyzer synchronously (joern + sentence-transformer init is 5-30s, over the
# <10s budget); we only ENQUEUE. Soft-fail throughout.
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
    for f in "${ALL_FILES[@]}"; do
        abs="$PROJECT_ROOT/$f"
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
}))
' 2>/dev/null)
            if [ -n "$JSONL_ALERT" ]; then
                printf '%s\n' "$JSONL_ALERT" >> "$ALERT_LOG" 2>/dev/null || true
            fi
        fi
    done
fi

# -------------------- Step 5: Nudge counter increment --------------------
# Add subagent work to the parent session's row in the kg-update-nudge
# counter file. The counter file lives under ~/.claude/metrics/ (not
# .claude/) because nudge state is per-session not per-project; the
# kg-update-nudge.sh hook reads from the same path.
#
# Work-unit estimate proxy: file_count * 50 + total_bytes / 4. 50 per
# file approximates the "agent had to think about it" overhead;
# bytes/4 approximates output tokens (~4 chars/token English).
# This is a rough metric — the nudge hook's threshold (175k first,
# 50k interval) is order-of-magnitude-tolerant by design.
if [ -n "$SESSION_ID" ] && [ "$FILE_COUNT" -gt 0 ]; then
    NUDGE_FILE="$HOME/.claude/metrics/kg_update_tokens.jsonl"
    mkdir -p "$(dirname "$NUDGE_FILE")" 2>/dev/null || true
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
# carrying just the subagent contribution. The nudge hook gracefully
# merges this on its next read.
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
        sys.exit(0)
except OSError:
    sys.exit(0)
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
#   - the payload exposed an isolation flag (ISO_FLAG == 1); else no-op
#     (we don't guess non-isolation agents into violations),
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
    fi
fi

# Cleanup: delete the snapshot file (we are done with it). Soft-fail.
if command -v cleanup_snapshot >/dev/null 2>&1; then
    cleanup_snapshot "$AGENT_ID" "$PROJECT_ROOT" "$STATE_DIR" 2>/dev/null || true
fi

exit 0
