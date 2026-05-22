# _lib/find-python.ps1
# Shared helper dot-sourced by .ps1 hooks that need to invoke a Python interpreter.
#
# Sets $script:PY (and the parent scope's $PY when dot-sourced) to the first
# available of `python`, `py`, `python3`. Default Windows installs register
# `python.exe` and the `py` launcher, not `python3` — that ordering matches
# the expectation on Windows hosts. See VCO portability audit 2026-04-30,
# finding F6.
#
# Usage (from any .ps1 hook):
#     $LibDir = Join-Path $PSScriptRoot "_lib"
#     . (Join-Path $LibDir "find-python.ps1")
#     if (-not $PY) { exit 0 }   # silent no-op if no python found
#     & $PY my-script.py ...
#
# This file is dot-sourced, never executed. It's a library, not a hook —
# it is NOT registered in settings.json.windows.template.

$PY = $null
foreach ($candidate in @('python', 'py', 'python3')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        $PY = $cmd.Source
        break
    }
}
# Make available to the dot-sourcing scope.
Set-Variable -Name PY -Value $PY -Scope 1 -ErrorAction SilentlyContinue
