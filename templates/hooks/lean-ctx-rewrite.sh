#!/usr/bin/env bash
# Per-project lean-ctx PreToolUse hook for Bash tool calls.
#
# CONTRACT (v0.2.101 — ALLOW-LIST INVERSION + LOSSLESS TEE)
# ----------------------------------------------------------
# Claude Code's PreToolUse pipes a JSON payload to this hook's stdin:
#   {"hook_event_name":"PreToolUse","tool_name":"Bash",
#    "tool_input":{"command":"<cmd>"}}
#
# The hook compresses ONLY commands on the one committed allow-list at
#   <hook-dir>/_lib/lean-ctx-allowlist.txt
# (a single rule table BOTH siblings parse — shared config, A>B>C tier B):
# package installs, image pulls, downloads, and (safe only because of the
# lossless tee below) test/build runners. Everything else runs RAW:
# loops, pipes, chains, redirects, command substitution, git (never
# allow-listed — owner rule), unknown commands, and anything matching a
# SEC-RAW credential pattern.
#
# For an allow-listed command the hook writes the command text VERBATIM to
# <project>/.claude/state/lean-ctx-tee/<ts>-<pid>-<ck>.cmd and emits
# `hookSpecificOutput.updatedInput.command` =
#   bash '<hook-dir>/_lib/lean-ctx-tee.sh' '<lean-ctx-bin>' '<raw-dir>' '<cmd-file>' '<ttl>'
# The wrapper runs the command, tees its FULL raw output to a .log beside
# the .cmd file (TTL-swept; default 168 h, knob VCO_LEAN_CTX_TEE_TTL_HOURS
# in .claude/env, 0 = keep forever), prints the lean-ctx-compressed
# output, ends with ONE pointer line naming the raw file, and exits with
# the command's own exit code. Compression is therefore lossless: the
# model reads the pointer file instead of re-running the command.
#
# The response is CONSTRUCTED BY THIS HOOK — every field is ours, so
# `permissionDecision` can never appear (the D-3 auto-approval footgun of
# lean-ctx 3.x responses is now structurally impossible, not filtered).
# All other tool_input fields (description, timeout, run_in_background)
# are preserved in updatedInput.
#
# When this script exits 0 with no stdout (every raw path below), Claude
# Code runs the original command unmodified → raw output.
#
# RETIRED (v0.2.101) — the allow-list subsumes them:
#   * TRIM-b git commit/push step-aside and TRIM-r read-only git verbs
#     (no git form is allow-listed, so all git runs raw);
#   * delegation of the wrap decision to lean-ctx's own rewrite handler
#     (and with it the permissionDecision strip filter — see above).
# KEPT: SEC-RAW (allow-listed commands CAN carry credentials: pip
# --index-url https://user:pass@host, curl -u / auth headers, wget
# --password, npm _authToken args, secret-shaped env prefixes).
#
# BYPASS (rare — compressed output always carries the pointer; use these
# only when the pointer is unexpectedly missing):
#   1. Per-call: prefix the command with `lean-ctx` (`lean-ctx bypass
#      "<cmd>"`, `lean-ctx -c --raw "<cmd>"`) — this hook steps aside for
#      any command whose program token is lean-ctx: no double-wrap.
#   2. Per-project: `VCO_LEAN_CTX_DEFAULT=off` in .claude/env (launcher:
#      project Hooks tab toggle).
#   3. Global: `export VCT_DISABLE_HOOKS=1` (disables ALL VCO hooks).
#
# WHY A PreToolUse HOOK, NOT THE OLD BASH_ENV SHIM
# -----------------------------------------------
# The pre-v0.2.11 BASH_ENV shim re-sourced itself into EVERY child
# subprocess; lean-ctx 3.x `-c` semantics made that recursion
# self-sustaining → fork-bomb (2026-04-30 + 2026-05-15 incidents: 4500+
# runaway procs in seconds, systemd-oomd tore down the session). This hook
# intercepts ONLY top-level Bash tool calls via PreToolUse and does not
# propagate through the environment.
#
# GUARD ORDER (intentional)
# -------------------------
# 1. VCT_DISABLE_HOOKS (global) — sledgehammer, checked first.
# 2. .claude/env source + VCO_LEAN_CTX_DEFAULT (per-project) — fine-grained.
# 3. lean-ctx binary availability — graceful no-op when missing.
# 4. Allow-list decision + cmd-file write + response construction; every
#    failure arm emits NOTHING (raw command under the normal permission
#    flow). Losing compression for one call is always safe; losing output
#    or auto-approving a call never is.
#
# MUST MATCH templates/hooks/lean-ctx-rewrite.ps1 (same decision rule,
# same SEC-RAW list, same allow-list file; behavioural parity locked by
# tests/test_v02101_lean_ctx_allowlist_tee.py).
set -u
# Scrub sensitive env vars before any subprocess spawning (defense-in-depth
# parity with every other VCO hook — enforced by tests/test_hooks_disable_guard.py).
# Note: this hook itself doesn't read secrets, but spawned subprocesses
# inherit our env; scrubbing first means they can't leak a credential via
# their own logs / debug output.
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Source .claude/env if present so per-project knobs (VCO_LEAN_CTX_DEFAULT,
# VCO_LEAN_CTX_TEE_TTL_HOURS) are visible. Plain `KEY=VALUE` shell syntax;
# hooks run with CWD=project-root.
[ -f .claude/env ] && . .claude/env
# SF-3 (v0.2.101 review): case-INsensitive "off", matching the .ps1's
# .ToLowerInvariant() and the launcher GUI mapping — a hand-edited `Off`
# must not render "off" in the launcher while POSIX keeps compressing.
# POSIX case-glob (bash 3.2-safe; ${var,,} is bash 4+).
case "${VCO_LEAN_CTX_DEFAULT:-on}" in
    [oO][fF][fF]) exit 0 ;;
