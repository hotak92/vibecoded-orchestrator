#!/usr/bin/env bash
# post-bash-file-sync.sh — PostToolUse(Bash) hook (v0.2.95, lane F10)
#
# A file written from the CLI must sync exactly like a file written with
# the Edit/Write tools. Before this hook it did not: post-file-edit.sh is
# registered on matcher `Edit|Write` ONLY, so
#
#     cat > knowledge/foo.md <<EOF … EOF
#     sed -i 's/x/y/' docs/architecture.md
#     cp scratch/module.py vco_lib/module.py
#
# reached Weaviate NEVER. (The git-commit hook needs a commit, and in the
# orchestrator's own tree `knowledge/` is gitignored, so that path cannot
# fire there either.) This hook closes the gap.
#
# ONE HOME, NOT A SECOND COPY
# ---------------------------
# The routing itself — knowledge/ → kg-sync, docs/ → the development
# collection, .claude/diagrams/ → the indexer, code files → the
# end-of-turn code-graph drain queue, each access-gated and debounced —
# lives in _lib/route-touched-path.sh and is called by BOTH this hook and
# post-file-edit.sh. Nothing about the routing is re-implemented here.
#
# WHAT THIS HOOK ADDS: turning a command STRING into the list of paths it
# wrote. That parse is vco_lib/bash_write_targets.py (the write-side
# sibling of vco_lib/diagram_delete_parser, sharing
# vco_lib/bash_command_walk for chains / wrappers / env prefixes /
# `bash -c`). It recognises redirections, heredocs, tee, `sed -i`,
# cp/mv/install destinations, touch, `dd of=`, and long `--output` flags.
#
# KNOWN MISSES — read before assuming a write syncs
# -------------------------------------------------
# 1. A write performed by an interpreter from its own source text —
#    `python - <<EOF … open(p,'w') … EOF`, `python -c "…"`, `perl -e`,
#    `patch -p1 < x.diff`, `git checkout -- f` — cannot be recovered from
#    the command string without executing it. For knowledge/ and docs/
#    those are caught anyway by a BOUNDED, WATERMARKED mtime scan of those
#    two directories (files modified since the last scan; lookback capped
#    at 300 s, at most 32 results, watermark advanced after each scan).
#    The scan runs when ALL THREE hold — this is the exact trigger, stated
#    in full because a narrower description of it was the v0.2.95 review's
#    MAJOR-3:
#      (a) the parser recovered no target;
#      (b) the command TEXT shows a write at all — a redirect, a heredoc,
#          one of the write verbs, an interpreter, or one of the opaque
#          writers listed above (`command_has_write_shape`);
#      (c) that write could have landed in the scanned directories: the
#          command names one of them, or its shape hides its target.
#    A command that merely NAMES `knowledge/` or `docs/` — `cat docs/x.md`,
#    `grep -rn foo knowledge/`, `ls docs/` — fails (b): it reaches neither
#    Python nor the scan. Before v0.2.95 it did both, and re-routed every
#    knowledge/docs file touched in the previous 300 s, files the Edit tool
#    had already synced included.
# 2. CODE files written that way are a REAL MISS and are NOT scanned: a
#    whole-repo walk on every Bash call is an unbounded cost, and the next
#    Edit/Write of that file re-queues it for the code-graph drain anyway.
# 3. A relative path written after a `cd` the parser cannot resolve
#    (`cd "$D"`, `cd -`, a subshell `cd`) is DROPPED rather than guessed —
#    doing nothing beats routing the wrong file into the KG.
#
# COST CONTROL
# ------------
# The pure-shell prefilter (`vco_bash_write_prefilter`, in the shared
# _lib/bash-write-targets.sh) rejects the routine commands (ls, git status,
# pytest, grep, a `2>&1` / `>/dev/null` redirect) with no subprocess at
# all. Python is spawned only for a command that plausibly wrote
# something — which since v0.2.95 excludes a bare mention of knowledge/ or
# docs/ without a write-shaped token beside it. The number that matters
# here is how OFTEN Python starts, not how fast the directory walk is: the
# walk is sub-10 ms on a 1 000-file tree, the interpreter start that
# precedes it is tens of ms, and the prefilter is what keeps both off the
# common path. `install(1)` is deliberately NOT a prefilter trigger —
# `npm install` / `pip install` are far more common than install(1) as a
# file copier, and an install(1) that targets knowledge/ or docs/ (or uses
# a redirect) still reaches the parser through the other triggers.
#
# Always exits 0 (a PostToolUse hook can never block) and never prints to
# plain stdout (PostToolUse stdout is discarded per the v2.1.x contract —
# LLM-visible text goes through emit_additional_context).

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=_lib/stderr-cap.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ] && . "$SCRIPT_DIR/_lib/stderr-cap.sh"
# shellcheck source=_lib/emit-context.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/emit-context.sh" ] && . "$SCRIPT_DIR/_lib/emit-context.sh"
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python available — silent no-op

