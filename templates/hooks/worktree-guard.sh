#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# worktree-guard.sh — WorktreeCreate hook (Layer 0, primary deterministic
# gate) for the worktree-isolation safeguard.
#
# ── Why this exists ──────────────────────────────────────────────────────
# The 2026-06-30 incident: three subagents dispatched with
# `isolation: worktree` all silently wrote to the SHARED parent working
# tree. Their reported cwd was `.claude/worktrees/agent-<id>` yet their
# commits/edits landed on the parent branch. The SILENCE was the bug —
# there was no deterministic, hook-level worktree at the create instant.
#
# ── What the WorktreeCreate contract ACTUALLY is (verified 2026-07-06) ────
# Per the official Claude Code Hooks Reference
# (https://code.claude.com/docs/en/hooks.md, retrieved 2026-07-06):
#   • "When a worktree is being created via `--worktree` or
#      `isolation: "worktree"`. Replaces default git behavior."
#     → THE HOOK IS RESPONSIBLE FOR CREATING THE WORKTREE, not merely
#       validating a path. It replaces git's default create step.
#   • stdin payload carries the common fields {session_id, transcript_path,
#     cwd, hook_event_name} plus a worktree IDENTIFIER. The docs name it
#     `worktree_name`; the live harness on the pinned build sends it as
#     `name` (= the agent id, e.g. "agent-a10c46d251a62b21d"). THERE IS NO
#     PROPOSED-PATH FIELD in the real payload — the hook must DECIDE the
#     path. We tolerate BOTH keys (`worktree_name` OR `name`).
#   • stdout: print the absolute worktree path (plain line). Exit 0 =
#     success (the worktree MUST exist at that path). ANY non-zero exit
#     ABORTS the create (strict — not just exit 2).
#
# ── The bug this version fixes ───────────────────────────────────────────
# The previous implementation was a VALIDATOR: it parsed stdin for a
# proposed path and, finding none (the real payload has none), no-op'd,
# echoed empty, exited 0. The harness then had no path → "hook succeeded
# but returned no worktree path" → the worktree was NEVER created → the
# subagent silently fell back to the shared parent tree. NO code path ran
# `git worktree add`. This version CREATES the worktree.
#
# ── Decision matrix ──────────────────────────────────────────────────────
#   1. Global bypass (VCT_DISABLE_HOOKS) → echo nothing and no-op (harness
#      uses its own default). Full no-op, consistent with every hook.
#   2. cwd is NOT a git repo → this create cannot be isolated; return the
#      working directory itself (the agent runs in place) and leave a
#      notice that tells the agent, and through its report the user, that
#      isolation is OFF (v0.2.102 W6-Q — echoing nothing made the harness
#      fail the launch). No identifier → plain no-op.
#   3. Derive `<toplevel>/.claude/worktrees/<sanitized-id>` and run
#      `git worktree add --detach <path> HEAD` — or, when the repository has
#      no commits yet (unborn HEAD), detach it at a snapshot commit of the
#      working tree built through a throwaway index, with a notice saying so
#      (v0.2.102 W6-Q).
#        • already a registered worktree (re-fire / retry) → idempotent
#          success: echo it, exit 0.
#        • `git worktree add` FAILS → LOUD abort: log the reason, print it
#          to stderr, exit NON-ZERO. A loud abort is correct here: the
#          whole point of this hook is to prevent the SILENT shared-tree
#          fallback, so a failed create must surface (harness aborts +
#          shows the reason) rather than fall through to the parent tree.
#        • success → echo the absolute worktree path, exit 0.
#   4. Belt-and-suspenders: if a future harness build DOES send an explicit
#      path field, honour it — validate it is a SEPARATE checkout (never
#      the parent toplevel) and create it if absent, same create semantics.
#
# ── VCT_WORKTREE_GUARD_ENFORCE (staged-enable flag — now vestigial) ───────
# In the previous log-only design this flag flipped the one violation branch
# from warn-only to block. Creation is now the DEFAULT behaviour (ungated) —
# a failed create always aborts loudly regardless of the flag, because the
# docs contract makes non-zero == abort unconditional. The flag is retained
# only for the belt-and-suspenders explicit-path branch: when a path IS
# supplied AND it equals the parent toplevel, ENFORCE hard-blocks (exit
# non-zero) while the default logs the anomaly and still derives a safe
# separate path. For the real (no-path) payload the flag has no effect —
# creation is unconditional. Documented here so the next editor doesn't
# assume creation is gated behind it.
#
# ── Cross-OS parity ──────────────────────────────────────────────────────
# MUST behave identically to worktree-guard.ps1: same decision matrix, same
# path convention (`<toplevel>/.claude/worktrees/<sanitized-id>`), same
# `git worktree add --detach`, same idempotent-re-fire handling, same
# stdout/exit semantics. Any change to the decision logic here MUST be
# mirrored in worktree-guard.ps1 and vice versa. Keep them in lockstep.
#
# ── Tunables ─────────────────────────────────────────────────────────────
#   VCT_DISABLE_HOOKS            — global bypass (consistent with every hook).
#   VCT_WORKTREE_GUARD_ENFORCE=1 — hard-block the explicit-path==parent case
#                                  (belt-and-suspenders branch only; no
#                                  effect on the real no-path payload).

