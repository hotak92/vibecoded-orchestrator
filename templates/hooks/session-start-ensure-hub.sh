#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# SessionStart hook: ensure vct-hub is running (Step 9, v0.2.21).
#
# Idempotent: `python -m vco_lib.hub_ensure ensure` leaves a live hub alone
# and otherwise invokes `vct-hub --start-if-not-running` (Step 5's CLI).
# Soft-fail throughout — never blocks Claude Code startup. Worst case:
# a single stderr line + exit 0.
#
# v0.2.92 (ruling R20): binary discovery and the spawn are NO LONGER mirrored
# here. They live in `vco_lib/hub_ensure.py`, the ONE home shared with the
# .ps1 sibling and the Rust launcher (`hub_launcher.rs`). The discovery chain
# it implements is unchanged:
#   1. $VCT_HUB_BIN        — explicit override (dev builds, custom installs)
#   2. <repo_root>/launcher/dist/<arch>/vct-hub  (INSTALL-FOLDER copy)
#      then <repo_root>/launcher/dist/vct-hub    (arch-less fallback)
#   3. PATH                — first `vct-hub` on PATH (install.py adds it)
#   4. $HOME/.vct/bin/vct-hub
# If none match the module exits 3 with a named reason; this hook turns that
# into one stderr line + exit 0.
#
# Env overrides:
#   $VCT_HUB_BIN          — explicit binary path (highest precedence).
#   $VCT_DISABLE_HOOKS    — set to non-empty to bypass entirely.
#   $VCO_HOOK_DEBUG=1     — verbose stderr, and run the spawn in the
#                           foreground (`--wait`) so its exit code is known.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ---------------------------------------------------------------------------
# debug :: emit a line to stderr only when $VCO_HOOK_DEBUG=1.
# ---------------------------------------------------------------------------
debug() {
    [ "${VCO_HOOK_DEBUG:-}" = "1" ] && echo "[vct] $*" >&2
    return 0
}

# ---------------------------------------------------------------------------
# v0.2.54 Track C (C-7): respect the orchestrator update gate.
#
# During `update_orchestrator` the launcher writes
# `<vct_root>/.update-in-progress.json` and explicitly STOPS vct-hub so
# the binary can be swapped (Windows mandatory locks). A Claude Code
# session starting mid-update would otherwise respawn the hub right
# here and re-lock `vct-hub.exe` between the stop and the swap —
# recreating the exact sharing-violation the stop was designed to
# prevent. MCP servers already honour this gate (exit 75); the hook
# now does too.
#
# The gate stays HERE, not in `vco_lib.hub_ensure`: the two callers answer
# it differently on purpose. The launcher parses the in-JSON deadline
# (`commands::update_gate::is_update_in_progress`); this hook has no JSON
# parser, so it uses the mtime proxy below. The launcher rewrites the
# lockfile on every phase advance and the expected update duration is
# 15 minutes, so "modified within the last 15 minutes" is a faithful
# proxy for the in-JSON `expected_completion_by` deadline. A crashed
# update's stale lockfile therefore never blocks the hook for more
# than 15 minutes (and the launcher's boot self-heal removes it).
# ---------------------------------------------------------------------------
UPDATE_GATE_FILE="${VCT_STATE_DIR:-$HOME/.vct}/.update-in-progress.json"
if [ -f "$UPDATE_GATE_FILE" ]; then
    if [ -n "$(find "$UPDATE_GATE_FILE" -mmin -15 2>/dev/null)" ]; then
        echo "[vct] orchestrator update in progress ($UPDATE_GATE_FILE) — skipping vct-hub auto-start" >&2
        exit 0
    fi
    debug "stale update gate file present (>15 min old) — ignoring"
fi

# ---------------------------------------------------------------------------
# Hub discovery + "start if not running": ONE home — `python -m vco_lib.hub_ensure`
# (v0.2.92, ruling R20). This hook, its .ps1 sibling and `hub_launcher.rs` each
# used to carry their own copy of the four-step chain; the copies had already
# drifted on arch-slot naming. Class A of the A>B>C rule: one Python
# implementation, called via a ~50 ms subprocess on this session-start path.
#
# Loud-fail: if the resolver cannot run at all (no interpreter, broken
# install), say so on stderr and skip — never fall back to an inline copy.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
if command -v resolve_vco_venv_python >/dev/null 2>&1; then
    resolve_vco_venv_python "$SCRIPT_DIR"
fi
RUN_PY="${VCO_VENV_PYTHON:-${PY:-}}"
if [ -z "$RUN_PY" ] || [ ! -x "$RUN_PY" ]; then
    # v0.2.92 MAJOR-6: stdout, not stderr. A SessionStart hook exiting 0 has
    # its stderr discarded — only stdout is injected as session context, so a
    # ">&2" report here reaches nobody on the one path that runs every session.
    echo "session-start-ensure-hub: no Python interpreter for vco_lib.hub_ensure (broken VCO install?); skipping"
    exit 0
fi

__vco_hub_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-hub-ensure.$$"
if [ "${VCO_HOOK_DEBUG:-}" = "1" ]; then
    # Foreground so the hub's own exit code is observable while debugging.
    __vco_hub_out="$("$RUN_PY" -m vco_lib.hub_ensure ensure --shell --wait --repo-root "$REPO_ROOT" 2>"$__vco_hub_err")"
    __vco_hub_rc=$?
else
    # The module detaches the spawn itself, so a slow first-time start cannot
    # block the SessionStart hook bus past its 10s budget.
    __vco_hub_out="$("$RUN_PY" -m vco_lib.hub_ensure ensure --shell --repo-root "$REPO_ROOT" 2>"$__vco_hub_err")"
    __vco_hub_rc=$?
fi
case "$__vco_hub_rc" in
    0|3|4) eval "$__vco_hub_out" ;;
    *)
        echo "session-start-ensure-hub: vco_lib.hub_ensure failed (rc=$__vco_hub_rc): $(tail -n 3 "$__vco_hub_err" 2>/dev/null | tr '\n' ' ')"
        rm -f "$__vco_hub_err"
        exit 0
        ;;
esac
rm -f "$__vco_hub_err"

# Soft-fail contract: the module exits 3 (no binary) / 4 (spawn failed) LOUDLY
# with a named reason; the hook reports it once and still exits 0, because a
# SessionStart hook must never block Claude Code from starting.
if [ "$__vco_hub_rc" != "0" ]; then
    echo "[vct] ${VCO_HUB_REASON:-vct-hub could not be started}"
    exit 0
fi

debug "vct-hub $VCO_HUB_STATE: ${VCO_HUB_BINARY:-pid ${VCO_HUB_PID:-?}}"
exit 0
