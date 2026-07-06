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
#   2. cwd is NOT a git repo → this create cannot be isolated; echo nothing
#      + exit 0 so the harness does its own default (do NOT abort a
#      legitimate non-git spawn with a non-zero exit).
#   3. Derive `<toplevel>/.claude/worktrees/<sanitized-id>` and run
#      `git worktree add --detach <path> HEAD`.
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

PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"
LOG_DIR="$PROJECT_ROOT/.claude/logs"
LOG_FILE="$LOG_DIR/worktree-guard.jsonl"
mkdir -p "$LOG_DIR" 2>/dev/null || true

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
sanitize_id() {
    local raw="$1" out
    out="$(printf '%s' "$raw" | tr -c 'A-Za-z0-9._-' '-' )"
    # Neutralise any surviving `..` run (it's already one path segment, so it
    # can't traverse — this is purely for a tidy token) and collapse dot runs.
    out="$(printf '%s' "$out" | sed -E 's/\.{2,}/./g')"
    # Trim leading/trailing dashes/dots and collapse runs of dashes.
    out="$(printf '%s' "$out" | sed -E 's/-+/-/g; s/^[-.]+//; s/[-.]+$//')"
    [ -n "$out" ] || out="agent"
    printf '%s' "$out"
}

# ── Resolve the git toplevel (the repo this create is scoped to) ──────────
# Prefer the payload cwd hint when present, else PROJECT_ROOT. We use
# `git -C <dir> rev-parse --show-toplevel` so monorepos / subdir-of-repo
# layouts resolve to the REAL git toplevel.
GIT_SCOPE_DIR="$PROJECT_ROOT"
if [ -n "$REPO_HINT" ] && [ -d "$REPO_HINT" ]; then
    GIT_SCOPE_DIR="$REPO_HINT"
fi
TOPLEVEL=""
if command -v git >/dev/null 2>&1; then
    TOPLEVEL="$(git -C "$GIT_SCOPE_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
fi

# ── Not a git repo ⇒ graceful no-op ───────────────────────────────────────
# This create can't be isolated (no repo to base a worktree on). Echo nothing
# and exit 0 so the harness does its own default — do NOT abort with a
# non-zero exit, which would block a legitimate non-git spawn.
if [ -z "$TOPLEVEL" ]; then
    log_event "noop" "not_a_repo" "$PROPOSED_PATH" ""
    exit 0
fi
TOPLEVEL_ABS="$(norm_path "$TOPLEVEL")"

# create_worktree ABS_PATH — run `git worktree add --detach ABS_PATH HEAD`.
# Idempotent: if ABS_PATH is already a registered worktree, treat as success.
# On genuine failure, emit the reason to stderr, log it, and abort NON-ZERO
# (per the strict contract — a failed create must be LOUD, never a silent
# shared-tree fallback). Returns via exit; never returns normally on failure.
create_worktree() {
    local target="$1" reason existing add_err
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
    if add_err="$(git -C "$TOPLEVEL_ABS" worktree add --detach "$target" HEAD 2>&1)"; then
        log_event "created" "worktree_add_detached_head" "$PROPOSED_PATH" "$target"
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