# Scrub sensitive env vars before any subprocess spawning.
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null

# Global bypass — echo nothing (the harness uses its own default) and exit 0.
# This matches every other hook's VCT_DISABLE_HOOKS behaviour (full no-op).
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# NOTE: deliberately NOT `set -e`. We want explicit control over every exit;
# soft-fail on best-effort steps and only ever abort (non-zero) on a genuine
# create failure or the explicit-path==parent enforce case.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Stderr cap so a buggy iteration cannot reproduce the 2026-05-07 GUI freeze.
if [ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ]; then
    # shellcheck source=_lib/stderr-cap.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/stderr-cap.sh"
fi

# Resolve a Python interpreter portably (used only for robust JSON parsing).
if [ -f "$SCRIPT_DIR/_lib/find-python.sh" ]; then
    # shellcheck source=_lib/find-python.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/find-python.sh"
fi
if [ -z "${PY:-}" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
fi

# Project root: the ONE shell rule (_lib/project-root.sh, must match
# vco_lib/project_root.py). Empty when no VCO project encloses the cwd: the
# worktree decision still runs, but the log row, notices and launch records
# (all state writes) are skipped rather than planted under the cwd.
# shellcheck source=_lib/project-root.sh disable=SC1091
. "$SCRIPT_DIR/_lib/project-root.sh"
PROJECT_ROOT="$(vco_project_root)" || PROJECT_ROOT=""
LOG_DIR=""
LOG_FILE=""
if [ -n "$PROJECT_ROOT" ]; then
    LOG_DIR="$PROJECT_ROOT/.claude/logs"
    LOG_FILE="$LOG_DIR/worktree-guard.jsonl"
    mkdir -p "$LOG_DIR" 2>/dev/null || true
else
    echo "worktree-guard: no VCO project encloses $PWD; log, notices and launch records skipped" >&2
fi

# ── Read stdin (the WorktreeCreate payload) ───────────────────────────────
HOOK_STDIN=$(cat 2>/dev/null || echo "")

# emit_path: satisfy the stdout contract. echo the absolute path the harness
# should use. Empty arg → echo nothing (harness uses its default).
emit_path() {
    [ -n "${1:-}" ] && printf '%s\n' "$1"
}

# log_event DECISION REASON PROPOSED RESOLVED — append a JSONL row. ALWAYS
# capture the FULL received payload (raw_payload) so the integrator can
# verify the live schema field names + stdout semantics across real spawns.
log_event() {
    [ -n "${PY:-}" ] || return 0
    DECISION_FOR_PY="${1:-}" \
    REASON_FOR_PY="${2:-}" \
    PROPOSED_FOR_PY="${3:-}" \
    RESOLVED_FOR_PY="${4:-}" \
    ENFORCE_FOR_PY="${VCT_WORKTREE_GUARD_ENFORCE:-}" \
    STRICT_FOR_PY="${VCT_WORKTREE_GUARD_STRICT:-}" \
    RAW_STDIN_FOR_PY="$HOOK_STDIN" \
    LOG_FILE_FOR_PY="$LOG_FILE" \
    "$PY" -c '
import json, os, sys
from datetime import datetime, timezone
out = os.environ.get("LOG_FILE_FOR_PY", "")
if not out:
    sys.exit(0)
row = {
    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "hook": "worktree-guard",
    "decision": os.environ.get("DECISION_FOR_PY", ""),
    "reason": os.environ.get("REASON_FOR_PY", ""),
    "proposed_path": os.environ.get("PROPOSED_FOR_PY", ""),
    "resolved_path": os.environ.get("RESOLVED_FOR_PY", ""),
    "enforce": bool(os.environ.get("ENFORCE_FOR_PY", "")),
    "strict": bool(os.environ.get("STRICT_FOR_PY", "")),
    # Full raw payload so the integrator can verify the live schema.
    "raw_payload": os.environ.get("RAW_STDIN_FOR_PY", ""),
}
try:
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
except OSError:
    pass
' 2>/dev/null || true
}

# Parse stdin defensively. We extract:
#   line 1 = worktree IDENTIFIER (worktree_name / name synonyms) — decides the
#            create path when no explicit path is supplied.
#   line 2 = explicit proposed path, IF a future harness build sends one
#            (worktree_path / path / proposed_path / ... synonyms). Empty in
#            the real payload.
#   line 3 = repo root hint (cwd / repo_root / ... synonyms) — we always
#            re-derive the toplevel via git as well.
# Any JSON error → all three empty.
WT_NAME=""
PROPOSED_PATH=""
REPO_HINT=""
if [ -n "$HOOK_STDIN" ] && [ -n "${PY:-}" ]; then
    # The single-quoted `-c` body is a Python script — the only `$`/`\n`
    # inside it (the `printf '\n\n'` fallback) is a printf escape, not a
    # shell expansion, so single quotes are correct. Silence SC2016.
    # shellcheck disable=SC2016
    PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
# Worktree identifier — the docs name this `worktree_name`; the live harness
# on the pinned build sends it as `name` (the agent id). Tolerate BOTH, plus
# a couple of likely synonyms. This is the field that drives path derivation.
name = (
    d.get("worktree_name")
    or d.get("name")
    or d.get("agent_id")
    or d.get("agent_name")
    or d.get("id")
    or ""
)
# Explicit proposed path — absent in the real payload; belt-and-suspenders
# for a future build that DOES send one. Same synonym set as before so the
# parity tests keep pinning both scripts to the same vocabulary.
path = (
    d.get("worktree_path")
    or d.get("path")
    or d.get("proposed_path")
    or d.get("worktree")
    or d.get("target_path")
    or d.get("dir")
    or ""
)
# Repo root hint — optional; we always re-derive via git too.
repo = (
    d.get("repo_root")
    or d.get("repo")
    or d.get("project_root")
    or d.get("cwd")
    or d.get("toplevel")
    or ""
)
sys.stdout.write(str(name) + "\n")
sys.stdout.write(str(path) + "\n")
sys.stdout.write(str(repo))
' 2>/dev/null || printf '\n\n')
    WT_NAME="$(printf '%s' "$PARSED" | sed -n '1p')"
    PROPOSED_PATH="$(printf '%s' "$PARSED" | sed -n '2p')"
    REPO_HINT="$(printf '%s' "$PARSED" | sed -n '3p')"
fi

# norm_path: absolutise best-effort. realpath -m resolves even non-existent
# paths (the worktree dir does not exist yet at create time). Fall back to
# the raw value if realpath is unavailable.
norm_path() {
    local p="$1"
    if command -v realpath >/dev/null 2>&1; then
        realpath -m "$p" 2>/dev/null || printf '%s' "$p"
    else
        printf '%s' "$p"
    fi
}

# sanitize_id: reduce a worktree identifier to a filesystem-safe token. Keep
# [A-Za-z0-9._-]; collapse everything else to '-'. Empty/degenerate → a
# stable "agent" fallback so we never derive an empty path segment.
#
# v0.2.74 (M-3): the token is NOT injective — distinct raw ids like `agent/x`,
# `agent-x`, `agent\x` all collapse to `agent-x`. If two agents' raw ids
# collide, the idempotent-re-fire check would match the first's worktree and the
# second would silently REUSE it (the shared-tree bug, via collision). To keep
# the token readable AND collision-free, append a short deterministic hash of
# the RAW id. The live harness sends distinct hex ids (`agent-<hex>`) that never
# collide today, so this is belt-and-suspenders — but it makes reuse impossible
# for ANY distinct raw ids. Same rule mirrored in worktree-guard.ps1.
_id_short_hash() {
    # First 8 hex chars of a hash of the RAW id. Portable: prefer sha256sum,
    # fall back to cksum (POSIX). Never empty.
    local raw="$1" h=""
    if command -v sha256sum >/dev/null 2>&1; then
        h="$(printf '%s' "$raw" | sha256sum 2>/dev/null | cut -c1-8)"
    elif command -v shasum >/dev/null 2>&1; then
        h="$(printf '%s' "$raw" | shasum -a 256 2>/dev/null | cut -c1-8)"
    fi
    if [ -z "$h" ]; then
        # cksum → decimal; hex-ish enough for disambiguation.
        h="$(printf '%s' "$raw" | cksum 2>/dev/null | cut -d' ' -f1)"
    fi
    [ -n "$h" ] || h="0"
    printf '%s' "$h"
}

sanitize_id() {
    local raw="$1" out
    out="$(printf '%s' "$raw" | tr -c 'A-Za-z0-9._-' '-' )"
    # Neutralise any surviving `..` run (it's already one path segment, so it
    # can't traverse — this is purely for a tidy token) and collapse dot runs.
    out="$(printf '%s' "$out" | sed -E 's/\.{2,}/./g')"
    # Trim leading/trailing dashes/dots and collapse runs of dashes.
    out="$(printf '%s' "$out" | sed -E 's/-+/-/g; s/^[-.]+//; s/[-.]+$//')"
    [ -n "$out" ] || out="agent"
    # Append the raw-id hash so distinct raw ids never share a token/path.
    printf '%s-%s' "$out" "$(_id_short_hash "$raw")"
}

# ── Resolve the git toplevel (the repo this create is scoped to) ──────────
# Prefer the payload cwd hint when present, else PROJECT_ROOT. We use
# `git -C <dir> rev-parse --show-toplevel` so monorepos / subdir-of-repo
# layouts resolve to the REAL git toplevel.
GIT_SCOPE_DIR="${PROJECT_ROOT:-$PWD}"  # a read: the cwd is a fine scope
if [ -n "$REPO_HINT" ] && [ -d "$REPO_HINT" ]; then
    GIT_SCOPE_DIR="$REPO_HINT"
fi
TOPLEVEL=""
if command -v git >/dev/null 2>&1; then
    TOPLEVEL="$(git -C "$GIT_SCOPE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
fi

# ── Isolation notices (v0.2.102 W6-Q) ─────────────────────────────────────
# A successful WorktreeCreate cannot speak to anyone: the harness discards
# the hook's systemMessage. So when this hook has to create something other
# than an ordinary worktree of HEAD (no commits yet, or not a git repo at
# all), it leaves a one-shot notice keyed by the worktree identifier, and
# subagent-start-isolation-check.{sh,ps1} hands it to the subagent as
# additionalContext (the SubagentStart `agent_id` is this `name` minus its
# `agent-` prefix — observed on Claude Code 2.1.285). The subagent is told to
# repeat it in its final report, which is how the user learns why isolation
# is off or different. Notices live in .claude/state/worktree-guard/; an
# identifier outside [A-Za-z0-9._-] gets no notice (the log row remains).
NOTICE_DIR=""
if [ -n "$PROJECT_ROOT" ]; then NOTICE_DIR="$PROJECT_ROOT/.claude/state/worktree-guard"; fi

# Per-launch records (v0.2.102 W6-Q follow-up): the one home is
# _lib/worktree-records.sh. subagent-stop-reconcile reaps an unchanged
# worktree from its record; subagent-start-isolation-check uses the record to
# tell "the guard handled this agent" from "isolation was skipped".
if [ -f "$SCRIPT_DIR/_lib/worktree-records.sh" ]; then
    # shellcheck source=_lib/worktree-records.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/worktree-records.sh"
    if [ -n "$PROJECT_ROOT" ]; then wtrec_prune "$PROJECT_ROOT"; fi
fi
record_launch() {
    [ -n "$PROJECT_ROOT" ] || return 0
    command -v wtrec_write >/dev/null 2>&1 || return 0
    wtrec_write "$PROJECT_ROOT" "$WT_NAME" "$@"
}
write_notice() {
    case "$WT_NAME" in
        ''|*[!A-Za-z0-9._-]*|.*) return 0 ;;
    esac
    [ -n "$NOTICE_DIR" ] || return 0
    mkdir -p "$NOTICE_DIR" 2>/dev/null || return 0
    printf '%s\n' "$1" > "$NOTICE_DIR/notice-$WT_NAME.txt" 2>/dev/null || true
}