esac

_lc_cmd="$(cat 2>/dev/null || true)"
[ -n "$_lc_cmd" ] || exit 0

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

PYBIN="$(command -v python3 || command -v python || true)"
[ -z "$PYBIN" ] && exit 0

# Resolve the sibling allow-list + tee wrapper next to THIS hook. Works
# both installed (<project>/.claude/hooks/) and in the template checkout
# (templates/hooks/), because both live beside their _lib/ directory.
_lc_hook_dir="$(cd "$(dirname "$0")" 2>/dev/null && pwd)" || exit 0
_lc_allowlist="$_lc_hook_dir/_lib/lean-ctx-allowlist.txt"
_lc_tee="$_lc_hook_dir/_lib/lean-ctx-tee.sh"
[ -f "$_lc_allowlist" ] || exit 0
[ -f "$_lc_tee" ] || exit 0
_lc_proj="${CLAUDE_PROJECT_DIR:-$(pwd)}"
_lc_rawdir="$_lc_proj/.claude/state/lean-ctx-tee"

export _LC_ALLOWLIST="$_lc_allowlist"
export _LC_RAWDIR="$_lc_rawdir"
export _LC_TEE="$_lc_tee"
export _LC_BIN="$LEAN_CTX_BIN"
export _LC_TTL="${VCO_LEAN_CTX_TEE_TTL_HOURS:-168}"

# Decision + cmd-file write + response construction in ONE python hop
# (JSON + regex logic stays robust vs brittle shell string splitting).
# EVERY failure arm prints nothing → the original command runs raw under
# the normal permission flow. No single-quote character may appear in the
# python source below (shell single-quoted -c argument).
printf '%s' "$_lc_cmd" | "$PYBIN" -c '
import json, os, re, shlex, sys, time, zlib

def nothing():
    sys.exit(0)

try:
    d = json.load(sys.stdin)
    ti = d.get("tool_input") or {}
    cmd = ti.get("command", "")
except Exception:
    nothing()
