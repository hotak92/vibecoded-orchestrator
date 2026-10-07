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
if [ -z "$PY" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
fi
export PY
unset _FIND_PYTHON_LIB_DIR
