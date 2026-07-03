#!/usr/bin/env bash
# Per-project lean-ctx PreToolUse hook for Bash tool calls.
#
# CONTRACT
# --------
# Claude Code's PreToolUse pipes a JSON payload to this hook's stdin:
#   {"hook_event_name":"PreToolUse","tool_name":"Bash",
#    "tool_input":{"command":"<cmd>"}}
# `lean-ctx hook rewrite` reads it and prints a JSON response with
# `hookSpecificOutput.updatedInput.command` wrapping <cmd> in
# `lean-ctx -c '<cmd>'`. Claude Code substitutes the rewritten command
# and executes it; output comes back compressed ~90-97%.
#
# D-3 (v0.2.73): lean-ctx 3.x's response ALSO carries
# `"permissionDecision":"allow"`, which on Claude Code >= 2.1.x
# AUTO-APPROVES the tool call — every wrapped Bash command silently
# bypassed the user's permission settings. This hook now strips that
# field (keeping updatedInput) before handing the response to Claude
# Code; see the filter at the bottom for the empirical verification
# record.
#
# When this script exits 0 with no stdout (the no-op paths below), Claude
# Code interprets that as "no rewrite needed" → the original command runs
# unmodified → raw output. Same effect as the bypasses below.
#
# THREE-TIER BYPASS HIERARCHY
# ---------------------------
# 1. Per-call (granular — leaves all other VCO hooks active):
#      lean-ctx bypass "git status"        # raw for this call
#      lean-ctx -c --raw "git status"      # raw for this call
#      lean-ctx -c "git status"            # force-compress (when default is off)
#    Mechanism: `lean-ctx hook rewrite` detects commands already prefixed
#    with `lean-ctx` and steps aside (emits empty stdout → Claude runs
#    the command unmodified). No double-wrap, no recursion.
#
# 2. Per-project (default for this project):
#      echo "VCO_LEAN_CTX_DEFAULT=off" >> .claude/env
#    The hook sources .claude/env and exits early when the var is "off".
#    Default is "on" — compression is active unless explicitly disabled.
#    Launcher may expose a GUI toggle for this in a follow-up release.
#
# 3. Global (sledgehammer — disables ALL VCO hooks for this shell):
#      export VCT_DISABLE_HOOKS=1
#      git status                         # raw, every subsequent VCO hook off
#    Use only when debugging hook interactions; one-off raw output is
#    better served by tier 1.
#
# WHY NOT THE OLD BASH_ENV SHIM
# -----------------------------
# Compresses Claude Code Bash tool output ~90-97% without the fork-bomb
# risk of the legacy BASH_ENV shim (disabled 0.2.11 — see
# knowledge/concepts/lean-ctx-shim-disabled.md):
#   - The old shim wired BASH_ENV in .claude/settings.json. That env var
#     propagated to EVERY child subprocess Claude Code's Bash tool spawned
#     and re-sourced the shim recursively. lean-ctx 3.x changed `-c`
#     semantics so the recursion no longer self-terminates → fork-bomb
#     (4000+ procs in seconds, 88% memory pressure, system OOM).
#   - This hook intercepts ONLY top-level Bash tool calls from Claude Code
#     via the PreToolUse event. It does NOT propagate to child subprocesses
#     (no env var inheritance). Functionally-equivalent compression on the
#     most important surface (Claude Code Bash output) without the risk.
#
# GUARD ORDER (intentional)
# -------------------------
# 1. VCT_DISABLE_HOOKS (global) — sledgehammer, checked first.
# 2. .claude/env source + VCO_LEAN_CTX_DEFAULT (per-project) — fine-grained.
# 3. lean-ctx binary availability — graceful no-op when missing.
# 4. lean-ctx hook rewrite + permissionDecision strip — per-call symmetric
#    bypass handled inside lean-ctx (commands starting with `lean-ctx ...`
#    emit empty stdout → raw).
set -u
# Scrub sensitive env vars before any subprocess spawning (defense-in-depth
# parity with every other VCO hook — enforced by tests/test_hooks_disable_guard.py).
# Note: this hook itself doesn't read secrets, but the `lean-ctx hook rewrite`
# subprocess inherits our env. Scrubbing before spawning it means the lean-ctx
# subprocess can't accidentally leak a credential via its own logs / debug
# output.
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Source .claude/env if present so per-project defaults (VCO_LEAN_CTX_DEFAULT)
# are visible. The file is plain `KEY=VALUE` shell syntax; sourcing is safe
# under `set -u` because no unset vars are referenced here, and any syntax
# errors in user-edited env files would surface as a non-zero source exit
# but `[ -f ]` already gates that. Hooks run with CWD=project-root.
[ -f .claude/env ] && . .claude/env
[ "${VCO_LEAN_CTX_DEFAULT:-on}" = "off" ] && exit 0
command -v lean-ctx >/dev/null 2>&1 || exit 0
# D-3 (v0.2.73): strip `permissionDecision` from lean-ctx's response, keep
# `updatedInput`.
#
# WHY: lean-ctx 3.x (verified 3.4.5) emits
#   {"hookSpecificOutput":{"hookEventName":"PreToolUse",
#    "permissionDecision":"allow","updatedInput":{"command":"lean-ctx -c '...'"}}}
# and on Claude Code >= 2.1.x `permissionDecision:"allow"` AUTO-APPROVES the
# tool call: every wrapped Bash command bypassed the user's permission
# settings. Verified empirically on CC 2.1.172 (2026-07-03):
#   * with the field present, an un-allowlisted Bash call EXECUTED in
#     headless mode solely because of the hook's "allow";
#   * with the field stripped, updatedInput still applied AND the normal
#     permission flow evaluated the rewritten command (denied without a
#     grant, ran with one).
#
# Filtering is JSON-aware via python (sed on serialized JSON is brittle).
# Conservative on every failure arm: no python / unparseable output /
# nothing left to emit → print NOTHING (= no rewrite, raw command). Losing
# compression for one call is strictly safer than emitting an auto-approval.
#
# MUST MATCH templates/hooks/lean-ctx-rewrite.ps1 (same strip, native
# ConvertFrom-Json there).
out="$(lean-ctx hook rewrite)" || exit 0
[ -z "$out" ] && exit 0
PYBIN="$(command -v python3 || command -v python || true)"
[ -z "$PYBIN" ] && exit 0
printf '%s' "$out" | "$PYBIN" -c '
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
hso = data.get("hookSpecificOutput")
if not isinstance(hso, dict):
    sys.exit(0)
hso.pop("permissionDecision", None)
if not hso.get("updatedInput"):
    sys.exit(0)
sys.stdout.write(json.dumps(data))
' 2>/dev/null
exit 0