# D-16 (v0.2.73): prefer CLAUDE_PROJECT_DIR so worktree-isolated sessions
# resolve state/log paths against the SAME root as the sibling hooks.
PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# The prefilter + the parser invocation live in _lib/bash-write-targets.sh,
# shared with pre-bash-context-inject.sh (which needs the SAME parse to
# build its retrieval query from the target path). Conditional source, so a
# partial install cannot make the user's Bash call ERROR — but its ABSENCE is
# a broken install and is reported below, after the payload is decoded, not
# skipped in silence (v0.2.95 ship-gate review MAJOR-2: this exact `exit 0`
# carried a comment restating the finding that forbids it).
# shellcheck source=_lib/bash-write-targets.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/bash-write-targets.sh" ] && . "$SCRIPT_DIR/_lib/bash-write-targets.sh"

# ---------------------------------------------------------------------------
# Hook input
# ---------------------------------------------------------------------------
HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

# ONE decoder for tool_name + command + session_id (a PostToolUse(Bash)
# turn fires several sibling hooks; each extra `$PY -c` is a full
# interpreter start). NUL-delimited so a newline-bearing command survives.
COMMAND=""
SESSION_ID=""
TOOL_NAME=""
_PBFS_IDX=0
while IFS= read -r -d '' _PBFS_VAL; do
    case "$_PBFS_IDX" in
        0) TOOL_NAME="$_PBFS_VAL" ;;
        1) COMMAND="$_PBFS_VAL" ;;
        2) SESSION_ID="$_PBFS_VAL" ;;
    esac
    _PBFS_IDX=$((_PBFS_IDX + 1))
