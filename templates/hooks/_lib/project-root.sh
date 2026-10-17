# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/project-root.sh — the shell answer to "which VCO project is here?".
#
# MUST MATCH vco_lib/project_root.py :: resolve_project_root (the Python SSOT)
# and _lib/project-root.ps1 (the PowerShell sibling). This is a C-rule mirror:
# hooks run under shells that may have no vco_lib, so the rule is restated
# here with identical data, and `tests/test_v02102_w6w_project_root_parity.py`
# executes all three (plus the stdlib mirror in
# templates/scripts/agent-skill-keyword-match.py) over one shared fixture
# table. Change the rule in one place and that test fails until the others move.
#
# The rule (v0.2.102 W6-W):
#   1. CLAUDE_PROJECT_DIR when set and not blank (surrounding whitespace
#      trimmed) — the harness named the workspace; taken as given.
#   2. Else the nearest folder at or above the start dir (default: $PWD) that
#      has `.claude/settings.json` or `.claude/env`. A bare `.claude/` is NOT
#      a marker (a stray `<sub>/.claude/state` must never become a root), and
#      a `.git` is NOT a boundary (projects contain nested repos).
#   3. The walk stops at the user's home and never accepts it: every Claude
#      Code user has `~/.claude/settings.json`, the harness's GLOBAL config.
#      "Home" is EITHER `VCT_USER_HOME_OVERRIDE` (else $HOME) OR the OS home
#      ($HOME, else $USERPROFILE) — the two members vco_lib uses.
#   4. Nothing found -> empty output, return 1. Callers that WRITE skip the
#      write; they never fall back to $PWD (that planted undrainable
#      `<subfolder>/.claude/state/` dirs before this helper existed).
#
# Sourced, never executed. Not a hook; not registered in settings.json.

# Trim leading/trailing whitespace (Python's str.strip for the ASCII set).
_vco_pr_trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# Physical (symlink-resolved) absolute path of an existing dir, like
# Path.resolve(). Empty when it does not exist / cannot be entered.
_vco_pr_phys() {
    [ -n "$1" ] || return 1
    (cd -P -- "$1" 2>/dev/null && pwd -P) || return 1
}

# vco_find_project_root [start] — the walk alone (rungs 2-4).
vco_find_project_root() {
    local start="${1:-$PWD}" dir home_a="" home_b="" override os_home
    dir="$(_vco_pr_phys "$start")" || return 1
    [ -n "$dir" ] || return 1
    override="$(_vco_pr_trim "${VCT_USER_HOME_OVERRIDE:-}")"
    os_home="${HOME:-${USERPROFILE:-}}"
    if [ -n "$override" ]; then
        home_a="$(_vco_pr_phys "$override" || printf '')"
    elif [ -n "$os_home" ]; then
        home_a="$(_vco_pr_phys "$os_home" || printf '')"
    fi
    if [ -n "$os_home" ]; then
        home_b="$(_vco_pr_phys "$os_home" || printf '')"
    fi
    while :; do
        if { [ -n "$home_a" ] && [ "$dir" = "$home_a" ]; } || \
           { [ -n "$home_b" ] && [ "$dir" = "$home_b" ]; }; then
            return 1  # home, and everything above it, is out of bounds
        fi
        if [ -f "$dir/.claude/settings.json" ] || [ -f "$dir/.claude/env" ]; then
            printf '%s' "$dir"
            return 0
        fi
        [ "$dir" = "/" ] && return 1
        dir="$(dirname -- "$dir")"
    done
}

# vco_project_root [start] — the full rule. Prints the root (no newline) and
# returns 0, or prints nothing and returns 1.
vco_project_root() {
    local ws
    ws="$(_vco_pr_trim "${CLAUDE_PROJECT_DIR:-}")"
    if [ -n "$ws" ]; then
        printf '%s' "$ws"
        return 0
    fi
    vco_find_project_root "${1:-$PWD}"
}