# ── Not a git repo ⇒ run in place, isolation OFF, said out loud ───────────
# No repository means nothing to base a worktree on. Echoing nothing is NOT
# "let the harness do its default": the WorktreeCreate contract is "If the
# hook fails or produces no path, worktree creation fails with an error"
# (https://code.claude.com/docs/en/hooks#worktreecreate-output), and without
# a hook the harness itself refuses ("not in a git repository and no
# WorktreeCreate hooks are configured"). Either way every `isolation:
# worktree` agent would be unlaunchable in a plain folder. So the hook
# returns the working directory itself: the harness accepts a directory with
# no git metadata when no repository contains it (docs: worktrees, "Claude
# Code refuses to use a worktree"), the agent launches, and the notice above
# tells it — and through its report, the user — that it edits the folder
# directly. A degenerate payload with no identifier stays a no-op (below).
if [ -z "$TOPLEVEL" ]; then
    if [ -z "$WT_NAME" ]; then
        log_event "noop" "not_a_repo" "$PROPOSED_PATH" ""
        exit 0
    fi
    RUN_DIR="$GIT_SCOPE_DIR"
    if command -v realpath >/dev/null 2>&1; then
        RUN_DIR="$(realpath "$RUN_DIR" 2>/dev/null || printf '%s' "$RUN_DIR")"
    fi
    write_notice "VCO worktree-guard: worktree isolation is OFF for this subagent. $RUN_DIR is not inside a git repository, so no isolated worktree can be created; you are working DIRECTLY in the project folder and every edit lands there immediately. Say this in your final report. Isolation turns on by itself once the folder is a git repository (git init)."
    log_event "in_place" "not_a_repo" "$PROPOSED_PATH" "$RUN_DIR"
    record_launch "in_place" "$RUN_DIR" "" ""
    printf 'worktree-guard: %s is not a git repository - the subagent runs in place, without worktree isolation\n' "$RUN_DIR" >&2
    emit_path "$RUN_DIR"
    exit 0
