# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# shellcheck shell=bash
# vct_venv_ladder.sh — ONE home for the dependency-gated orchestrator-venv
# ladder the KG wrappers use. Sourced, never executed (no shebang).
#
# WHY THIS FILE (v0.2.94): the ladder was INLINED in `kg-sync` and `kg-dedup`,
# and `kg-duplicates` had NO ladder at all — it sourced `$PROJECT_ROOT/.venv`
# and ran `python`, so on a user project it reached whatever interpreter was
# first on PATH. That divergence is a cross-OS bug generator: the `.ps1`
# siblings carried the hardened ladder while their bash siblings did not, and
# `kg-duplicates.ps1` gated on a WEAKER module list than its script needs.
# A third inline copy would have made it four. The ladder lives here now;
# each wrapper declares only what is genuinely ITS own: the tool name, the
# import probe it requires, its exit code, and its refusal tail.
#
# Ships as a top-level `templates/scripts/*.sh`, so `bundle_globs.script_patterns()`
# already copies it into `.claude/scripts/` beside the wrappers that source it
# — no installer change, and no wrapper can be shipped without its ladder.
#
# PARITY: `vct_venv_ladder.ps1` is the Windows-native sibling and must stay in
# lockstep — same tiers, same order, same refusal text, same module gating.
#
# Usage:
#
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     . "$SCRIPT_DIR/vct_venv_ladder.sh"
#     vct_venv_ladder_resolve "$SCRIPT_DIR" "import weaviate, vco_lib"
#     if [ -z "$LADDER_PYTHON" ]; then
#         vct_venv_ladder_refusal "kg-duplicates" "import weaviate, vco_lib" "$SCRIPT_DIR"
#         { echo "kg-duplicates: <tool-specific tail>"; } >&2
#         exit 1
#     fi
#     "$LADDER_PYTHON" ...
#
# The IMPORT PROBE is passed verbatim as the code the candidate interpreter
# must run, so each wrapper names its real requirement in the exact form it is
# executed — a wrapper cannot claim one dependency and gate on another.
#
# THE PROBE RULE (v0.2.94 review item 7): a wrapper's probe names what its
# script needs to reach a CORRECT VERDICT — not merely what it needs to start.
# That is broader than "module-scope imports" in both directions:
#
#   * a module-scope import inside a `try:` that PRINTS AND CONTINUES still
#     belongs in the probe (`weaviate` in `query_code_graph.py`): without it
#     the tool runs and reports "no matches", which is a wrong answer, not a
#     failure;
#   * a FUNCTION-LOCAL import belongs in the probe when its absence changes the
#     ANSWER rather than the speed (`vco_lib` in `search_knowledge.py` decides
#     which named vector is queried; in `detect_duplicates.py` the scan cannot
#     run at all without it);
#   * an import whose absence changes only what is LOGGED does not belong
#     (`weaviate_mcp.query_logger` telemetry in the two read-only KG tools).
#
# The earlier wording — "what the script hard-imports at module scope" — was
# narrower than what the wrappers actually gate on, so it read as false the
# moment a reader compared it against `kg-duplicates`.
#
# Outputs (globals, set by `vct_venv_ladder_resolve`):
#   LADDER_PYTHON      — the interpreter, or "" when no candidate qualified
#   LADDER_CANDIDATES  — array of probed venv DIRECTORIES, in probe order
#   LADDER_CLONE_ROOT  — the 2-up path, for the refusal's "not a clone" line