# ---------------------------------------------------------------------------
# Path containment (v0.2.104 E-1/E-2; R6 F5/F6). MUST MATCH the Test-VcoPath*
# pair in _lib/project-root.ps1 — the same rule on both sides:
#   * a `.` or `..` segment, or a backslash, in either path → not contained
#     (a lexical `..` could escape; no caller hands such a path in);
#   * both sides are symlink-resolved through their EXISTING part, the
#     missing tail kept as given (a new file in a new directory still
#     resolves), an existing symlinked leaf followed (40 hops at most);
#   * <path> must then sit strictly below <root>: equal is not "under", and
#     a symlink that leaves the root is outside. There is no raw fast path —
#     the resolved comparison is the decision.
#
# _vco_pr_real_path <path> — print the resolved absolute form described
# above, or return 1. No subprocess beyond the `cd -P` subshell.
_vco_pr_real_path() {
    local p="$1" hops=0 dir tail target phys
    [ -n "$p" ] || return 1
    case "$p" in *\\*) return 1 ;; esac
    case "/$p/" in */./*|*/../*) return 1 ;; esac
    case "$p" in /*) ;; *) p="$PWD/$p" ;; esac
    while :; do
        while [ "${p%/}" != "$p" ] && [ "$p" != "/" ]; do p="${p%/}"; done
        dir="$p"
        tail=""
        while [ "$dir" != "/" ] && [ ! -e "$dir" ] && [ ! -L "$dir" ]; do
            tail="/${dir##*/}$tail"
            dir="${dir%/*}"
            [ -n "$dir" ] || dir="/"
        done
        if [ -L "$dir" ] && [ ! -d "$dir" ]; then
            # A symlink that is not a directory (to a file, or dangling):
            # follow it and re-append the tail, as Resolve-VcoPrRealPath does.
            hops=$((hops + 1))
            [ "$hops" -le 40 ] || return 1
            target="$(readlink -- "$dir")" || return 1
            [ -n "$target" ] || return 1
            case "$target" in
                /*) p="$target$tail" ;;
                *) p="${dir%/*}/$target$tail" ;;
            esac
            continue
        fi
        if [ -d "$dir" ]; then
            phys="$(_vco_pr_phys "$dir")" || return 1
        else
            # An existing non-directory: resolve its directory and keep its
            # name (plus any tail, which simply names nothing that exists).
            phys="$(_vco_pr_phys "${dir%/*}/")" || return 1
            tail="/${dir##*/}$tail"
        fi
        [ "$phys" = "/" ] && phys=""
        printf '%s' "$phys$tail"
        return 0
    done
}

# vco_path_under_root <root> <path> — 0 when <path> sits strictly inside
# <root> under the rule above. No subprocess beyond `cd -P`.
vco_path_under_root() {
    local proot pphys
    [ -n "${1:-}" ] && [ -n "${2:-}" ] || return 1
    proot="$(_vco_pr_real_path "$1")" || return 1
    pphys="$(_vco_pr_real_path "$2")" || return 1
    case "$pphys" in
        "$proot"/*) return 0 ;;
    esac
    return 1
}

# vco_path_in_project <root> <path> — 0 when <path> belongs to the project:
# under <root>, or under one of the project's ENABLED extra code folders as
# listed in <root>/.claude/state/codegraph_extra_paths_cache.txt (one path
# per line; the cache _lib/route-touched-path.sh maintains). A missing cache
# means "no extras". Read-only — this never refreshes the cache.
vco_path_in_project() {
    local root="$1" p="$2" cache line
    vco_path_under_root "$root" "$p" && return 0
    cache="$root/.claude/state/codegraph_extra_paths_cache.txt"
    [ -f "$cache" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line="${line%$'\r'}"
        [ -n "$line" ] || continue
        vco_path_under_root "$line" "$p" && return 0
    done < "$cache"
    return 1
}