fi
TOPLEVEL_ABS="$(norm_path "$TOPLEVEL")"

# ── No commits yet ⇒ base the worktree on a snapshot commit ───────────────
# A freshly `git init`-ed repository has an unborn HEAD, so
# `git worktree add --detach <path> HEAD` fails ("fatal: invalid reference:
# HEAD") and every isolated agent aborts — the state of EVERY brand-new
# project until its first commit. Returning the main checkout instead is
# refused by the harness (it is "its own repository", not a separate
# worktree — verified live on Claude Code 2.1.285). So the hook builds a
# commit of the current working tree through a THROWAWAY index (the user's
# index, refs and HEAD are untouched; .gitignore is honoured; other agents'
# worktrees are excluded) and detaches the worktree there. The commit is on
# no branch; the notice gives the agent the exact command that carries its
# changes back into the main checkout.
#
# v0.2.102 W6-R: credential FILES are excluded from the snapshot. In a new
# project with no .gitignore, `add -A` would otherwise copy a `.env` into the
# snapshot commit (git's object store) and into the agent's worktree. The
# globs come from the ONE credential-file list VCO keeps
# (.claude/scripts/bash_security.py --credential-file-globs, the same list
# the secrets guard's `read_env_files` rule reads). When that list cannot be
# read (python or the script missing — a broken install) the snapshot is
# REFUSED rather than taken without the exclusion.
BASE_REF="HEAD"
BASE_REASON="worktree_add_detached_head"
SNAP_CRED_EXCLUDED=""
if ! git -C "$TOPLEVEL_ABS" rev-parse --verify -q 'HEAD^{commit}' >/dev/null 2>&1; then
    SNAP_SECURITY="$SCRIPT_DIR/../scripts/bash_security.py"
    SNAP_GLOBS=""
    if [ -n "${PY:-}" ] && [ -f "$SNAP_SECURITY" ]; then
        SNAP_GLOBS="$("$PY" "$SNAP_SECURITY" --credential-file-globs 2>/dev/null || true)"
    fi
    if [ -z "$SNAP_GLOBS" ]; then
        REASON="broken install: repository at $TOPLEVEL_ABS has no commits, and the credential-file list its snapshot must exclude could not be read (${PY:-no python interpreter} / $SNAP_SECURITY) - refusing to snapshot a working tree that may hold credential files; repair it with the project's bundle update (launcher: project Settings -> Update bundle)"
        log_event "create_failed" "$REASON" "$PROPOSED_PATH" ""
        printf 'worktree-guard: ABORT - %s\n' "$REASON" >&2
        exit 1
    fi
    SNAP_EXCLUDES=(':(exclude).claude/worktrees')
    while IFS= read -r _g; do
        [ -n "$_g" ] && SNAP_EXCLUDES+=(":(exclude,glob)$_g")
    done <<EOF
