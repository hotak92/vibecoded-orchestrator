#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# stop-codegraph-drain.sh — Stop hook (v0.2.73 FIX-B)
#
# END-OF-TURN BATCHED code-graph sync. Replaces the per-EDIT
# code-graph-incremental.sh scheduling that post-file-edit.sh used to fire on
# every code-file Edit. That per-edit cadence hit the big CodeFunction
# collection's insert-time HNSW churn on every keystroke, and parallel
# subagents editing in worktrees multiplied it into the measured Weaviate disk
# write-amplification (857 GB/8h vs a 5 GB dataset). Instead, post-file-edit.sh
# now only APPENDS each edited path to a per-turn drain queue
# (.claude/state/codegraph_drain_<sid>.txt); this Stop hook drains it at
# end-of-turn and runs ONE analyzer pass over all the turn's files, grouped by
# canonical repo root, SUBJECT TO a 2-minute rate limit.
#
# Design (maintainer decision 2026-07-03):
#   * END-OF-TURN batch + 2-MINUTE RATE LIMIT. If the turn ends < 120s since
#     the last sync for this project, DON'T run — leave the queue so the next
#     eligible drain (past the 2-min window) processes the UNION.
#   * FIX-A' gate applied per path: an edit in an EPHEMERAL/unregistered
#     worktree is DROPPED here too (cross-fix consistency — else the drain
#     re-introduces the worktree churn FIX-A' removes at per-edit time).
#   * GROUP by canonical root: a turn's files can span worktree + main → two
#     canonical roots → two analyzer batches (each with its own
#     --canonical-source + resolved --project prefix).
#   * SERIALIZE per canonical root: a per-root lock-dir (mkdir is atomic on
#     POSIX) so a later drain for the same root can't be overtaken by an
#     earlier one. If the lock is held (a prior drain still running for this
#     root), skip THIS root's batch and leave its paths queued.
#   * edited-then-DELETED-in-turn → the analyzer's --only-files-from prunes a
#     vanished path (deletes its objects), never a self-inflicted orphan.
#
# Contract / constraints:
#   * Always exit 0 — a Stop hook must never block turn end.
#   * Soft-fail throughout: a missing queue, unkeyable session, or resolution
#     failure is a clean no-op (the paths stay queued for a later drain).
#   * The analyzer runs DETACHED in the background so the hook returns fast.
#   * MUST MATCH templates/hooks/stop-codegraph-drain.ps1 (cross-language).

unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/stderr-cap.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ] && . "$SCRIPT_DIR/_lib/stderr-cap.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
# shellcheck source=_lib/canonical-repo-root.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/canonical-repo-root.sh" ] && . "$SCRIPT_DIR/_lib/canonical-repo-root.sh"
# shellcheck source=_lib/worktree-gate.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/worktree-gate.sh" ] && . "$SCRIPT_DIR/_lib/worktree-gate.sh"

# Rate-limit window (seconds) between code-graph drains per project. Maintainer:
# END-OF-TURN batch + 2-MINUTE RATE LIMIT. Overridable for tests / tuning.
_CG_DRAIN_MIN_INTERVAL="${VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS:-120}"
case "$_CG_DRAIN_MIN_INTERVAL" in
    ''|*[!0-9]*) _CG_DRAIN_MIN_INTERVAL=120 ;;
esac

# Interpreter for hashing + running the analyzer. No python → soft no-op
# (nothing can run; the queue persists for a session that later has python).
[ -z "${PY:-}" ] && exit 0

HOOK_STDIN=$(cat 2>/dev/null || echo "")
SESSION_ID=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    print(json.loads(sys.stdin.read()).get('session_id', ''))
except Exception:
    print('')
" 2>/dev/null || echo "")

# Untrustworthy session id → no keyed queue to drain (post-file-edit skips
# accumulation for the same case).
[ -n "$SESSION_ID" ] || exit 0
case "$SESSION_ID" in
    *[!A-Za-z0-9_-]*) exit 0 ;;
esac

STATE_DIR="$PROJECT_ROOT/.claude/state"
QUEUE="$STATE_DIR/codegraph_drain_${SESSION_ID}.txt"
[ -f "$QUEUE" ] || exit 0

# ── RATE LIMIT ──────────────────────────────────────────────────────────────
# Per-project last-sync timestamp. If < interval since the last drain, DON'T
# run — leave the queue so the next eligible drain processes the union.
LAST_TS_FILE="$STATE_DIR/codegraph_drain_last_sync.ts"
NOW="$(date +%s 2>/dev/null || echo 0)"
LAST_TS=0
if [ -f "$LAST_TS_FILE" ]; then
    LAST_TS=$(cat "$LAST_TS_FILE" 2>/dev/null || echo 0)
    case "$LAST_TS" in ''|*[!0-9]*) LAST_TS=0 ;; esac
fi
if [ "$LAST_TS" -gt 0 ]; then
    _AGE=$(( NOW - LAST_TS ))
    if [ "$_AGE" -lt "$_CG_DRAIN_MIN_INTERVAL" ]; then
        # Rate-limited: leave the queue (union grows) for the next eligible drain.
        exit 0
    fi
