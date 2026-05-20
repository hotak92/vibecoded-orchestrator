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
# Binary-discovery order:
#   1. $VCT_HUB_BIN        — explicit override (dev builds, custom installs)
#   2. PATH                — first `vct-hub` on PATH (install.py adds it)
#   3. $HOME/.vct/bin/vct-hub
#   4. <orchestrator_root>/launcher/dist/<arch>/vct-hub  (in-tree dev)
#   5. <orchestrator_root>/launcher/dist/vct-hub         (arch-less fallback)
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
# detect_arch :: emit a directory name matching launcher/dist/<arch>/.
# Best-effort; falls back to empty string if uname is unavailable.
# ---------------------------------------------------------------------------
detect_arch() {
    local os arch
    os="$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]')"
    arch="$(uname -m 2>/dev/null)"
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

    # 2. PATH.
    local on_path
    on_path="$(command -v vct-hub 2>/dev/null || true)"
    if [ -n "$on_path" ] && [ -x "$on_path" ]; then
        debug "found on PATH: $on_path"
        printf '%s\n' "$on_path"
        return 0
    fi

    # 3. Known user-install location.
    local user_install="${HOME:-}/.vct/bin/vct-hub"
    if [ -n "${HOME:-}" ] && [ -x "$user_install" ]; then
        debug "found at user install: $user_install"
        printf '%s\n' "$user_install"
        return 0
    fi

    # 4. In-tree dev build (arch-qualified subdir).
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

    # 5. In-tree dev build (arch-less fallback).
    local flat_dist="$REPO_ROOT/launcher/dist/vct-hub"
    if [ -x "$flat_dist" ]; then
        debug "found at in-tree flat dist: $flat_dist"
        printf '%s\n' "$flat_dist"
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