$SNAP_GLOBS
EOF
    SNAP_CRED_SPECS=()
    while IFS= read -r _g; do
        [ -n "$_g" ] && SNAP_CRED_SPECS+=(":(glob)$_g")
    done <<EOF
$SNAP_GLOBS
EOF
    # The credential files this snapshot leaves out (named in the notice).
    SNAP_CRED_EXCLUDED="$(git -C "$TOPLEVEL_ABS" ls-files --cached --others --exclude-standard \
        -- "${SNAP_CRED_SPECS[@]}" 2>/dev/null | sort -u | head -20 | tr '\n' ' ' || true)"
    SNAP_TMP="$(mktemp -d 2>/dev/null || true)"
    SNAP_SHA=""
    SNAP_ERR=""
    if [ -n "$SNAP_TMP" ]; then
        SNAP_IDX="$SNAP_TMP/index"
        if SNAP_ERR="$(GIT_INDEX_FILE="$SNAP_IDX" git -C "$TOPLEVEL_ABS" add -A -- . "${SNAP_EXCLUDES[@]}" 2>&1)"; then
            SNAP_TREE="$(GIT_INDEX_FILE="$SNAP_IDX" git -C "$TOPLEVEL_ABS" write-tree 2>/dev/null || true)"
            if [ -n "$SNAP_TREE" ]; then
                SNAP_SHA="$(GIT_AUTHOR_NAME="VCO worktree-guard" GIT_AUTHOR_EMAIL="worktree-guard@vco.invalid" \
                    GIT_COMMITTER_NAME="VCO worktree-guard" GIT_COMMITTER_EMAIL="worktree-guard@vco.invalid" \
                    git -C "$TOPLEVEL_ABS" -c commit.gpgsign=false commit-tree "$SNAP_TREE" \
                    -m "VCO worktree-guard: snapshot of the working tree (repository has no commits yet)" 2>/dev/null || true)"
            fi
        fi
        rm -rf "$SNAP_TMP" 2>/dev/null || true
    fi
    if [ -z "$SNAP_SHA" ]; then
        REASON="repository at $TOPLEVEL_ABS has no commits and a snapshot commit of its working tree could not be built: ${SNAP_ERR:-git write-tree/commit-tree failed}"
        log_event "create_failed" "$REASON" "$PROPOSED_PATH" ""
        printf 'worktree-guard: ABORT - %s\n' "$REASON" >&2
        exit 1
    fi
    BASE_REF="$SNAP_SHA"
    BASE_REASON="worktree_add_snapshot_no_head"
