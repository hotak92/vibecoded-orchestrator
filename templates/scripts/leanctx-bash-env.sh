#!/usr/bin/env bash
# leanctx-bash-env.sh - DISABLED as of vibecoded-orchestrator 0.2.11
#
# WHAT THIS USED TO BE
# --------------------
# Previously this file was sourced via the BASH_ENV env var (set in
# .claude/settings.json) so every non-interactive Bash subprocess spawned
# by Claude Code's Bash tool would re-source it and get function-style
# wrappers around `git`, `npm`, `cargo`, `pip`, etc., that re-invoked the
# commands through `lean-ctx -c "..."` for ~90-97% output compression.
#
# WHY IT'S DISABLED
# -----------------
# The BASH_ENV approach is fork-bomb-prone on lean-ctx 3.x:
#
#   1. BASH_ENV propagates to EVERY child bash subprocess.
#   2. Each subprocess re-sources this file, redefining the wrappers.
#   3. lean-ctx 3.x's `-c "<cmd>"` semantics now invoke bash itself to
#      execute <cmd>; that child bash re-sources BASH_ENV; the wrapper
#      re-invokes lean-ctx -c; recursion explodes.
#
# Real damage observed: 4000+ processes spawned in seconds, 88% memory
# pressure, system OOM. Incident 2026-04-30 (lean-ctx 3.x rollout) and
# again on 2026-05-15 (recidiva). Full forensic write-up:
# knowledge/concepts/lean-ctx-shim-disabled.md (orchestrator KG).
#
# WHAT REPLACED IT
# ----------------
# Per-project PreToolUse hook .claude/hooks/lean-ctx-rewrite.sh registered
# in .claude/settings.json. The hook intercepts ONLY top-level Bash tool
# calls from Claude Code (no env-var inheritance, no recursion risk) and
# delegates to `lean-ctx hook rewrite` for the same compression coverage
# on the most important surface.
#
# DEFENSE-IN-DEPTH EARLY EXIT
# ---------------------------
# The file is preserved on disk (rather than deleted) so that any stray
# BASH_ENV pointing at this path — set manually or left behind by a
# pre-0.2.11 install we missed — still no-ops instead of fork-bombing.
return 0