if not isinstance(cmd, str) or not cmd.strip():
    nothing()

# SEC-RAW (2026-07-21; kept through the v0.2.101 allow-list inversion):
# allow-listed commands CAN carry credentials — pip install --index-url
# https://user:pass@host/simple, curl -u / auth headers, wget --password,
# npm registry _authToken args, secret-shaped env prefixes. Any hit
# ANYWHERE in the command → raw. Losing compression for one call is
# strictly safer than corrupting or leaking a credential. Pattern list
# between SEC-RAW-PATTERNS-BEGIN/END MUST MATCH lean-ctx-rewrite.ps1
# (parity-pinned by tests/test_d11_trimb_lean_ctx_discovery_and_git_bypass.py
# and tests/test_v02101_lean_ctx_allowlist_tee.py).
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
    r"://[^\s/:@]+:[^\s/@]+@",
    r"(?i)\b_?auth(?:token|_token)?\s*=",
)
# SEC-RAW-PATTERNS-END
for _p in _SECRET_PATTERNS:
    if re.search(_p, cmd):
        nothing()

# Shape gate: single simple commands only. Loops, pipes, chains,
# redirects, subshells, command substitution → raw (owner rule; the
# measured harm cases were compound commands). A false negative costs one
# compression; a false positive risks mangling evidence.
if re.search(r"[|&;<>()\n\r`]", cmd) or "$(" in cmd:
    nothing()

toks = cmd.split()
_env_assign = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_i = 0
while _i < len(toks) and _env_assign.match(toks[_i]):
    _i += 1
if _i >= len(toks):
    nothing()
rest = toks[_i:]
# basename the program token: .venv/bin/python -m pytest == python -m pytest
rest[0] = rest[0].replace("\\", "/").rsplit("/", 1)[-1]
if rest[0] == "lean-ctx":
    # per-call bypass / forced form — step aside, never double-wrap.
    nothing()

entries = []
try:
    with open(os.environ.get("_LC_ALLOWLIST", ""), encoding="utf-8") as f:
        for _line in f:
            _entry = _line.split("#", 1)[0].strip()
            if _entry:
                entries.append(_entry.split())
except OSError:
    nothing()
if not any(len(e) <= len(rest) and rest[:len(e)] == e for e in entries):
    nothing()

# Allow-listed → write the cmd file (verbatim; the wrapper re-reads it, so
# the command text never travels through another quoting layer) and emit
# the wrapper rewrite. Any write failure → nothing → raw.
_ttl = os.environ.get("_LC_TTL", "168")
if not re.match(r"^[0-9]+$", _ttl):
    _ttl = "168"
try:
    _rawdir = os.environ["_LC_RAWDIR"]
    os.makedirs(_rawdir, 0o700, exist_ok=True)
    # SF-1: the dir holds raw command OUTPUT that can carry credentials
    # (SEC-RAW guards the command text, not the output) — 0700 even when
    # an older run left it 0755.
    os.chmod(_rawdir, 0o700)
    _ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    _ck = format(zlib.crc32(cmd.encode("utf-8", "replace")) & 0xFFFFFFFF, "08x")
    _cmdfile = os.path.join(_rawdir, _ts + "-" + str(os.getpid()) + "-" + _ck + ".cmd")
    # 0600 AT BIRTH (O_CREAT mode; umask can only remove bits) — never a
    # world-readable window, not even empty.
    _fd = os.open(_cmdfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(_fd, "w", encoding="utf-8", newline="") as f:
        f.write(cmd)
except (OSError, KeyError):
    nothing()
_wrapped = "bash " + " ".join([
    shlex.quote(os.environ["_LC_TEE"]),
    shlex.quote(os.environ["_LC_BIN"]),
    shlex.quote(_rawdir),
    shlex.quote(_cmdfile),
    shlex.quote(_ttl),
])
_ui = dict(ti)
_ui["command"] = _wrapped
sys.stdout.write(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": _ui,
    },
}))
' 2>/dev/null
exit 0