fi

# create_worktree ABS_PATH — run `git worktree add --detach ABS_PATH $BASE_REF`
# (HEAD, or the no-commits snapshot above).
# Idempotent: if ABS_PATH is already a registered worktree, treat as success.
# On genuine failure, emit the reason to stderr, log it, and abort NON-ZERO
# (per the strict contract — a failed create must be LOUD, never a silent
# shared-tree fallback). Returns via exit; never returns normally on failure.
create_worktree() {
    local target="$1" reason existing add_err rec_mode
    # Already registered at this exact path? (re-fire / retry) → success.
    # `git worktree list --porcelain` emits `worktree <abs-path>` lines.
    existing="$(git -C "$TOPLEVEL_ABS" worktree list --porcelain 2>/dev/null \
        | sed -n 's/^worktree //p')"
    if printf '%s\n' "$existing" | grep -qxF "$target"; then
        log_event "created" "idempotent_existing_worktree" "$PROPOSED_PATH" "$target"
        emit_path "$target"
        exit 0
    fi
    # Ensure the parent directory exists (e.g. <toplevel>/.claude/worktrees).
    mkdir -p "$(dirname "$target")" 2>/dev/null || true
    # Detached HEAD is the safest base: no branch-name collisions across
    # parallel agents, and the agent gets a clean separate checkout of HEAD.
    if add_err="$(git -C "$TOPLEVEL_ABS" worktree add --detach "$target" "$BASE_REF" 2>&1)"; then
        if [ "$BASE_REF" != "HEAD" ]; then
            write_notice "VCO worktree-guard: this repository has no commits yet, so your isolated worktree ($target) starts from a SNAPSHOT of the project's working tree (commit $BASE_REF, on no branch) instead of HEAD. The main checkout is untouched by your edits. Credential files (VCO's credential-file list: *.env, *.env.*, *.credentials, *.netrc, *.pgpass and the like) are EXCLUDED from the snapshot and are not in your worktree${SNAP_CRED_EXCLUDED:+ - left out here: ${SNAP_CRED_EXCLUDED% }}; if the task needs one, ask the caller instead of recreating it. Say this in your final report and give the user the command that brings your changes back: git -C \"$target\" add -A && git -C \"$target\" diff --binary --cached $BASE_REF | git -C \"$TOPLEVEL_ABS\" apply"
        fi
        log_event "created" "$BASE_REASON" "$PROPOSED_PATH" "$target"
        if [ "$BASE_REF" = "HEAD" ]; then rec_mode="head"; else rec_mode="snapshot"; fi
        record_launch "$rec_mode" "$target" "$(git -C "$target" rev-parse HEAD 2>/dev/null || true)" "$TOPLEVEL_ABS"
        emit_path "$target"
        exit 0
    fi
    # Failure → LOUD abort. Non-zero exit makes the harness surface the
    # failure instead of silently falling back to the shared parent tree.
    reason="git worktree add failed for $target: $add_err"
    log_event "create_failed" "$reason" "$PROPOSED_PATH" "$target"
    printf 'worktree-guard: ABORT — %s\n' "$reason" >&2
    exit 1
}

