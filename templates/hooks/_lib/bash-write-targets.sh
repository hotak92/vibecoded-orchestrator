# shellcheck shell=bash
# bash-write-targets.sh — THE shell-side home for "which files did / will
# this Bash command write?" (v0.2.95, lane F10).
#
# Two hooks need the same answer for different reasons:
#   * post-bash-file-sync.sh — AFTER the command, to route what was written
#     through _lib/route-touched-path.sh (KG / docs / code-graph), so a CLI
#     write syncs like an Edit/Write;
#   * pre-bash-context-inject.sh — BEFORE the command, to build the
#     retrieval query from the TARGET PATH (module name + extension +
#     --anchor) the way pre-edit-context-inject.sh does, instead of from
#     the raw first 500 chars of the command text.
#
# So the prefilter and the parser invocation live here once. The parse
# itself is vco_lib/bash_write_targets.py — Python, because it is the same
# decision on both operating systems and a mirrored shell implementation
# would drift (A>B>C: shared code beats shared config beats a mirror).
#
# Contract:
#   . _lib/bash-write-targets.sh
#   vco_bash_write_init "<hooks_dir>" "<fallback_python>"
#   vco_bash_write_prefilter "$COMMAND" || <skip: nothing was written>
#   vco_bash_write_targets  "$COMMAND" "<project_root>" ["<scan_state>"]
#   vco_bash_write_prebash  "$COMMAND" "<project_root>"

# --- Idempotent double-source guard ---------------------------------------
if [ -n "${_VCO_BASH_WRITE_TARGETS_SOURCED:-}" ]; then
    return 0 2>/dev/null || true
fi
_VCO_BASH_WRITE_TARGETS_SOURCED=1

_VCO_BWT_HOOKS=""
_VCO_BWT_PY=""

# vco_bash_write_init <hooks_dir> <fallback_python>
#
# The parser needs `import vco_lib`, so it runs on the VCO venv. When no
# VCO venv resolves we fall back to the caller's interpreter with
# PYTHONPATH pointing at the project root — that works inside the
# orchestrator clone and soft-fails (empty stdout) anywhere else. A broken
# install must never break the user's Bash.
vco_bash_write_init() {
    _VCO_BWT_HOOKS="${1:-}"
    _VCO_BWT_PY="${2:-}"
    if [ -f "$_VCO_BWT_HOOKS/_lib/resolve-vco-venv.sh" ]; then
        # shellcheck source=resolve-vco-venv.sh disable=SC1091
        . "$_VCO_BWT_HOOKS/_lib/resolve-vco-venv.sh"
        resolve_vco_venv_python "$_VCO_BWT_HOOKS"
        if [ -n "${VCO_VENV_PYTHON:-}" ]; then
            _VCO_BWT_PY="$VCO_VENV_PYTHON"
        fi
    fi
    # Explicit: a caller under `set -e` aborts if this function's LAST
    # command happens to be a false test (an `&&` list that did not run its
    # right-hand side returns 1, and a bare function call is not exempt).
    return 0
}