# Validate a venv by RUNNING the probe in it. Windows shapes
# (`Scripts/python.exe`) are included so the wrappers work under Git Bash /
# WSL2 (v0.2.49 Bug K).
_vct_ladder_interp_for_candidate() {
    local c="$1"
    # INTERPRETER NAMES, IN ORDER — this list and its order are pinned across
    # all four ladders (this file, `vct_venv_ladder.ps1`, `vco_lib/python_exe.py`,
    # `vct-launcher-core/src/python_resolve.rs`) by
    # tests/test_v0294_python_exe_parity.py. `Scripts/python3.exe` was
    # here and in NO other ladder: a name only one of four sides probes is a
    # machine where the four disagree, so it was dropped rather than added to
    # the other three (a venv always provides `Scripts/python.exe`).
    if [ -x "$c/bin/python" ]; then printf '%s' "$c/bin/python"; return 0; fi
    if [ -x "$c/bin/python3" ]; then printf '%s' "$c/bin/python3"; return 0; fi
    # Windows (Git Bash / WSL2) shape.
    if [ -x "$c/Scripts/python.exe" ]; then printf '%s' "$c/Scripts/python.exe"; return 0; fi
    # $VCT_VENV given as the INTERPRETER itself rather than a venv dir — the
    # RT-4 tier `code-graph-analyze` shipped and `resolve-vco-venv.sh` tier 1
    # honours. `-f` before `-x` is load-bearing: a directory satisfies `-x`
    # (that is the search bit), and "resolving" to a directory produces an
    # exit-126 spawn whose message says nothing about the misconfiguration.
    if [ -f "$c" ] && [ -x "$c" ]; then printf '%s' "$c"; return 0; fi
    return 1
}

_vct_ladder_venv_has_deps() {
    local v="$1"
    local probe="$2"
    local py=""
    py="$(_vct_ladder_interp_for_candidate "$v")" || return 1
    [ -n "$py" ] || return 1
    "$py" -c "$probe" 2>/dev/null
}

# v0.2.92 (W3 §4bis): read `VCT_ORCHESTRATOR_ROOT` out of the project's own
# `.claude/env`. This is the DURABLE tier — the one that works in a plain
# terminal, in CI, and from any cron: it needs no env inheritance at all,
# because the value is file-backed and written by the canonical env projection
# on every install and update.
#
# It exists because the field failure had no env at all: a user project with
# its own dep-less `.venv`, run from a shell with no `VCT_INSTALL_ROOT`, fell
# through every candidate to a bare `python3` and crashed inside the script
# with `ModuleNotFoundError`. An env-only ladder cannot help there.
_vct_ladder_orchestrator_root_from_project_env() {
    local env_file="$1/../env"
    [ -r "$env_file" ] || return 1
    # First assignment wins, `export ` prefix tolerated, one quote pair
    # stripped — the same line rule `vco_lib/envfile.py::parse_env_lines`
    # applies, so the file has one meaning on both sides.
    local line
    line="$(grep -m1 -E '^[[:space:]]*(export[[:space:]]+)?VCT_ORCHESTRATOR_ROOT=' \
            "$env_file" 2>/dev/null)" || return 1
    [ -n "$line" ] || return 1
    line="${line#*=}"
    line="${line%\"}"; line="${line#\"}"
    line="${line%\'}"; line="${line#\'}"
    [ -n "$line" ] || return 1
    printf '%s' "$line"
}

# Is $1 a real VCO orchestrator clone (not the user's project that merely
# happens to own a `.venv`)? Same discriminator as
# `templates/hooks/_lib/resolve-vco-venv.sh::_is_vco_orchestrator_clone` and
# as `install.py::validate_source_repo`. Without it, `$SCRIPT_DIR/../../.venv`
# IS the user's project venv whenever a wrapper is bundled into a project.
_vct_ladder_is_vco_orchestrator_clone() {
    [ -d "$1" ] || return 1
    [ -f "$1/install.py" ] || return 1
    [ -f "$1/first-install.sh" ] || return 1
    return 0
}

