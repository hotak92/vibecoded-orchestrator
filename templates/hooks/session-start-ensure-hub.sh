#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# SessionStart hook: ensure vct-hub is running (Step 9, v0.2.21).
#
# Idempotent: invokes `vct-hub --start-if-not-running` (Step 5's CLI),
# which returns 0 whether the hub started fresh OR was already running.
# Soft-fail throughout — never blocks Claude Code startup. Worst case:
# a single stderr line + exit 0.
#
# Binary-discovery order (v0.2.63: install-folder copy preferred over PATH —
# must match the launcher's hub_launcher::find_hub_binary):
#   1. $VCT_HUB_BIN        — explicit override (dev builds, custom installs)
#   2. <repo_root>/launcher/dist/<arch>/vct-hub  (INSTALL-FOLDER copy)
#      then <repo_root>/launcher/dist/vct-hub    (arch-less fallback)
#   3. PATH                — first `vct-hub` on PATH (install.py adds it)
#   4. $HOME/.vct/bin/vct-hub
# If none match: emit one stderr line, exit 0.
#
# Env overrides:
#   $VCT_HUB_BIN          — explicit binary path (highest precedence).
#   $VCT_DISABLE_HOOKS    — set to non-empty to bypass entirely.
#   $VCO_HOOK_DEBUG=1     — verbose stderr (which path won, exit code).

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
# Staleness check without a JSON parser: the launcher rewrites the
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
# detect_arch :: emit a directory name matching launcher/dist/<arch>/.
# Best-effort; falls back to empty string if uname is unavailable.
#
# v0.2.54 Track C: normalize machine names to the canonical dist-slot
# tokens — `uname -m` says `x86_64`/`amd64` but the dist dirs are
# `linux-x64` / `macos-x64`, and `aarch64` (Linux) maps to `arm64`.
# Pre-v0.2.54 this emitted `linux-x86_64` / `macos-x86_64`, which never
# matched a real dist dir (step-4 discovery silently dead on x86_64).
# ---------------------------------------------------------------------------
detect_arch() {
    local os arch
    os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m 2>/dev/null)"
    case "$arch" in
        x86_64|amd64) arch="x64" ;;
        aarch64)      arch="arm64" ;;
    esac
    case "$os" in
        linux)  printf 'linux-%s\n'  "$arch" ;;
        darwin) printf 'macos-%s\n'  "$arch" ;;
        *)      printf '\n' ;;
    esac
}

# ---------------------------------------------------------------------------
# find_hub_binary :: print absolute path of vct-hub, or empty on failure.
# Follows the discovery chain documented above. Never fails the hook.
# ---------------------------------------------------------------------------
find_hub_binary() {
    # 1. Explicit override.
    if [ -n "${VCT_HUB_BIN:-}" ]; then
        if [ -x "$VCT_HUB_BIN" ]; then
            debug "found via VCT_HUB_BIN: $VCT_HUB_BIN"
            printf '%s\n' "$VCT_HUB_BIN"
            return 0
        fi
        debug "VCT_HUB_BIN set but not executable: $VCT_HUB_BIN — falling through"
    fi

    # 2. INSTALL-FOLDER copy (v0.2.63): the repo's own dist hub, arch-qualified
    #    subdir then arch-less fallback. PREFERRED over PATH/`~/.vct/bin` so a
    #    stale `vct-hub` on PATH (a leftover dev build, an old global install)
    #    never wins over the copy install.py deployed for THIS project. Must
    #    match the launcher's `find_hub_binary` order (hub_launcher.rs, v0.2.63).
    local arch
    arch="$(detect_arch)"
    if [ -n "$arch" ]; then
        local arch_dist="$REPO_ROOT/launcher/dist/$arch/vct-hub"
        if [ -x "$arch_dist" ]; then
            debug "found at in-tree arch dist: $arch_dist"
            printf '%s\n' "$arch_dist"
            return 0
        fi
    fi
    local flat_dist="$REPO_ROOT/launcher/dist/vct-hub"
    if [ -x "$flat_dist" ]; then
        debug "found at in-tree flat dist: $flat_dist"
        printf '%s\n' "$flat_dist"
        return 0
    fi

    # 3. PATH.
    local on_path
    on_path="$(command -v vct-hub 2>/dev/null || true)"
    if [ -n "$on_path" ] && [ -x "$on_path" ]; then
        debug "found on PATH: $on_path"
        printf '%s\n' "$on_path"
        return 0
    fi

    # 4. Known user-install location.
    local user_install="${HOME:-}/.vct/bin/vct-hub"
    if [ -n "${HOME:-}" ] && [ -x "$user_install" ]; then
        debug "found at user install: $user_install"
        printf '%s\n' "$user_install"
        return 0
    fi

    return 1
}

HUB_BIN="$(find_hub_binary || true)"

if [ -z "$HUB_BIN" ]; then
    echo "[vct] vct-hub not found on PATH; skipping auto-start (set VCT_HUB_BIN to override)" >&2
    exit 0
fi

# Idempotent invocation. `--start-if-not-running` exits 0 whether the hub
# started fresh, was already running, or could not start (in which case
# the hub writes its own diagnostic to stderr).
#
# We deliberately background-detach via `nohup` so that a slow first-time
# start (e.g. cold container probe inside the hub) cannot block the
# SessionStart hook bus past its 10s budget. Output is dropped on the
# floor under normal operation; --verbose users can set VCO_HOOK_DEBUG=1
# AND tail the hub's own log file (see docs/HUB_DETACHMENT_v0.2.21.md).
if [ "${VCO_HOOK_DEBUG:-}" = "1" ]; then
    "$HUB_BIN" --start-if-not-running
    rc=$?
    debug "vct-hub --start-if-not-running exit=$rc"
else
    # Detach so we never block. The hub itself short-circuits when already
    # running, so the cost of the spawn is bounded.
    nohup "$HUB_BIN" --start-if-not-running >/dev/null 2>&1 &
    disown 2>/dev/null || true
fi

exit 0
