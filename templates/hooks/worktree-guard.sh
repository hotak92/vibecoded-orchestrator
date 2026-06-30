#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# worktree-guard.sh — WorktreeCreate hook (Layer 0, primary deterministic
# gate) for the worktree-isolation silent-fallback safeguard.
#
# ── Why this exists ──────────────────────────────────────────────────────
# The 2026-06-30 incident: three subagents dispatched with
# `isolation: worktree` all silently wrote to the SHARED parent working
# tree (the parent was dirty at dispatch time). Their reported cwd was
# `.claude/worktrees/agent-<id>` yet their commits/edits landed on the
# parent branch. The SILENCE is the bug — there was no deterministic,
# hook-level gate at the worktree-create instant.
#
# This hook is that gate. It fires only when the harness is actually about
# to create a worktree (i.e. isolation WAS requested), so it is
# automatically a NO-OP for non-isolation work, the deliberate-shared-tree
# case, and non-git projects — none of those fire WorktreeCreate.
#
# ── stdout contract (THE cross-OS-critical bit) ──────────────────────────
# Per Claude Code's hook table (ORCHESTRATOR-CLAUDE.md.template:504),
# WorktreeCreate can-block = Yes and the contract is "Must print absolute
# worktree path on stdout." So a hook can:
#   (a) ACCEPT by echoing the proposed path back,
#   (b) REDIRECT to a safer path by echoing a different path,
#   (c) BLOCK by refusing (non-zero / no path) with a human reason.
# We use (a) on the happy path and (c) ONLY on the one unambiguous
# violation (proposed path == / inside the parent checkout).
#
# ── Staged enable (log-only first) ───────────────────────────────────────
# VCO has NEVER exercised WorktreeCreate, so the live stdin schema + the
# exact stdout-consumption semantics must be verified against a real spawn
# before we rely on the BLOCK path. Therefore the block branch is gated
# behind VCT_WORKTREE_GUARD_ENFORCE (default off → log-only). In log-only
# mode a clear violation is LOGGED loudly to the JSONL but the proposed
# path is still echoed through (never break a legitimate create). The
# integrator verifies the round-trip from .claude/logs/worktree-guard.jsonl
# across a few real isolation spawns THIS cycle, then flips the flag — this
# is a same-cycle staged enable, NOT a deferred TODO.
#
# FALLBACK: if the pinned Claude Code build does NOT consume stdout-as-path
# (i.e. the echoed path is ignored), this hook degrades to warn+log and the
# SubagentStart isolation-check + SubagentStop violation-alert backstops act
# as the block-equivalent (post-hoc loud detection). That is the accepted
# cycle outcome, not a deferred fix.
#
# ── Cross-OS parity ──────────────────────────────────────────────────────
# MUST behave identically to worktree-guard.ps1 on the stdout-path
# contract: same decision matrix, same single-block case, same
# echo-through-on-any-doubt discipline. Keep them in lockstep — any change
# to the decision logic here MUST be mirrored in worktree-guard.ps1.
#
# ── Tunables ─────────────────────────────────────────────────────────────
#   VCT_DISABLE_HOOKS        — global bypass (consistent with every hook).
#   VCT_WORKTREE_GUARD_ENFORCE=1 — flip from log-only to block-on-violation.
#   VCT_WORKTREE_GUARD_STRICT=1  — upgrade dirty-parent WARN to a BLOCK
#                                  (belt-and-suspenders; off by default
#                                  because a dirty parent does NOT by itself
#                                  make a SEPARATE worktree unsafe).

# Scrub sensitive env vars before any subprocess spawning.
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null

# Global bypass — but we must still satisfy the stdout contract if we can
# cheaply echo back a proposed path. When hooks are globally disabled we
# echo nothing (the harness uses its own default) and exit 0. This matches
# every other hook's VCT_DISABLE_HOOKS behaviour (full no-op).
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# NOTE: deliberately NOT `set -e`. A WorktreeCreate hook that aborts mid-way
# on an unexpected non-zero would either block a legitimate spawn (if the
# build treats non-zero as block) or emit no path. We want explicit control
# over every exit; soft-fail everywhere and only ever block on the one
# explicit violation branch.
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

# log_event KEY DECISION REASON PROPOSED RESOLVED — append a JSONL row.
# ALWAYS capture the FULL received payload (RAW_STDIN_FOR_PY) so the
# integrator can verify the live schema field names + stdout semantics.
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