# Resolve the interpreter. Candidates, canonical first: $VCT_VENV explicit
# override, then the launcher-provided install root, then the file-backed
# orchestrator root, then clone-relative paths — the last GATED so a user
# project's venv can never be selected.
#
# Args: $1 = the wrapper's SCRIPT_DIR · $2 = the import probe to run.
vct_venv_ladder_resolve() {
    local script_dir="$1"
    local probe="$2"

    LADDER_PYTHON=""
    LADDER_CLONE_ROOT="$(cd "$script_dir/../.." 2>/dev/null && pwd || true)"

    local project_env_root
    project_env_root="$(_vct_ladder_orchestrator_root_from_project_env "$script_dir" || true)"

    # $VCT_ORCHESTRATOR_ROOT from the ENVIRONMENT (v0.2.94 review item 2a).
    # `vco_lib/python_exe.py` honours both install-root env vars and this side
    # honoured only the file-backed one, so a shell with a valid exported
    # orchestrator root and no `$VCT_INSTALL_ROOT` resolved differently from
    # the Python half of the same ladder.
    #
    # VALIDATED, unlike the file-backed tier: an exported value survives a
    # moved/deleted clone (the file-backed one is rewritten by the install that
    # owns the project), so it is accepted only when the path still LOOKS like
    # an orchestrator clone — the same discriminator the clone-relative tier
    # uses, which is this shell's expression of
    # `vco_lib.paths.looks_like_orchestrator_root`.
    local env_orch_root="${VCT_ORCHESTRATOR_ROOT:-}"
    if [ -n "$env_orch_root" ] && \
       ! _vct_ladder_is_vco_orchestrator_clone "$env_orch_root"; then
        env_orch_root=""
    fi
    # Same value from both channels ⇒ probe it once.
    [ -n "$env_orch_root" ] && [ "$env_orch_root" = "$project_env_root" ] && env_orch_root=""

    # The tier ORDER is the sequence of appends below, and nothing else may
    # live between the array's creation and the probe loop — the parity gate
    # (`test_v0292_wp17_move_delivery::TestParityClaims`) reads exactly this
    # region and compares it against the PowerShell sibling's.
    LADDER_CANDIDATES=()
    [ -n "${VCT_VENV:-}" ] && LADDER_CANDIDATES+=("$VCT_VENV")
    # The `${VCT_INSTALL_ROOT:-}` form is kept verbatim (rather than the bare
    # `$VCT_INSTALL_ROOT` the guard already makes safe) because the Gap-6b
    # canonical-first ordering gate pins this exact literal.
    [ -n "${VCT_INSTALL_ROOT:-}" ] && LADDER_CANDIDATES+=(
        "${VCT_INSTALL_ROOT:-}/.venv"
        "${VCT_INSTALL_ROOT:-}/claude_mcp_servers/.venv"
    )
    [ -n "$env_orch_root" ] && LADDER_CANDIDATES+=(
        "$env_orch_root/.venv"
        "$env_orch_root/claude_mcp_servers/.venv"
    )
    [ -n "$project_env_root" ] && LADDER_CANDIDATES+=(
        "$project_env_root/.venv"
        "$project_env_root/claude_mcp_servers/.venv"
    )
    if [ -n "$LADDER_CLONE_ROOT" ] && \
       _vct_ladder_is_vco_orchestrator_clone "$LADDER_CLONE_ROOT"; then
        LADDER_CANDIDATES+=(
            "$LADDER_CLONE_ROOT/.venv"
            "$LADDER_CLONE_ROOT/claude_mcp_servers/.venv"
        )
    fi

    local venv_path=""
    local c
    for c in "${LADDER_CANDIDATES[@]}"; do
        [ -z "$c" ] && continue
        if _vct_ladder_venv_has_deps "$c" "$probe"; then
            venv_path="$c"
            break
        fi
    done

    # v0.2.49 Bug K: invoke the venv's python binary directly rather than
    # `source bin/activate`. The source-then-python pattern was POSIX-only —
    # Windows venvs (incl. those used under Git Bash / WSL2) have no
    # `bin/activate`. Direct invocation works on every OS, and exporting
    # VIRTUAL_ENV keeps spawned subprocesses informed that a venv is active
    # (some libraries probe $VIRTUAL_ENV directly).
    if [ -n "$venv_path" ]; then
        LADDER_PYTHON="$(_vct_ladder_interp_for_candidate "$venv_path")"
        # VIRTUAL_ENV names the venv ROOT, not the interpreter. When the
        # candidate WAS the interpreter (the RT-4 `$VCT_VENV=/…/bin/python`
        # tier), the root is two directories up — both layouts
        # (`<root>/bin/python`, `<root>\Scripts\python.exe`) place the binary
        # exactly one directory inside the root, so the same two hops recover
        # it. Same rule as the .ps1 sibling.
        if [ "$LADDER_PYTHON" = "$venv_path" ]; then
            export VIRTUAL_ENV="$(cd "$(dirname "$venv_path")/.." 2>/dev/null && pwd || true)"
        else
            export VIRTUAL_ENV="$venv_path"
        fi
    fi
}

