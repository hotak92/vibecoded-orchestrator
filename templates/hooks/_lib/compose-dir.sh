# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/compose-dir.sh — WHICH compose directory the container hooks may run
# compose from, and the refusal when it is not the orchestrator's own.
# Windows mirror: _lib/compose-dir.ps1 (MUST stay logically identical).
#
# Resolution order (the first candidate that is a directory):
#   1. $VCT_COMPOSE_DIR                         — explicit override
#   2. $VCT_INFRASTRUCTURE_DIR                  — the orchestrator's infrastructure/
#   3. $VCT_ORCHESTRATOR_ROOT/infrastructure
#   4. <repo_root>/infrastructure
#   5. <repo_root>/claude_mcp_servers           — the orchestrator clone's legacy home
#
# v0.2.100 (review L1-F17, owner ruling Q3): the directory is used ONLY when
# its PARENT is the orchestrator clone — it carries `vct-module.json` whose
# "id" is "orchestrator". A project used to receive a copy of the compose
# files in <project>/infrastructure/, without the orchestrator's .env data
# knobs, override or build context, and a hook there silently fell back to
# it: compose then created containers on EMPTY default volumes. Now such a
# directory is REFUSED with a message naming VCT_ORCHESTRATOR_ROOT, and
# nothing is composed from it.
#
# Usage:
#     . "$SCRIPT_DIR/_lib/compose-dir.sh"
#     vco_resolve_compose_dir "$REPO_ROOT"
#     # → COMPOSE_DIR (usable dir or "") and COMPOSE_DIR_REFUSAL ("" or the line to print)

# vco_is_orchestrator_root DIR :: 0 when DIR/vct-module.json names id "orchestrator".
vco_is_orchestrator_root() {
    local manifest="$1/vct-module.json"
    [ -f "$manifest" ] && grep -Eq '"id"[[:space:]]*:[[:space:]]*"orchestrator"' "$manifest"
}

# vco_resolve_compose_dir REPO_ROOT :: sets COMPOSE_DIR and COMPOSE_DIR_REFUSAL.
# Returns 0 (usable), 1 (no candidate directory), 2 (refused).
vco_resolve_compose_dir() {
    local repo_root="$1" candidate found="" parent
    COMPOSE_DIR=""
    COMPOSE_DIR_REFUSAL=""
    for candidate in "${VCT_COMPOSE_DIR:-}" "${VCT_INFRASTRUCTURE_DIR:-}" \
            "${VCT_ORCHESTRATOR_ROOT:+$VCT_ORCHESTRATOR_ROOT/infrastructure}" \
            "$repo_root/infrastructure" "$repo_root/claude_mcp_servers"; do
        if [ -n "$candidate" ] && [ -d "$candidate" ]; then
            found="$candidate"
            break
        fi
    done
    [ -n "$found" ] || return 1
    parent="$(cd "$found/.." 2>/dev/null && pwd)"
    if [ -n "$parent" ] && vco_is_orchestrator_root "$parent"; then
        COMPOSE_DIR="$found"
        return 0
    fi
    COMPOSE_DIR_REFUSAL="refusing to run compose from $found: it is not the VCO orchestrator's own infrastructure/ (no vct-module.json with id \"orchestrator\" beside it), so compose could create containers on EMPTY default volumes. Set VCT_ORCHESTRATOR_ROOT to your VCO clone in .claude/env; nothing was created."
    return 2
}
