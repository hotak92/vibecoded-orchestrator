# shellcheck shell=bash
# _lib/resolve-vco-venv.sh
# Shared helper sourced by hooks that need a Python interpreter capable
# of importing VCO's own packages (weaviate, weaviate_mcp, vco_lib, …).
#
# v0.2.46 post-adversarial follow-up — eliminates venv-resolver drift
# across 9 hooks (5 .sh + 4 .ps1). Before this helper, several hooks
# fell back to `$PROJECT_ROOT/.venv` when `$VCT_INSTALL_ROOT` was unset,
# which would activate the USER's project venv — which doesn't have
# weaviate-client + the rest of VCO's deps, producing confusing
# `ImportError: No module named 'weaviate'` failures.
#
# The script-level resolvers (templates/scripts/kg-*, code-graph-*)
# already implement the correct discipline; this helper applies the
# same discipline to hooks.
#
# Canonical precedence (must match the inline order in
# templates/scripts/kg-search and friends):
#
#   1. $VCT_VENV/bin/python (POSIX) or $VCT_VENV/Scripts/python.exe
#      (Windows) — explicit override the user / launcher sets.
#   2. $VCT_INSTALL_ROOT/.venv          ← launcher-provided (CANONICAL)
#   3. $VCT_INSTALL_ROOT/claude_mcp_servers/.venv  ← legacy launcher path
#   4. <SCRIPT_DIR>/../../.venv         ← orchestrator-clone fallback
#                                         (only when this is NOT the
#                                         user's project root)
#   5. <SCRIPT_DIR>/../../claude_mcp_servers/.venv  ← legacy clone path
#
# Tier 4-5 are gated by `_is_user_project_venv` — if `SCRIPT_DIR/../..`
# resolves to something containing a `CLAUDE.md` + `.claude/` but no
# `install.py`, that's the user's project, NOT a VCO clone, and we
# refuse to activate.
#
# NEVER falls back to `$PROJECT_ROOT/.venv` directly. If
# $VCT_INSTALL_ROOT is unset AND no clone-relative fallback resolves,
# we leave $VCO_VENV_PYTHON empty and the caller is expected to log
# a clear "VCO venv not resolvable" diagnostic + exit cleanly.
#
# Usage from a hook:
#
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     # shellcheck source=_lib/resolve-vco-venv.sh
#     . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
#     resolve_vco_venv_python "$SCRIPT_DIR"
#     if [ -z "$VCO_VENV_PYTHON" ]; then
#         echo "[hook-name] VCO venv not resolvable; skipping" >&2
#         exit 0
#     fi
#     "$VCO_VENV_PYTHON" my-script.py ...
#
# This file is sourced, never executed, so it has no shebang. It is a
# library, not a hook — it is NOT registered in settings.json.template.

# Returns 0 (success) when $1/.venv exists AND $1 looks like a VCO
# orchestrator clone (has both install.py + first-install.sh, matching
# `validate_source_repo` in install.py:160). Returns 1 otherwise.
_is_vco_orchestrator_clone() {
    local candidate="$1"
    [ -d "$candidate" ] || return 1
    [ -f "$candidate/install.py" ] || return 1
    [ -f "$candidate/first-install.sh" ] || return 1
    return 0
}

# Probe a single venv-dir candidate. Sets VCO_VENV_PYTHON to the python
# interpreter inside it on success. Empty string on failure.
_probe_venv_python() {
    local venv_dir="$1"
    [ -d "$venv_dir" ] || return 1
    for candidate in \
        "$venv_dir/bin/python" \
        "$venv_dir/bin/python3" \
        "$venv_dir/Scripts/python.exe"; do
        if [ -x "$candidate" ]; then
            VCO_VENV_PYTHON="$candidate"
            return 0
        fi
    done
    return 1
}

# Resolve VCO's Python interpreter. Sets VCO_VENV_PYTHON to the
# resolved path (empty string if no tier hits).
#
# Args:
#   $1 - the hook's SCRIPT_DIR (`templates/hooks/` at install time,
#        `<project>/.claude/hooks/` at runtime). Used for the clone-
#        relative tier-4/5 probe.
resolve_vco_venv_python() {
    local script_dir="${1:-}"
    VCO_VENV_PYTHON=""

    # Tier 1: $VCT_VENV explicit override.
    if [ -n "${VCT_VENV:-}" ]; then
        for candidate in \
            "$VCT_VENV/bin/python" \
            "$VCT_VENV/bin/python3" \
            "$VCT_VENV/Scripts/python.exe" \
            "$VCT_VENV"; do
            if [ -x "$candidate" ]; then
                VCO_VENV_PYTHON="$candidate"
                return 0
            fi
        done
    fi

    # Tier 2 + 3: $VCT_INSTALL_ROOT (canonical).
    if [ -n "${VCT_INSTALL_ROOT:-}" ]; then
        if _probe_venv_python "$VCT_INSTALL_ROOT/.venv"; then return 0; fi
        if _probe_venv_python "$VCT_INSTALL_ROOT/claude_mcp_servers/.venv"; then return 0; fi
    fi

    # Tier 4 + 5: clone-relative, gated by VCO-clone discriminator.
    # Only try when script_dir was provided AND the resolved 2-up path
    # has install.py + first-install.sh (= it's a real VCO clone, not
    # the user's project that just happens to have a .venv).
    if [ -n "$script_dir" ]; then
        local clone_root
        clone_root="$(cd "$script_dir/../.." 2>/dev/null && pwd)" || clone_root=""
        if [ -n "$clone_root" ] && _is_vco_orchestrator_clone "$clone_root"; then
            if _probe_venv_python "$clone_root/.venv"; then return 0; fi
            if _probe_venv_python "$clone_root/claude_mcp_servers/.venv"; then return 0; fi
        fi
    fi

    # All tiers failed. Caller MUST check $VCO_VENV_PYTHON for empty
    # before using and decide whether to soft-fail (typical) or hard-
    # fail (rare). NEVER fall back to $PROJECT_ROOT/.venv.
    return 1
}