# Print the SHARED half of the refusal to stderr: what qualifies, what was
# probed, and the remedies. The caller appends its own tool-specific tail and
# chooses the exit code — a shipped component never falls back to a bare
# interpreter (standing rule), so every caller must exit non-zero after this.
#
# Args: $1 = tool name · $2 = the import probe · $3 = the wrapper's SCRIPT_DIR.
vct_venv_ladder_refusal() {
    local tool="$1"
    local probe="$2"
    local script_dir="$3"

    # "import weaviate, vco_lib" → ['weaviate', 'vco_lib']. The prose names
    # exactly what the probe executes; deriving it from the probe is what
    # keeps the message from drifting off the gate. One, two and three-plus
    # module gates all ship (kg-search needs `weaviate` alone; kg-migrate
    # needs three), so the sentence adapts rather than each wrapper owning
    # its own copy of it.
    local modules="${probe#import }"
    local -a mods=()
    local part
    local old_ifs="$IFS"
    IFS=','
    for part in $modules; do
        part="${part# }"; part="${part% }"
        [ -n "$part" ] && mods+=("$part")
    done
    IFS="$old_ifs"

    local last_index=$(( ${#mods[@]} - 1 ))
    local qualifies_head=""
    if [ "${#mods[@]}" -le 1 ]; then
        qualifies_head="A candidate qualifies only when '${mods[0]:-}' imports from it."
    elif [ "${#mods[@]}" -eq 2 ]; then
        qualifies_head="A candidate qualifies only when BOTH '${mods[0]}' and"
    else
        local joined="'${mods[0]}'"
        local i
        for (( i = 1; i < last_index; i++ )); do joined="$joined, '${mods[$i]}'"; done
        qualifies_head="A candidate qualifies only when ALL of $joined and"
    fi

    {
        echo "$tool: ERROR - no Python environment with VCO's KG dependencies."
        echo "$tool: $qualifies_head"
        if [ "${#mods[@]}" -le 1 ]; then
            echo "$tool: Probed, in order:"
        else
            echo "$tool: '${mods[$last_index]}' import from it. Probed, in order:"
        fi
        if [ "${#LADDER_CANDIDATES[@]}" -eq 0 ]; then
            echo "$tool:   (none - no VCT_VENV, no VCT_INSTALL_ROOT, no"
            echo "$tool:    VCT_ORCHESTRATOR_ROOT in $script_dir/../env, and"
            echo "$tool:    $LADDER_CLONE_ROOT is not a VCO orchestrator clone)"
        else
            local c
            for c in "${LADDER_CANDIDATES[@]}"; do echo "$tool:   - $c"; done
        fi
        echo "$tool: Fix by any ONE of:"
        echo "$tool:   * run this from a launcher-managed session (it exports"
        echo "$tool:     VCT_INSTALL_ROOT);"
        echo "$tool:   * export VCT_VENV=/path/to/orchestrator/.venv;"
        echo "$tool:   * export VCT_INSTALL_ROOT=/path/to/orchestrator;"
        echo "$tool:   * export VCT_ORCHESTRATOR_ROOT=/path/to/orchestrator;"
        echo "$tool:   * re-run the orchestrator install so this project's"
        echo "$tool:     .claude/env carries VCT_ORCHESTRATOR_ROOT."
    } >&2
}

# The LAST line of every refusal: one ⚠️-prefixed sentence, after the
# tool-specific tail (v0.2.94 review item 3).
#
# WHY THE EMOJI IS LOAD-BEARING: the every-10-edits duplicate scan in
# `post-file-edit.sh` pipes the wrapper's whole output through
# `grep -E "(✅|⚠️|📊|❌)"` and writes a report only when something matches.
# A refusal carries none of those markers, so the scan was a SILENT no-op on
# exactly the installs where it had something to say. Every consumer that keys
# on the markers now sees the refusal; the hook ALSO surfaces `^<tool>: ERROR`
# lines, so neither side depends on the other being remembered.
#
# Args: $1 = tool name · $2 = the exit code the caller is about to use.
vct_venv_ladder_refusal_summary() {
    echo "⚠️  $1 did NOT run (exit $2): no Python environment with its dependencies. See the lines above." >&2
}
