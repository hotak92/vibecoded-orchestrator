#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Parity: kept in step with stop-codegraph-drain.ps1 (its v0.2.92 PowerShell-only
# detached-spawn hardening is the Windows twin of the setsid / nohup spawn below;
# every other behaviour must match the .ps1 sibling).
# stop-codegraph-drain.sh — Stop + SessionStart hook: the code graph follows
# the COMMITTED state of the main checkout (v0.2.107 CG-3).
#
# The code graph of a project reflects the files at HEAD of its MAIN checkout's
# current branch, and of each enabled extra code folder at its own HEAD. It is
# updated when HEAD moves and at no other time (owner rulings 2026-11-17 and
# 2026-11-19): an Edit/Write queues nothing, an edit in a linked git worktree
# (a lane) is never indexed — not under the project, not into a collection of
# its own — and reaches the graph when it is merged into the main checkout's
# branch and that HEAD moves. Uncommitted edits in the main checkout are not
# indexed either; the commit that carries them is. A slightly outdated graph
# is accepted, a graph bouncing between worktrees is not.
#
# What this hook does, each at most once per its interval per project, each
# DETACHED (a Stop/SessionStart hook never waits on an analyzer):
#   1. extras check  → `python -m vco_lib.codegraph_extras_refresh` (an enabled
#      extra code folder whose HEAD moved is re-indexed by commit range);
#   2. HEAD catch-up → `python -m vco_lib.codegraph_head_catchup` (the main
#      checkout's HEAD vs the binding's recorded commit: ONE analyzer run per
#      HEAD move; it skips a linked worktree; it also runs the daily
#      path-existence reconciliation that removes rows of code deleted outside
#      any diffed range).
# Registered on Stop AND on SessionStart, so a pull or merge made outside a
# session is picked up when the next session starts. The name is historical:
# the per-edit drain queue this hook used to consume was retired in v0.2.107.
# A project that is not a git repository is updated by a manual re-analysis
# (launcher Codegraph page) only.
#
# Contract / constraints:
#   * Always exit 0 — a Stop hook must never block turn end.
#   * Soft-fail throughout.
#   * MUST MATCH templates/hooks/stop-codegraph-drain.ps1 (cross-language).

# Canonical list: _lib/scrub-env.sh; the inline unset is the fallback for a missing lib.
{ . "${BASH_SOURCE[0]%/*}/_lib/scrub-env.sh" && vct_scrub_secret_env; } 2>/dev/null ||
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# shellcheck source=_lib/stderr-cap.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ] && . "$SCRIPT_DIR/_lib/stderr-cap.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

# Interpreter for the analyzer. No python → soft no-op.
[ -z "${PY:-}" ] && exit 0

# The payload carries nothing this hook needs; read it so the harness's pipe
# is consumed.
cat >/dev/null 2>&1 || true