fi

# ── DRAIN: read queue, gate each path, group by canonical root ──────────────
# Read the queue's full paths (dedup, preserve first-seen order). We consume
# the queue now (rename aside) so concurrent appends during the run start a
# fresh queue — but we ONLY unlink the consumed copy after we've dispatched the
# batches, so a crash mid-dispatch leaves the consumed file for the next drain
# to recover is NOT attempted here (soft-fail: worst case a turn's edits wait
# for the next edit+drain — acceptable, eventually-consistent index).
CONSUMED="$QUEUE.draining.$$"
mv "$QUEUE" "$CONSUMED" 2>/dev/null || exit 0

# Resolve analyzer + python (mirror code-graph-incremental.sh's resolution).
DEFAULT_REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ANALYZER="${VCT_ANALYZER_SCRIPT:-$DEFAULT_REPO_ROOT/.claude/scripts/analyze_code_graph.py}"
if [ ! -f "$ANALYZER" ]; then
    # No analyzer → put the queue back (append, so nothing is lost) + no-op.
    cat "$CONSUMED" >> "$QUEUE" 2>/dev/null || true
    rm -f "$CONSUMED" 2>/dev/null || true
    exit 0
fi
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
ANALYZER_PY="$PY"
if command -v resolve_vco_venv_python >/dev/null 2>&1; then
    resolve_vco_venv_python "$SCRIPT_DIR"
    [ -n "${VCO_VENV_PYTHON:-}" ] && ANALYZER_PY="$VCO_VENV_PYTHON"
fi

# Resolve the code-graph collection prefix for a canonical root (hub resolver
# `code_graph_collection_prefix`, else basename). Mirrors
# code-graph-incremental.sh :: _resolve_codegraph_project.
_drain_resolve_project() {
    local _root="$1" _name="" _resolver="$1/.claude/scripts/vct_project_config.sh"
    if [ -x "$_resolver" ]; then
        _name=$("$_resolver" "$_root" --field code_graph_collection_prefix 2>/dev/null) || _name=""
    fi
    [ -z "$_name" ] && _name="$(basename "$_root")"
    printf '%s' "$_name"
}

# md5 of a string → slash-free key (portable, via python).
_drain_key() {
    printf '%s' "$1" | "$PY" -c "import hashlib,sys; print(hashlib.md5(sys.stdin.buffer.read()).hexdigest())" 2>/dev/null || printf '%s' "_"
}

CODE_RE='\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$'

# Per-canonical-root batch files: <hash> → tmp file of paths for that root.
BATCH_DIR="$(mktemp -d "$STATE_DIR/cg_drain_batch.XXXXXX" 2>/dev/null || mktemp -d 2>/dev/null)"
if [ -z "$BATCH_DIR" ] || [ ! -d "$BATCH_DIR" ]; then
    # mktemp failed → put the queue back + no-op.
    cat "$CONSUMED" >> "$QUEUE" 2>/dev/null || true
    rm -f "$CONSUMED" 2>/dev/null || true
    exit 0
fi
# Map canonical-root-hash → canonical root path (for --canonical-source).
declare -A _ROOT_FOR_HASH 2>/dev/null || true