# Parse stdin defensively (synonym-tolerant). We need the proposed worktree
# path and (optionally) the repo root. Emit two stdout lines:
#   line 1 = proposed worktree path (or empty)
#   line 2 = repo root hint (or empty)
# Any JSON error → both empty (downstream treats as "no path to validate").
PROPOSED_PATH=""
REPO_HINT=""
if [ -n "$HOOK_STDIN" ] && [ -n "${PY:-}" ]; then
    PARSED=$(printf '%s' "$HOOK_STDIN" | "$PY" -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
# Proposed worktree path — try the documented + likely synonyms. The
# WorktreeCreate stdin schema is not frozen; be liberal.
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
sys.stdout.write(str(path) + "\n")
sys.stdout.write(str(repo))
' 2>/dev/null || printf '\n')
    PROPOSED_PATH="$(printf '%s' "$PARSED" | sed -n '1p')"
    REPO_HINT="$(printf '%s' "$PARSED" | sed -n '2p')"
fi

# ── Step 1: parse failure / no path ───────────────────────────────────────
# Cannot parse a path ⇒ nothing to validate ⇒ echo back whatever we got
# (possibly empty) and exit 0. Never break a legitimate create.
if [ -z "$PROPOSED_PATH" ]; then
    log_event "noop" "no_proposed_path_parsed" "" ""
    emit_path "$PROPOSED_PATH"
    exit 0
fi

# Normalise the proposed path to absolute (best-effort). realpath -m
# resolves even non-existent paths (the worktree dir does not exist yet at
# create time). Fall back to the raw value if realpath is unavailable.
norm_path() {
    local p="$1"
    if command -v realpath >/dev/null 2>&1; then
        realpath -m "$p" 2>/dev/null || printf '%s' "$p"
    else
        printf '%s' "$p"
    fi
}
PROPOSED_ABS="$(norm_path "$PROPOSED_PATH")"

# ── Step 2: not-a-repo ⇒ graceful no-op ───────────────────────────────────
# Resolve the repo toplevel relative to the project root. We use
# `git -C "$PROJECT_ROOT"` so monorepos / subdir-of-bigger-repo layouts
# resolve to the REAL git toplevel, never an assumed project root.
TOPLEVEL=""
if command -v git >/dev/null 2>&1; then
    TOPLEVEL="$(git -C "$PROJECT_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
fi
if [ -z "$TOPLEVEL" ]; then
    # Not inside any git repo ⇒ nothing to isolate ⇒ echo through.
    log_event "noop" "not_a_repo" "$PROPOSED_ABS" ""
    emit_path "$PROPOSED_ABS"
    exit 0
fi
TOPLEVEL_ABS="$(norm_path "$TOPLEVEL")"

# ── Step 3+4: validate the proposed path is a SEPARATE checkout ────────────
# CLEAR VIOLATION when the proposed worktree path IS the parent checkout
# toplevel, OR is the parent toplevel itself by any path form. A worktree
# that resolves to the primary checkout shares HEAD ⇒ exactly the silent
# shared-tree fallback we must stop.
#
# We treat "inside the parent checkout" carefully: the harness legitimately
# places worktrees under `<toplevel>/.claude/worktrees/agent-<id>` in some
# builds — that is a path INSIDE the toplevel directory tree but is still a
# genuinely separate `git worktree` (its own HEAD). So "inside the toplevel
# directory" is NOT by itself a violation. The unambiguous violation is the
# narrow case: proposed == toplevel (the worktree path equals the primary
# checkout root). That is the one case we block.
IS_VIOLATION=0
if [ "$PROPOSED_ABS" = "$TOPLEVEL_ABS" ]; then
    IS_VIOLATION=1
fi

if [ "$IS_VIOLATION" -eq 1 ]; then
    REASON="isolation:worktree requested but the proposed worktree path IS the parent checkout ($TOPLEVEL_ABS) — refusing to avoid silent shared-tree fallback"
    if [ -n "${VCT_WORKTREE_GUARD_ENFORCE:-}" ]; then
        # ENFORCE mode: BLOCK. Emit the reason on stderr, no path on stdout,
        # exit non-zero. (The exact "no path + non-zero = block" semantics
        # are what the integrator verifies on a live spawn before relying
        # on this branch.)
        log_event "block" "$REASON" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
        printf 'worktree-guard: BLOCK — %s\n' "$REASON" >&2
        exit 2
    else
        # LOG-ONLY mode (default until verified-on-live-spawn): log loudly
        # but echo the proposed path through so we never break a create
        # while the contract is still unverified. The SubagentStart /
        # SubagentStop backstops will catch the resulting shared-tree write.
        log_event "violation_logged_only" "$REASON" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
        printf 'worktree-guard: WARNING (log-only) — %s\n' "$REASON" >&2
        emit_path "$PROPOSED_ABS"
        exit 0
    fi
fi

# ── Step 5: dirty-parent handling (the incident trigger) ───────────────────
# A dirty parent does NOT by itself make a SEPARATE worktree unsafe — git
# can create a worktree from a dirty primary tree. The danger is only the
# fallback collapsing to the shared tree, which Step 4 already guards. So:
# WARN by default, escalate to BLOCK only under VCT_WORKTREE_GUARD_STRICT.
PARENT_DIRTY=0
if command -v git >/dev/null 2>&1; then
    if [ -n "$(git -C "$TOPLEVEL_ABS" status --porcelain 2>/dev/null)" ]; then
        PARENT_DIRTY=1
    fi
fi

if [ "$PARENT_DIRTY" -eq 1 ]; then
    REASON="parent checkout ($TOPLEVEL_ABS) is dirty at worktree-create time — separate worktree still safe, but a fallback to the shared tree would not be"
    if [ -n "${VCT_WORKTREE_GUARD_STRICT:-}" ] && [ -n "${VCT_WORKTREE_GUARD_ENFORCE:-}" ]; then
        # Belt-and-suspenders: strict + enforce together upgrade dirty-parent
        # to a block. (Strict alone, without enforce, stays a warn — we never
        # block before the stdout contract is verified.)
        log_event "block" "$REASON" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
        printf 'worktree-guard: BLOCK (strict) — %s\n' "$REASON" >&2
        exit 2
    fi
    # WARN (not block): the proposed path is a SEPARATE checkout (Step 4
    # passed) so the worktree itself is safe; we just record the dirty-parent
    # signal and echo the path through. Exit here so we don't also emit the
    # Step-6 "pass" row — the last log row should read "warn_dirty_parent".
    log_event "warn_dirty_parent" "$REASON" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
    printf 'worktree-guard: WARNING — %s\n' "$REASON" >&2
    emit_path "$PROPOSED_ABS"
    exit 0
fi

# ── Step 6: happy path ─────────────────────────────────────────────────────
# Proposed path validated as a separate checkout, clean parent. Echo the
# (absolute) worktree path on stdout so the harness uses it. Satisfies the
# contract.
log_event "pass" "validated_separate_checkout" "$PROPOSED_ABS" "$TOPLEVEL_ABS"
emit_path "$PROPOSED_ABS"
exit 0
