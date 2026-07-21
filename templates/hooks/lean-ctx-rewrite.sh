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

# TRIM-b (v0.2.75): auto-bypass compression for `git commit` / `git push`.
# lean-ctx's default mode can swallow stderr from a hook-failed `git commit`
# to ZERO output (exit 1, no message — the file stays staged) making the
# failure invisible. Rather than force the user to remember `lean-ctx
# bypass`, step aside for these commands: emit nothing → Claude runs the
# command RAW under the normal permission flow. We gate on the FINAL
# `&&`-segment so `git log && git commit` still passes through (the commit
# governs) while `echo git commit` (a benign echo, not a real commit) is
# still compressed. MUST MATCH templates/hooks/lean-ctx-rewrite.ps1.
#
# SEC-RAW (2026-07-21): credential-bearing commands ALSO run raw. lean-ctx's
# wrap heuristic is not credential-aware: wrapped inline commands carrying
# auth material have returned 401 with VALID tokens (field incident, Jira
# basic-auth curl), and a multi-line wrap corruption once leaked a secret
# into error output. The guard is deterministic at THIS layer: if ANY part
# of the command matches a credential pattern (auth headers, user:pass
# flags, secret-shaped env-var names, well-known token literals, the
# vct-secrets tooling), the hook emits nothing and the command runs raw.
# Losing compression for one call is strictly safer than corrupting or
# leaking a credential. Unlike the git gate, this scans the WHOLE command,
# not just the final `&&` segment — a credential anywhere disqualifies the
# wrap. Pattern list between SEC-RAW-PATTERNS-BEGIN/END MUST MATCH
# lean-ctx-rewrite.ps1 (parity-pinned by
# tests/test_d11_trimb_lean_ctx_discovery_and_git_bypass.py).
_lc_cmd="$(cat 2>/dev/null || true)"
if [ -n "$_lc_cmd" ]; then
    PYBIN_BP="$(command -v python3 || command -v python || true)"
    if [ -n "$PYBIN_BP" ]; then
        # Parse tool_input.command, run the SEC-RAW credential scan over the
        # whole command, then the TRIM-b git check on the last `&&` segment.
        # Python keeps the JSON + regex logic robust vs brittle shell string
        # splitting. Prints "raw" when we must step aside, empty otherwise.
        # Conservative: any parse failure prints nothing → normal
        # (compressed) path continues.
        _lc_decision="$(printf '%s' "$_lc_cmd" | "$PYBIN_BP" -c '
import json, re, sys
try:
    d = json.load(sys.stdin)
    cmd = (d.get("tool_input") or {}).get("command", "")
except Exception:
    sys.exit(0)
if not isinstance(cmd, str) or not cmd.strip():
    sys.exit(0)
# SEC-RAW-PATTERNS-BEGIN
_SECRET_PATTERNS = (
    r"(?i)\bauthorization\s*:",
    r"(?i)\b(x-api-key|private-token|x-auth-token|api-key)\s*:",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
    r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}",
    r"(?:^|\s)(?:-u|--user|--proxy-user)\s+[^\s:]+:\S",
    r"--(?:password|http-password|api-key|token|access-token)[= ]",
    r"\$\{?[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL)",
    r"\b[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL)[A-Za-z_]*=\S",
    r"\bvct\s+(?:exec|get)\b",
    r"vct_secrets_resolve",
    r"agent_secrets",
    r"\b(?:ATATT[A-Za-z0-9_=-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|ghs_[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9_-]{8,}|xox[bpoas]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,})",
)
# SEC-RAW-PATTERNS-END
for _p in _SECRET_PATTERNS:
    if re.search(_p, cmd):
        sys.stdout.write("raw")
        sys.exit(0)
# FINAL && segment governs (last command actually run in the chain).
seg = cmd.split("&&")[-1].strip()
toks = seg.split()
if len(toks) >= 2 and toks[0] == "git" and toks[1] in ("commit", "push"):
    sys.stdout.write("raw")
' 2>/dev/null || true)"
        if [ "$_lc_decision" = "raw" ]; then
            exit 0
        fi
    fi
fi

# D-11 (v0.2.75): probe the same candidate list install.py uses before
# giving up. `command -v` only checks PATH; a `cargo install lean-ctx`
# binary lands at ~/.cargo/bin, which a non-interactive hook shell's PATH
# often lacks (cargo adds it to ~/.profile, not every shell). Without this
# probe, install declares "lean-ctx detected" while the hook shell can't
# see it → compression silently never activates (the "assigned ≠ landed"
# case, F1 NEW-3). MUST MATCH the CANONICAL POSIX candidate order in
# install.py::_find_lean_ctx_binary (the ":9497" comment there names this
# hook as its mirror — keep the two lists identical; if you add/remove a
# path in one, mirror it in the other AND in lean-ctx-rewrite.ps1).
LEAN_CTX_BIN=""
if command -v lean-ctx >/dev/null 2>&1; then
    LEAN_CTX_BIN="lean-ctx"
else
    for _cand in \
        "$HOME/.cargo/bin/lean-ctx" \
        "$HOME/.local/bin/lean-ctx" \
        "/usr/local/bin/lean-ctx" \
        "/usr/bin/lean-ctx" \
        "/opt/homebrew/bin/lean-ctx" \
        "/home/linuxbrew/.linuxbrew/bin/lean-ctx"; do
        if [ -x "$_cand" ]; then
            LEAN_CTX_BIN="$_cand"
            break
        fi
    done
fi
[ -z "$LEAN_CTX_BIN" ] && exit 0
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
#
# TRIM-b (v0.2.75): stdin was already consumed into $_lc_cmd above (for the
# git-commit/push step-aside), so re-feed it to `lean-ctx hook rewrite`
# rather than reading an empty stdin. Invoke the RESOLVED binary
# ($LEAN_CTX_BIN — may be a candidate-path absolute, not on PATH; D-11).
out="$(printf '%s' "$_lc_cmd" | "$LEAN_CTX_BIN" hook rewrite)" || exit 0
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
