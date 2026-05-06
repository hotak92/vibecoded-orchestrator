# shellcheck shell=bash
# _lib/find-python.sh
# Shared helper sourced by hooks that need to invoke a Python interpreter.
#
# Sets $PY to the first available of `python3`, `python`, `py`. Windows
# (Git Bash, MSYS) typically only has `python.exe` and `py.exe` — `python3`
# is unset there. macOS Homebrew installs as `python3`. Linux distros vary.
# See VCO portability audit 2026-04-30, finding F6.
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

PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
export PY