STATE_DIR="$PROJECT_ROOT/.claude/state"
# ── RUNTIME: analyzer script + interpreter (one home for this hook) ─────────
# Used by BOTH the extra-path refresh and the HEAD catch-up below. Sets ANALYZER,
# ANALYZER_PY and _CG_RUNTIME_RC (cached — resolved at most once per run):
#   0 usable · 1 no analyzer script · 2 bare-PATH interpreter cannot import
#   the analyzer's dependencies.
_CG_RUNTIME_RC=""
_drain_resolve_runtime() {
    [ -n "$_CG_RUNTIME_RC" ] && return "$_CG_RUNTIME_RC"
    local _default_repo_root
    _default_repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
    ANALYZER="${VCT_ANALYZER_SCRIPT:-$_default_repo_root/.claude/scripts/analyze_code_graph.py}"
    if [ ! -f "$ANALYZER" ]; then
        _CG_RUNTIME_RC=1
        return 1
    fi
    # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
    [ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
    # Interpreter for the analyzer. Precedence — MUST MATCH
    # stop-codegraph-drain.ps1 (`$env:VCT_PYTHON` → resolver → PATH loop):
    #   1. $VCT_PYTHON      explicit operator override (a user statement)
    #   2. shared venv resolver (VCT_INSTALL_ROOT/.venv, clone-relative, …)
    #   3. $PY              bare PATH — python3 / python / py
    # v0.2.96: this file read NEITHER $VCT_PYTHON (its .ps1 sibling and
    # the since-retired code-graph-incremental.sh both did — a silent cross-OS divergence) nor any
    # record of WHICH tier answered, which is what the probe below needs.
    ANALYZER_PY="${VCT_PYTHON:-}"
    local _from_bare_path=0
    if [ -z "$ANALYZER_PY" ] && command -v resolve_vco_venv_python >/dev/null 2>&1; then
        resolve_vco_venv_python "$SCRIPT_DIR"
        ANALYZER_PY="${VCO_VENV_PYTHON:-}"
    fi
    if [ -z "$ANALYZER_PY" ]; then
        ANALYZER_PY="$PY"
        _from_bare_path=1
    fi
    # v0.2.96 (WP-5 S2): when every venv tier missed, ANALYZER_PY is whatever
    # `python3`/`python`/`py` happens to be on PATH — an interpreter that in a
    # user project routinely has neither `weaviate` nor `vco_lib`. The
    # analyzer would then die inside a detached run. Probe the SAME import string the shipped
    # wrappers gate on (`kg-duplicates`, `vct_venv_ladder.sh`), and only when
    # we actually fell back — a healthy install resolves a venv and pays
    # nothing.
    if [ "$_from_bare_path" = "1" ] \
        && ! "$ANALYZER_PY" -c 'import weaviate, vco_lib' >/dev/null 2>&1; then
        _CG_RUNTIME_RC=2
        return 2
    fi
    _CG_RUNTIME_RC=0
    return 0
}

# Run a `sh -c` snippet fully detached (new session when possible) — the ONE
# spawn shape of this hook, so a Stop hook never waits on an analyzer.
#
# v0.2.102 NB-H5: the command string is $1; any further arguments are passed
# to `sh -c` as POSITIONAL PARAMETERS ($0, $1, … inside the snippet). Data
# (paths, interpreters) must travel as argv, NEVER interpolated into the
# snippet: a path containing `$`, backticks or `"` inside a double-quoted
# `sh -c` string is re-parsed by the shell — command substitution / word
# splitting / injection. With argv the shell never re-parses the data.
_drain_spawn_detached() {
    local _cmd="$1"
    shift || true
    if command -v setsid >/dev/null 2>&1; then
        setsid sh -c "$_cmd" "$@" >/dev/null 2>&1 < /dev/null &
    elif command -v nohup >/dev/null 2>&1; then
        nohup sh -c "$_cmd" "$@" >/dev/null 2>&1 < /dev/null &
        disown 2>/dev/null || true
    else
        ( sh -c "$_cmd" "$@" ) >/dev/null 2>&1 < /dev/null &
    fi
}

# ── EXTRA CODE-GRAPH PATHS: automatic re-index (v0.2.100 W5R-04) ────────────
# An extra path (launcher → project → Codegraph → "Extra codegraph paths")
# feeds another checkout into THIS project's code-graph collections. It is
# re-indexed when ITS HEAD moves (edits there queue nothing either). At most once per
# VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS (default 600) per project, it
# spawns `python -m vco_lib.codegraph_extras_refresh` DETACHED: that module
# compares each enabled extra path's HEAD with its last indexed commit, runs
# the SAME single-path analyzer argv the panel's Sync button uses (one home:
# `extra_path_sync_args` / `build_extra_sync_argv`), throttled per path
# (VCO_CODEGRAPH_EXTRAS_MIN_INTERVAL_SECONDS, default 3600) and bounded
# (30 min), then records the new commit through the hub route — never by
# opening launcher.db. Every decision is logged to
# .claude/logs/codegraph_extras_refresh.log. MUST MATCH the .ps1 sibling.
_CG_EXTRAS_INTERVAL="${VCO_CODEGRAPH_EXTRAS_CHECK_INTERVAL_SECONDS:-600}"
case "$_CG_EXTRAS_INTERVAL" in
    ''|*[!0-9]*) _CG_EXTRAS_INTERVAL=600 ;;
esac
if [ -d "$STATE_DIR" ]; then
    _CG_EXTRAS_TS_FILE="$STATE_DIR/codegraph_extras_check.ts"
    _CG_EXTRAS_NOW="$(date +%s 2>/dev/null || echo 0)"
    _CG_EXTRAS_LAST=0
    if [ -f "$_CG_EXTRAS_TS_FILE" ]; then
        _CG_EXTRAS_LAST=$(cat "$_CG_EXTRAS_TS_FILE" 2>/dev/null || echo 0)
        case "$_CG_EXTRAS_LAST" in ''|*[!0-9]*) _CG_EXTRAS_LAST=0 ;; esac
    fi
    # A stamp in the FUTURE is not a recent check — a pre-v0.2.103 local-time
    # stamp the .ps1 sibling wrote on Windows PowerShell 5.1 into this same
    # state dir, or a clock moved back: read it as "no stamp" (R20 L1).
    # MUST MATCH the .ps1 sibling's clamp.
    [ "$_CG_EXTRAS_LAST" -gt "$_CG_EXTRAS_NOW" ] && _CG_EXTRAS_LAST=0
    if [ "$_CG_EXTRAS_LAST" -eq 0 ] \
        || [ $(( _CG_EXTRAS_NOW - _CG_EXTRAS_LAST )) -ge "$_CG_EXTRAS_INTERVAL" ]; then
        printf '%s' "$_CG_EXTRAS_NOW" > "$_CG_EXTRAS_TS_FILE" 2>/dev/null || true
        if _drain_resolve_runtime; then
            _CG_EXTRAS_LOG_DIR="$PROJECT_ROOT/.claude/logs"
            mkdir -p "$_CG_EXTRAS_LOG_DIR" 2>/dev/null || true
            _CG_EXTRAS_LOG="$_CG_EXTRAS_LOG_DIR/codegraph_extras_refresh.log"
            # The child's own stderr (an import failure, a crash) lands in the
            # same log the module writes, so a broken refresh is never silent.
            # NB-H5 (v0.2.102): every path travels as a POSITIONAL PARAMETER —
            # interpolating "$PROJECT_ROOT" into the sh -c string re-parsed
            # `$`, backticks and `"` in the path (command injection).
            _drain_spawn_detached '"$0" -m vco_lib.codegraph_extras_refresh --project-root "$1" --analyzer "$2" --state-dir "$3" >>"$4" 2>&1' \
                "$ANALYZER_PY" "$PROJECT_ROOT" "$ANALYZER" "$STATE_DIR" "$_CG_EXTRAS_LOG"
        fi
    fi