# Dedup + gate + group.
_SEEN_TMP="$BATCH_DIR/.seen"
: > "$_SEEN_TMP"
while IFS= read -r _p; do
    [ -n "$_p" ] || continue
    # Dedup within this drain.
    if grep -Fxq "$_p" "$_SEEN_TMP" 2>/dev/null; then continue; fi
    printf '%s\n' "$_p" >> "$_SEEN_TMP"

    # Only code files (mirror the incremental hook's extension gate).
    case "$_p" in
        *) ;;
    esac
    if ! printf '%s' "$_p" | grep -Eq "$CODE_RE"; then continue; fi
    # Never index transient scratch.
    case "$_p" in */.claude/state/*) continue ;; esac

    # Resolve canonical root. A vanished (deleted) path may not resolve via its
    # dirname if the dir is gone; fall back to PROJECT_ROOT's canonical root so
    # a deleted file still gets pruned under the right prefix.
    _canon="$(_canonical_repo_root "$_p")" || _canon=""
    if [ -z "$_canon" ]; then
        _canon="$(_canonical_repo_root "$PROJECT_ROOT/.")" || _canon="$PROJECT_ROOT"
    fi

    # FIX-A' gate: drop EPHEMERAL/unregistered worktree edits (cross-fix
    # consistency). REPO_PATH here is PROJECT_ROOT (the on-disk session root);
    # the gate compares it against the canonical root.
    if command -v _worktree_gate_should_skip >/dev/null 2>&1 \
        && _worktree_gate_should_skip "$_p" "$PROJECT_ROOT" "$_canon"; then
        continue
    fi

    _h="$(_drain_key "$_canon")"
    _ROOT_FOR_HASH["$_h"]="$_canon"
    printf '%s\n' "$_p" >> "$BATCH_DIR/$_h.paths"
done < "$CONSUMED"

# Nothing survived the gate → clean up + record the drain ts (so a turn of
# pure-worktree edits still advances the rate-limit clock) and exit.
_ANY=0
for _pf in "$BATCH_DIR"/*.paths; do
    [ -e "$_pf" ] || continue
    _ANY=1
    break
done
if [ "$_ANY" -eq 0 ]; then
    printf '%s' "$NOW" > "$LAST_TS_FILE" 2>/dev/null || true
    rm -rf "$BATCH_DIR" 2>/dev/null || true
    rm -f "$CONSUMED" 2>/dev/null || true
    exit 0
fi

# Resolve the `.claude` gate flag for a canonical root (mirror the incremental
# hook's _resolve_index_dot_claude fallback).
_drain_index_dot_claude() {
    local _root="$1" _val="" _resolver="$1/.claude/scripts/vct_project_config.sh"
    if [ -x "$_resolver" ]; then
        _val=$("$_resolver" "$_root" --field code_graph_index_dot_claude 2>/dev/null) || _val=""
    fi
    case "$_val" in
        true|True|TRUE|1)  printf -- '--index-dot-claude'; return 0 ;;
        false|False|FALSE|0) printf -- '--no-index-dot-claude'; return 0 ;;
    esac
    if [ -d "$_root/vco_lib" ] && [ -d "$_root/.claude" ]; then
        printf -- '--index-dot-claude'
    else
        printf -- '--no-index-dot-claude'
    fi
}

# Stale-lock breaker (v0.2.73 I/O-audit HIGH-1): the per-root lock below is a
# lock-DIR released only inside the detached analyzer's `_snip` (rm -rf "$_lock").
# If that detached process dies before the release — SIGKILL, OOM, ENOSPC
# (the exact disk-full condition this whole effort targets), or a reboot — the
# lock dir LEAKS and the `mkdir` acquire below fails forever, so that root's
# code graph would freeze silently and never drain again. Break locks older
# than the max plausible analyzer runtime so a dead drain self-heals on the next
# turn. 30 min is well past a normal per-turn batch; a lock older than that
# means the holder is gone, not slow. `find -mmin +30` on the lock DIR's mtime
# (set at mkdir); soft-fail if `find` is absent (rare).
if command -v find >/dev/null 2>&1; then
    find "$STATE_DIR" -maxdepth 1 -type d -name 'codegraph_drain_root_*.lock' \
        -mmin +30 -exec rm -rf {} + 2>/dev/null || true
fi

# Run one analyzer batch per canonical root, serialized per-root.
for _pf in "$BATCH_DIR"/*.paths; do
    [ -e "$_pf" ] || continue
    _h="$(basename "$_pf" .paths)"
    _canon="${_ROOT_FOR_HASH[$_h]:-$PROJECT_ROOT}"
    _project="$(_drain_resolve_project "$_canon")"
    _dot_flag="$(_drain_index_dot_claude "$_canon")"

    # Per-canonical-root serialization: atomic mkdir lock. If held, a prior
    # drain for THIS root is still running → skip this batch and put its paths
    # back on the queue for the next drain (never lose them).
    _lock="$STATE_DIR/codegraph_drain_root_${_h}.lock"
    if ! mkdir "$_lock" 2>/dev/null; then
        cat "$_pf" >> "$QUEUE" 2>/dev/null || true
        continue
    fi

    # Persist the batch's path list to a stable file the analyzer reads (the
    # BATCH_DIR is removed after dispatch, so copy it out).
    _list="$STATE_DIR/codegraph_drain_list_${_h}.txt"
    cp "$_pf" "$_list" 2>/dev/null || cat "$_pf" > "$_list" 2>/dev/null || true

    # Detached background run: hold the per-root lock for the WHOLE analyzer
    # run so a later drain for the same root serializes behind it, then release.
    _snip="\"$ANALYZER_PY\" \"$ANALYZER\" \"$_canon\" --project \"$_project\" --only-files-from \"$_list\" --canonical-source \"$_canon\" $_dot_flag >/dev/null 2>&1; rm -f \"$_list\" 2>/dev/null; rm -rf \"$_lock\" 2>/dev/null"
    if command -v setsid >/dev/null 2>&1; then
        setsid sh -c "$_snip" >/dev/null 2>&1 < /dev/null &
    elif command -v nohup >/dev/null 2>&1; then
        nohup sh -c "$_snip" >/dev/null 2>&1 < /dev/null &
        disown 2>/dev/null || true
    else
        ( sh -c "$_snip" ) >/dev/null 2>&1 < /dev/null &
    fi
done

# Record the drain timestamp (rate-limit clock) + clean up.
printf '%s' "$NOW" > "$LAST_TS_FILE" 2>/dev/null || true
rm -rf "$BATCH_DIR" 2>/dev/null || true
rm -f "$CONSUMED" 2>/dev/null || true

exit 0