done < <(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get('tool_input', {}) or {}
    fields = [
        d.get('tool_name', '') or '',
        ti.get('command', '') or '',
        d.get('session_id', '') or '',
    ]
except Exception:
    fields = ['', '', '']
sys.stdout.write(''.join(str(f) + '\0' for f in fields))
" 2>/dev/null)

[ "$TOOL_NAME" = "Bash" ] || exit 0
[ -z "$COMMAND" ] && exit 0

# The parser lib is missing → this hook can recover NO write target from any
# command, so every CLI write stops syncing while everything else looks
# healthy. Report once per session (stderr + one envelope) and exit 0.
#
# Deliberately placed AFTER the decode rather than at the source: the notice
# is keyed on the session id, which only the payload carries, and a sentinel
# keyed "default" would report once per PROJECT, ever. The cost is one
# interpreter start per Bash call on a BROKEN install — the install being
# broken is the point, and the healthy path is unchanged.
if ! command -v vco_bash_write_prefilter >/dev/null 2>&1; then
    if command -v vco_report_missing_hook_lib >/dev/null 2>&1; then
        vco_report_missing_hook_lib "$SCRIPT_DIR" "$PROJECT_ROOT" \
            "$SESSION_ID" "bash-write-targets.sh"
        if [ -n "${VCO_MISSING_LIB_NOTICE:-}" ] \
            && command -v emit_additional_context >/dev/null 2>&1; then
            emit_additional_context "$VCO_MISSING_LIB_NOTICE" PostToolUse
        fi
    fi
    exit 0
fi

vco_bash_write_prefilter "$COMMAND" || exit 0

# Telemetry attribution for the children we spawn (same 3-layer chain as
# post-file-edit.sh: an empty VCT_SESSION_ID makes every CLI-emitted event
# from a hook-triggered sync unattributable).
[ -n "$SESSION_ID" ] && export VCT_SESSION_ID="$SESSION_ID"

# ---------------------------------------------------------------------------
# Command → written paths
# ---------------------------------------------------------------------------
vco_bash_write_init "$SCRIPT_DIR" "$PY"
SCAN_STATE="$PROJECT_ROOT/.claude/state/bash_write_scan.ts"
PATHS=$(vco_bash_write_targets "$COMMAND" "$PROJECT_ROOT" "$SCAN_STATE")

[ -z "$PATHS" ] && exit 0

# ---------------------------------------------------------------------------
# Route each path through the SHARED home
# ---------------------------------------------------------------------------
# shellcheck source=_lib/route-touched-path.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/route-touched-path.sh" ] && . "$SCRIPT_DIR/_lib/route-touched-path.sh"
if ! command -v vco_route_touched_path >/dev/null 2>&1; then
    # The routing home is absent — a BROKEN INSTALL, not a fallback case
    # (review MAJOR-1). We have already parsed real write targets, so every
    # one of them is about to be dropped; report once per session on stderr
    # + one additionalContext envelope, then exit 0 as the contract requires.
    if command -v vco_report_missing_hook_lib >/dev/null 2>&1; then
        vco_report_missing_hook_lib "$SCRIPT_DIR" "$PROJECT_ROOT" \
            "$SESSION_ID" "route-touched-path.sh"
        if [ -n "${VCO_MISSING_LIB_NOTICE:-}" ] \
            && command -v emit_additional_context >/dev/null 2>&1; then
            emit_additional_context "$VCO_MISSING_LIB_NOTICE" PostToolUse
        fi
    fi
    exit 0
fi

vco_route_init "$SCRIPT_DIR" "$PROJECT_ROOT" "$PY"

# JSON-escape a path for the kg-summary re-dispatch payload below.
_pbfs_json_esc() {
    printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

while IFS= read -r _pbfs_path; do
    [ -z "$_pbfs_path" ] && continue
    vco_route_touched_path "$_pbfs_path" "$SESSION_ID"

    # kg-summary parity. The shipped kg-summary-generator.sh is registered
    # on Edit|Write(knowledge/**/*.md) only, so a CLI-written node had no
    # LLM summary in knowledge/.node_formats.json and therefore rendered
    # empty at hybrid_search's `summary` detail tier. Re-dispatch the
    # EXISTING hook with a synthesized payload rather than duplicating its
    # logic; it self-validates the path, self-debounces (60 s per file) and
    # backgrounds its own generator.
    case "$_pbfs_path" in
        "$PROJECT_ROOT"/knowledge/*.md)
            if [ -f "$SCRIPT_DIR/kg-summary-generator.sh" ]; then
                printf '{"tool_name":"Write","tool_input":{"file_path":"%s"}}' \
                    "$(_pbfs_json_esc "$_pbfs_path")" \
                    | bash "$SCRIPT_DIR/kg-summary-generator.sh" >/dev/null 2>&1 &
            fi
            ;;
    esac
done <<< "$PATHS"

# Emit whatever the routing left for the model (currently the pending
# duplicate-scan report) as ONE PostToolUse envelope.
if [ -n "${VCO_ROUTE_NUDGE:-}" ] && command -v emit_additional_context >/dev/null 2>&1; then
    emit_additional_context "$VCO_ROUTE_NUDGE" PostToolUse
fi

exit 0