# vco_bash_write_prefilter <command>
#
# Pure bash, no subprocess: this runs on EVERY Bash tool call and must cost
# ~nothing. Exit 0 when the command plausibly wrote a file.
#
# The `/dev/null` sinks and fd duplications are stripped FIRST because
# `2>&1` and `>/dev/null` appear in a large fraction of all commands and
# would otherwise wake Python on nearly every call.
#
# A BARE `knowledge/` / `docs/` MENTION IS NOT A WRITE (v0.2.95 review
# MAJOR-3). Until that review this function returned 0 for ANY command
# containing either string — before it even looked for a redirect — so
# `cat docs/x.md`, `grep -rn foo knowledge/` and `ls docs/` each started a
# Python interpreter, ran the mtime scan and re-routed every knowledge/docs
# file touched in the last 300 s, including files the Edit tool had already
# synced seconds earlier. The directory trigger now requires a companion:
# one of the opaque writers below, whose target genuinely cannot be read out
# of the command text (`python -c "open('knowledge/x.md','w')"`,
# `patch -p1 < x.diff`, `git checkout -- docs/x.md`). Anything that shows
# its write in the text — a redirect, a heredoc, a write verb — is caught by
# the two blocks above and never needed the directory trigger at all.
#
# `install(1)` is deliberately absent: `npm install` / `pip install` are
# far more common than install(1) as a file copier, and an install(1) that
# targets knowledge/ or docs/ (or uses a redirect) still reaches the parser
# through the other triggers.
#
# This gate is deliberately NARROWER than the Python side's
# `should_fallback_scan`: a bare `python build.py` (interpreter, no redirect,
# no directory named) could write anything, but waking Python on every such
# command to find out costs more than the miss. The shared library's contract
# is the broader one; this is one caller's affordable slice of it.
#
# MUST MATCH _lib/bash-write-targets.ps1's Test-VcoWriteSuspicious — the
# two operating systems must agree about which commands are worth parsing.
# `tests/test_v0295_bash_write_sync.py` runs both against one corpus.
vco_bash_write_prefilter() {
    local c="${1:-}"
    [ -n "$c" ] || return 1
    local s="$c"
    s="${s//2>&1/}"
    s="${s//1>&2/}"
    s="${s//>&2/}"
    s="${s//&>\/dev\/null/}"
    s="${s//2>\/dev\/null/}"
    s="${s//1>\/dev\/null/}"
    s="${s//>\/dev\/null/}"
    s="${s//> \/dev\/null/}"
    case "$s" in
        *'>'*|*'<<'*) return 0 ;;
    esac
    # Shell separators become spaces so a verb that opens a pipeline stage or
    # follows a `;`/`&&` is still word-matched — the same boundary the Python
    # side spells `(?:^|[\s;&|(])`.
    local t=" ${s//[;&|()]/ } "
    case "$t" in
        *' tee '*|*' cp '*|*' mv '*|*' touch '*|*' dd '*) return 0 ;;
        *'sed -i'*|*'sed --in-place'*|*'perl -i'*|*'ruby -i'*) return 0 ;;
        *'--output'*|*'--outfile'*|*'--out-file'*|*'--output-file'*) return 0 ;;
        *'Set-Content'*|*'Add-Content'*|*'Out-File'*|*'Tee-Object'*) return 0 ;;
    esac
    # Past this point the command shows no write of its own. It is worth
    # parsing only if it NAMES a scanned directory and could have written
    # into it opaquely.
    case "$c" in
        *knowledge/*|*docs/*) ;;
        *) return 1 ;;
    esac
    case "$t" in
        *' python '*|*' python3 '*|*' perl '*|*' ruby '*) return 0 ;;
        *' node '*|*' php '*|*' Rscript '*) return 0 ;;
        *' patch '*|*' rsync '*) return 0 ;;
        *' git checkout '*|*' git restore '*|*' git apply '*) return 0 ;;
        *' git stash '*|*' git reset '*) return 0 ;;
    esac
    return 1
}

# vco_bash_write_targets <command> <project_root> [scan_state]
#
# One absolute path per line, for paths that EXIST (the command has already
# run, so a candidate that isn't on disk was a parse artefact). Passing a
# scan_state path enables the bounded knowledge/ + docs/ mtime fallback for
# writes the command text cannot express — see the Python module's
# docstring for the bound and the cost.
vco_bash_write_targets() {
    local cmd="${1:-}" root="${2:-}" scan_state="${3:-}"
    [ -n "$cmd" ] && [ -n "$root" ] && [ -n "$_VCO_BWT_PY" ] || return 0
    local args=( -m vco_lib.bash_write_targets --project-root "$root" --require-exists )
    [ -n "$scan_state" ] && args+=( --scan-state "$scan_state" )
    printf '%s' "$cmd" | \
        PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
        "$_VCO_BWT_PY" "${args[@]}" 2>/dev/null || true
}

# vco_bash_write_prebash <command> <project_root>
#
# Two lines: the write TARGET the query should be built from (empty when
# none is recoverable) and a content SNIPPET (empty unless the command
# carries a heredoc body destined for knowledge/ or docs/ — see the Python
# module for why other bodies are withheld). Existence is NOT required: the
# file is about to be created.
vco_bash_write_prebash() {
    local cmd="${1:-}" root="${2:-}"
    [ -n "$cmd" ] && [ -n "$root" ] && [ -n "$_VCO_BWT_PY" ] || return 0
    printf '%s' "$cmd" | \
        PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" \
        "$_VCO_BWT_PY" -m vco_lib.bash_write_targets \
            --project-root "$root" --format prebash 2>/dev/null || true
}