# ── Belt-and-suspenders: an explicit path WAS supplied ────────────────────
# The real payload has no path; this branch only fires on a future harness
# build that sends one. Validate it is a SEPARATE checkout (never the parent
# toplevel) and create it if absent.
if [ -n "$PROPOSED_PATH" ]; then
    PROPOSED_ABS="$(norm_path "$PROPOSED_PATH")"
    if [ "$PROPOSED_ABS" = "$TOPLEVEL_ABS" ]; then
        # Proposed path == parent checkout: creating a worktree there is
        # exactly the silent shared-tree collapse we exist to prevent.
        if [ -n "${VCT_WORKTREE_GUARD_ENFORCE:-}" ]; then
            REASON="explicit worktree path IS the parent checkout ($TOPLEVEL_ABS) — refusing (would collapse to the shared tree)"
            log_event "block" "$REASON" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
            printf 'worktree-guard: BLOCK — %s\n' "$REASON" >&2
            exit 2
        fi
        # Default: don't create at the parent; derive a safe separate path
        # from the identifier instead (fall through to derivation below).
        log_event "redirect_parent_path" "explicit path equals parent toplevel; deriving a separate worktree path instead" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
    else
        # A genuinely separate proposed path → create it there.
        create_worktree "$PROPOSED_ABS"
    fi
fi

# ── No usable identifier ⇒ graceful no-op ─────────────────────────────────
# We reach here with an empty WT_NAME only on a degenerate/malformed
# invocation (empty stdin, non-JSON, or a payload with neither an identifier
# nor an explicit path). Do NOT fabricate a worktree from a fallback token in
# that case — echo nothing + exit 0 so the harness uses its own default. The
# "agent" fallback in sanitize_id is reserved for the case where a NON-empty
# identifier sanitizes down to empty (e.g. name == "///").
if [ -z "$WT_NAME" ]; then
    log_event "noop" "no_worktree_identifier" "$PROPOSED_PATH" ""
    exit 0
fi

# ── Primary path: derive + create under the VCO convention ────────────────
# `<toplevel>/.claude/worktrees/<sanitized-id>`. This matches the harness's
# observed nesting (`.claude/worktrees/agent-<id>`) and the subagent-stop
# reconcile's expectations.
SAFE_ID="$(sanitize_id "$WT_NAME")"
DERIVED_PATH="$TOPLEVEL_ABS/.claude/worktrees/$SAFE_ID"
DERIVED_ABS="$(norm_path "$DERIVED_PATH")"
create_worktree "$DERIVED_ABS"
