#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# SessionStart hook: ensure VCO's detached services are running — vct-hub
# (Step 9, v0.2.21) and, since v0.2.95, a model gateway the user REGISTERED for
# autostart plus the launcher GUI (tray-only, ruling R2). One hook for all
# three, per ruling R20 ("one ensure mechanism, not a second"); the file name
# predates the other two services and is kept because renaming a shipped hook
# churns every project's manifest for nothing. `.vscode/tasks.json` runs this
# same file on VS Code's `folderOpen`, which is what makes "auto-start when VS
# Code starts" true without a Claude Code session.
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
__vco_hub_reported=0
case "$__vco_hub_rc" in
    0|3|4) eval "$__vco_hub_out" ;;
    *)
        echo "session-start-ensure-hub: vco_lib.hub_ensure failed (rc=$__vco_hub_rc): $(tail -n 3 "$__vco_hub_err" 2>/dev/null | tr '\n' ' ')"
        __vco_hub_reported=1
        ;;
esac
rm -f "$__vco_hub_err"

# Soft-fail contract: the module exits 3 (no binary) / 4 (spawn failed) LOUDLY
# with a named reason; the hook reports it once and still exits 0, because a
# SessionStart hook must never block Claude Code from starting.
if [ "$__vco_hub_rc" != "0" ]; then
    [ "$__vco_hub_reported" = "1" ] || echo "[vct] ${VCO_HUB_REASON:-vct-hub could not be started}"
else
    debug "vct-hub $VCO_HUB_STATE: ${VCO_HUB_BINARY:-pid ${VCO_HUB_PID:-?}}"
fi

# ---------------------------------------------------------------------------
# Model gateway (v0.2.95, R5c) — ensure a REGISTERED gateway is running.
#
# Here rather than in a hook of its own, per ruling R20: one ensure mechanism
# per session, not two registrations, two settings-template entries and two
# copies of the update gate above. The file KEEPS its name — renaming a shipped
# hook churns every project's manifest and the retirement registry for a
# cosmetic gain — so read it as "ensure VCO's detached services", of which the
# hub is the first and the gateway the second.
#
# It runs even when the hub leg failed, and that is deliberate: the gateway
# starts fine without the hub (vendor-key resolution is lazy and retried), so
# making its availability depend on the hub's would be an invented dependency.
#
# `vco_lib.gateway_ensure` decides everything; this leg is a call and a report.
# The three states it can answer with:
#   not_registered  — the default. Autostart is opt-in, so this is a silent,
#                     successful no-op. NOTHING here ever registers it.
#   running         — the daemon's own pid/port guard says so; nothing is done.
#   registered_but_unrunnable (rc 3) — the eight-hour state of 2026-09-10.
#                     Reported, never restarted: the init system is already
#                     retrying an argv that fails the same way every time.
# On Linux a start is preceded by `systemctl --user reset-failed`, because a
# unit parked by StartLimitBurst answers `start` with "repeated too quickly"
# and does nothing — a no-op exactly when the ensure is needed.
# ---------------------------------------------------------------------------
__vco_gw_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-gateway-ensure.$$"
__vco_gw_out="$("$RUN_PY" -m vco_lib.gateway_ensure ensure --shell --folder "${CLAUDE_PROJECT_DIR:-$PWD}" 2>"$__vco_gw_err")"
__vco_gw_rc=$?
case "$__vco_gw_rc" in
    0|3|4)
        eval "$__vco_gw_out"
        [ "$__vco_gw_rc" = "0" ] || echo "[vct] ${VCO_GATEWAY_REASON:-the model gateway could not be ensured}"
        debug "model-gateway ${VCO_GATEWAY_STATE:-?}: ${VCO_GATEWAY_REASON:-}"
        ;;
    *)
        echo "session-start-ensure-hub: vco_lib.gateway_ensure failed (rc=$__vco_gw_rc): $(tail -n 3 "$__vco_gw_err" 2>/dev/null | tr '\n' ' ')"
        ;;
esac
rm -f "$__vco_gw_err"

# ---------------------------------------------------------------------------
# Launcher GUI (v0.2.95, R2) — ensure it is running, TRAY ONLY.
#
# The owner's ruling is "the launcher and the hub must auto-start when VS Code
# starts". This file is already wired to that event twice: Claude Code fires it
# on SessionStart, and `.vscode/tasks.json` runs it on VS Code's `folderOpen`
# (no Claude Code session needed). A login-time boot registration — the other
# candidate — fires at a DIFFERENT moment, starts a GUI for logins that never
# open an editor, cannot bring back a launcher the user quit, and would need a
# third home for boot registration. So the leg lives here, next to the hub's
# and the gateway's, per ruling R20.
#
# `vco_lib.launcher_ensure` decides everything; this leg is a call and a
# report. What it guarantees, and why each is not left to chance:
#   * NO WINDOW STEAL — the spawn passes `--start-hidden`, which the launcher
#     applies to its window config BEFORE any window exists. Nothing is
#     mapped, activated or destroyed: that sequence as a side effect is what
#     aborted mutter and killed a whole desktop session on 2026-09-09.
#   * NO SECOND INSTANCE — it only spawns when a process scan finds nothing,
#     and the launcher's single-instance plugin refuses a duplicate that races
#     it (and, seeing the flag in the duplicate's argv, does not take focus
#     either).
#   * CHEAP — a launcher already running costs one `pgrep` and stops there.
#   * SWITCHABLE — `launcher.session_autostart` in the launcher's Preferences →
#     Startup, default ON. `VCT_DISABLE_LAUNCHER_AUTOSTART=1` is the env kill
#     switch for CI and headless machines (a Linux session with no $DISPLAY is
#     already skipped on its own).
# No launcher binary on the machine is a SILENT success: exit 0, nothing said
# unless VCO_HOOK_DEBUG=1.
# ---------------------------------------------------------------------------
__vco_gui_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-launcher-ensure.$$"
__vco_gui_out="$("$RUN_PY" -m vco_lib.launcher_ensure ensure --shell --repo-root "$REPO_ROOT" 2>"$__vco_gui_err")"
__vco_gui_rc=$?
case "$__vco_gui_rc" in
    0|3|4)
        eval "$__vco_gui_out"
        [ "$__vco_gui_rc" = "0" ] || echo "[vct] ${VCO_LAUNCHER_REASON:-the launcher could not be started}"
        debug "launcher ${VCO_LAUNCHER_STATE:-?}: ${VCO_LAUNCHER_REASON:-}"
        ;;
    *)
        echo "session-start-ensure-hub: vco_lib.launcher_ensure failed (rc=$__vco_gui_rc): $(tail -n 3 "$__vco_gui_err" 2>/dev/null | tr '\n' ' ')"
        ;;
esac
rm -f "$__vco_gui_err"

exit 0
