# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/ssrf-allowlist.sh — the pre-tool-use SSRF guard's caller of
# `python -m vco_lib.ssrf_url`, the ONE home of the WebFetch URL decision
# (which URLs are this machine's own VCO services, which target a private /
# internal address, which are public). Windows sibling: _lib/ssrf-allowlist.ps1.
#
# v0.2.100 review R18F-04: this file used to hold a ~450-line URL parser,
# mirrored line for line in the .ps1 — and the two drifted in ways their
# shared case table could not see. The parse, the normalisation, the IDNA
# conversion, the allowed services and the block rules now live in
# vco_lib/ssrf_url.py (its docstring is the contract); this file only finds
# the interpreter, hands it the URL and passes back its answer.
#
# Interpreter: the VCO venv (_lib/resolve-vco-venv.sh), else the hooks' $PY
# (_lib/find-python.sh). No `-I`: like every other hook's `-m vco_lib.*` call
# the import must honour the install's own paths. When the install root is
# known ($VCT_INSTALL_ROOT holding vco_lib/), it goes first on PYTHONPATH so
# the hooks judge with the vco_lib of the install they belong to.
# The URL goes in on STDIN, never through an argument or into code.
#
# Usage:
#     . "$SCRIPT_DIR/_lib/ssrf-allowlist.sh"
#     vco_ssrf_run "$URL" "$SCRIPT_DIR"     # call directly, never in $(...)
#     $_vco_ssrf_verdict  allow | block | pass — the module's line 1. Anything
#                         else (empty when Python or vco_lib is missing) MUST
#                         be treated as block by the caller.
#     $_vco_ssrf_pairs    after `block`: the allowed host:port pairs (line 2)
#     $_vco_ssrf_err      why the module gave no answer (empty when it did)

_vco_ssrf_verdict=""
_vco_ssrf_pairs=""
_vco_ssrf_err=""

# vco_ssrf_python HOOKS_DIR :: the interpreter that runs vco_lib.ssrf_url
# (empty when none is found).
vco_ssrf_python() {
    local hooks_dir="${1:-}"
    if ! command -v resolve_vco_venv_python >/dev/null 2>&1 \
        && [[ -f "$hooks_dir/_lib/resolve-vco-venv.sh" ]]; then
        # shellcheck source=resolve-vco-venv.sh disable=SC1091
        . "$hooks_dir/_lib/resolve-vco-venv.sh"
    fi
    VCO_VENV_PYTHON=""
    if command -v resolve_vco_venv_python >/dev/null 2>&1; then
        resolve_vco_venv_python "$hooks_dir"
    fi
    printf '%s' "${VCO_VENV_PYTHON:-${PY:-}}"
}

# vco_ssrf_run URL HOOKS_DIR :: run the module once; sets the three globals.
vco_ssrf_run() {
    local url="$1" hooks_dir="${2:-}" py pypath errfile out rc=0
    _vco_ssrf_verdict="" _vco_ssrf_pairs="" _vco_ssrf_err=""
    py="$(vco_ssrf_python "$hooks_dir")"
    if [[ -z "$py" ]]; then
        _vco_ssrf_err="no Python interpreter was found"
        return 0
    fi
    pypath="${PYTHONPATH:-}"
    if [[ -n "${VCT_INSTALL_ROOT:-}" && -f "$VCT_INSTALL_ROOT/vco_lib/__init__.py" ]]; then
        pypath="$VCT_INSTALL_ROOT${pypath:+:$pypath}"
    fi
    errfile="$(mktemp 2>/dev/null)" || errfile=""
    out="$(printf '%s' "$url" | PYTHONPATH="$pypath" "$py" -m vco_lib.ssrf_url verdict \
        2>"${errfile:-/dev/null}")" || rc=$?
    if [[ $rc -eq 0 && -n "$out" ]]; then
        _vco_ssrf_verdict="${out%%$'\n'*}"
        [[ "$out" == *$'\n'* ]] && _vco_ssrf_pairs="${out#*$'\n'}"
    elif [[ -n "$errfile" ]]; then
        _vco_ssrf_err="$(tail -n 3 "$errfile" 2>/dev/null | tr '\n' ' ')"
    fi
    [[ -n "$_vco_ssrf_verdict" || -n "$_vco_ssrf_err" ]] || _vco_ssrf_err="$py -m vco_lib.ssrf_url gave no answer (exit $rc)"
    [[ -n "$errfile" ]] && rm -f "$errfile"
    return 0
}
