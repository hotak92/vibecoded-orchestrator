# shellcheck shell=bash
# _lib/find-python.sh
# Shared helper sourced by hooks that need to invoke a Python interpreter.
#
# Sets $PY to the orchestrator venv's interpreter when one resolves, else
# the first available of `python3`, `python`, `py` on PATH. Windows
# (Git Bash, MSYS) typically only has `python.exe` and `py.exe` — `python3`
# is unset there. macOS Homebrew installs as `python3`. Linux distros vary.
# See VCO portability audit 2026-04-30, finding F6.
#
# v0.2.102 NB-H2 (FRR:65): the orchestrator venv is probed FIRST, PATH
# second. Before this, "no Python on this machine" was decided from PATH
# alone, so a machine whose ONLY interpreter is the VCO venv's — a healthy
# install — reported a broken install (pre-tool-use's no-Python arm blocked
# WebFetch and skipped the security scans for no reason). The probe is the
# _lib/resolve-vco-venv.sh ladder every other VCO-venv consumer uses (pure
# file tests, no interpreter spawn); when the lib is absent (partial
# install) or no tier hits, the PATH probe below decides, exactly as before.
#
# `VCO_FIND_PYTHON_PATH_ONLY=1` SKIPS the venv probe (PATH decides, the
# pre-NB-H2 behaviour). For harnesses that simulate a machine with no
# interpreter at all — the venv ladder's clone-relative tier cannot be
# defeated by unsetting env vars when the checkout under test carries its
# own .venv (CI does exactly that) — and for any user who deliberately
# wants PATH resolution.
#
# v0.2.103 (Windows field): the PATH fallback skips the Microsoft Store
# app-execution-alias stubs (`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`
# and friends). With no Python installed that stub is FIRST on PATH, and it
# only prints "Python was not found" (localized) and exits 9009 — so a hook
# took it for a real interpreter and failed on every call. A WindowsApps
# candidate is kept only if it actually runs Python (a real Store install
# also lives there); otherwise the next PATH match is tried. On Windows
# (Git Bash / MSYS / Cygwin) the `py -3` launcher is preferred before PATH
# names and resolved to the real interpreter path it starts. When the ONLY
# candidates found are alias stubs, one stderr line says so and $PY stays
# empty (callers keep their own no-Python handling). POSIX behaviour is
# unchanged: no PATH entry there is a WindowsApps alias, and the order
# stays python3, python, py.
#
# Usage (from any hook):
#     SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#     # shellcheck source=_lib/find-python.sh
#     . "$SCRIPT_DIR/_lib/find-python.sh"
#     [ -n "$PY" ] || exit 0   # silent no-op if no python found
#     "$PY" my-script.py ...
#
# This file is sourced, never executed, so it has no shebang. It is a
# library, not a hook — it is NOT registered in settings.json.template.

_FIND_PYTHON_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=""
if [ -z "${VCO_FIND_PYTHON_PATH_ONLY:-}" ] \
    && [ -f "$_FIND_PYTHON_LIB_DIR/resolve-vco-venv.sh" ]; then
    # shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
    . "$_FIND_PYTHON_LIB_DIR/resolve-vco-venv.sh"
    # SCRIPT_DIR for the clone-relative tier is the HOOKS dir (one up from
    # _lib), so its 2-up probe lands on the orchestrator root exactly as it
    # does for the hooks themselves.
    resolve_vco_venv_python "$(cd "$_FIND_PYTHON_LIB_DIR/.." && pwd)"
    PY="$VCO_VENV_PYTHON"
fi
# True when $1 is a Microsoft Store app-execution alias location.
_find_python_is_store_alias() {
    case "$1" in
        *[Mm]icrosoft[/\\][Ww]indows[Aa]pps[/\\]*) return 0 ;;
    esac
    return 1
}
# Set _FIND_PYTHON_FOUND to the first REAL interpreter among the PATH
# matches of the names given, in order (empty when none); set
# _FIND_PYTHON_ALIAS_SEEN=1 when a stub was skipped. Called directly, never
# in $(...), so both variables reach this scope.
_find_python_on_path() {
    local _fp_name _fp_cand
    _FIND_PYTHON_FOUND=""
    for _fp_name in "$@"; do
        while IFS= read -r _fp_cand; do
            [ -n "$_fp_cand" ] || continue
            if _find_python_is_store_alias "$_fp_cand"; then
                if ! "$_fp_cand" -c "import sys" </dev/null >/dev/null 2>&1; then
                    _FIND_PYTHON_ALIAS_SEEN=1
                    continue
                fi
            fi
            _FIND_PYTHON_FOUND="$_fp_cand"
            return 0
        done <<EOF
$(type -ap "$_fp_name" 2>/dev/null)
EOF
    done
    return 1
}
_FIND_PYTHON_ALIAS_SEEN=""
_FIND_PYTHON_FOUND=""
if [ -z "$PY" ]; then
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*)
            # Windows: the py launcher first, resolved to the interpreter it runs.
            _find_python_on_path py || true
            _fp_launcher="$_FIND_PYTHON_FOUND"
            if [ -n "$_fp_launcher" ]; then
                _fp_exe="$("$_fp_launcher" -3 -c "import sys; print(sys.executable)" </dev/null 2>/dev/null | tr -d '\r' || true)"
                if [ -n "$_fp_exe" ] && command -v cygpath >/dev/null 2>&1; then
                    _fp_exe="$(cygpath -u "$_fp_exe" 2>/dev/null || printf '%s' "$_fp_exe")"
                fi
                if [ -n "$_fp_exe" ] && [ -f "$_fp_exe" ]; then
                    PY="$_fp_exe"
                fi
            fi
            unset _fp_launcher _fp_exe
            ;;
    esac
fi
if [ -z "$PY" ]; then
    _find_python_on_path python3 python py || true
    PY="$_FIND_PYTHON_FOUND"
fi
if [ -z "$PY" ] && [ -n "$_FIND_PYTHON_ALIAS_SEEN" ]; then
    printf '%s\n' "VCO: no Python interpreter found - only the Microsoft Store 'python' alias stub is on PATH. Install Python (python.org or 'winget install Python.Python.3.12'), or turn the alias off in Settings > Apps > Advanced app settings > App execution aliases." >&2
fi
export PY
unset _FIND_PYTHON_LIB_DIR _FIND_PYTHON_ALIAS_SEEN _FIND_PYTHON_FOUND
unset -f _find_python_is_store_alias _find_python_on_path