fi

# ── HEAD-DIFF CATCH-UP: the ONE writer of the primary root's code graph ────
# v0.2.105 (#512A); since v0.2.107 (CG-3) the only path by which the main
# checkout's code reaches the graph. Commits, merges of lane branches, pulls
# and GUI merges all move HEAD. At most once per
# VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS (default 600) per project, this
# spawns `python -m vco_lib.codegraph_head_catchup` DETACHED: it compares the
# binding's `last_analyzed_commit` (hub /config) with the main checkout's HEAD
# and, when the recorded commit is an ANCESTOR, indexes exactly
# `git diff --name-only <recorded>..HEAD` (existing code files only); an
# enormous diff, or a deleted code file, goes to the analyzer's incremental
# walk instead. A non-ancestor / missing recorded commit takes ONE full
# (non-incremental) walk, then records HEAD — the converging fallback. A
# project with no ENABLED code-graph binding is skipped entirely, and a
# session whose checkout is a linked worktree is skipped (the main checkout's
# own session owns the catch-up). On success it records HEAD through the hub
# route `/projects/{id}/codegraph/analyzed` — never by opening launcher.db.
# Coalescing is inherent: ten commits between two runs are one range.
# Deliberately installs NO git hook into the user's repo.
# Log: .claude/logs/codegraph_head_catchup.log. MUST MATCH the .ps1 sibling.
_CG_CATCHUP_INTERVAL="${VCO_CODEGRAPH_HEAD_CATCHUP_INTERVAL_SECONDS:-600}"
case "$_CG_CATCHUP_INTERVAL" in
    ''|*[!0-9]*) _CG_CATCHUP_INTERVAL=600 ;;
esac
if [ -d "$STATE_DIR" ]; then
    _CG_CATCHUP_TS_FILE="$STATE_DIR/codegraph_head_catchup.ts"
    _CG_CATCHUP_NOW="$(date +%s 2>/dev/null || echo 0)"
    _CG_CATCHUP_LAST=0
    if [ -f "$_CG_CATCHUP_TS_FILE" ]; then
        _CG_CATCHUP_LAST=$(cat "$_CG_CATCHUP_TS_FILE" 2>/dev/null || echo 0)
        case "$_CG_CATCHUP_LAST" in ''|*[!0-9]*) _CG_CATCHUP_LAST=0 ;; esac
    fi
    # A stamp in the FUTURE is not a recent check (R20 L1; MUST MATCH the
    # .ps1 sibling's clamp and the two blocks above).
    [ "$_CG_CATCHUP_LAST" -gt "$_CG_CATCHUP_NOW" ] && _CG_CATCHUP_LAST=0
    if [ "$_CG_CATCHUP_LAST" -eq 0 ] \
        || [ $(( _CG_CATCHUP_NOW - _CG_CATCHUP_LAST )) -ge "$_CG_CATCHUP_INTERVAL" ]; then
        printf '%s' "$_CG_CATCHUP_NOW" > "$_CG_CATCHUP_TS_FILE" 2>/dev/null || true
        if _drain_resolve_runtime; then
            _CG_CATCHUP_LOG_DIR="$PROJECT_ROOT/.claude/logs"
            mkdir -p "$_CG_CATCHUP_LOG_DIR" 2>/dev/null || true
            _CG_CATCHUP_LOG="$_CG_CATCHUP_LOG_DIR/codegraph_head_catchup.log"
            # Every path travels as a POSITIONAL PARAMETER (NB-H5, v0.2.102).
            _drain_spawn_detached '"$0" -m vco_lib.codegraph_head_catchup --project-root "$1" --analyzer "$2" --state-dir "$3" >>"$4" 2>&1' \
                "$ANALYZER_PY" "$PROJECT_ROOT" "$ANALYZER" "$STATE_DIR" "$_CG_CATCHUP_LOG"
        fi
    fi
fi

exit 0
