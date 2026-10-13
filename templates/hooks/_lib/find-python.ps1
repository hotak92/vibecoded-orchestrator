# _lib/find-python.ps1
# Shared helper dot-sourced by .ps1 hooks that need to invoke a Python interpreter.
#
# Sets $script:PY (and the parent scope's $PY when dot-sourced) to the
# orchestrator venv's interpreter when one resolves, else the first
# available of `python`, `py`, `python3`. Default Windows installs register
# `python.exe` and the `py` launcher, not `python3` — that ordering matches
# the expectation on Windows hosts. See VCO portability audit 2026-04-30,
# finding F6.
#
# v0.2.102 NB-H2 (FRR:65): the orchestrator venv is probed FIRST, PATH
# second — before this, a machine whose ONLY interpreter is the VCO venv's
# (a healthy install) reported a broken install. MUST MATCH the .sh sibling;
# the probe is the _lib/resolve-vco-venv.ps1 ladder every other VCO-venv
# consumer uses, PATH is the fallback exactly as before.
# `VCO_FIND_PYTHON_PATH_ONLY=1` SKIPS the venv probe (PATH decides, the
# pre-NB-H2 behaviour) — for harnesses simulating an interpreter-less
# machine and users who deliberately want PATH resolution.
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
$resolveVenvLib = Join-Path $PSScriptRoot "resolve-vco-venv.ps1"
if (-not $env:VCO_FIND_PYTHON_PATH_ONLY -and (Test-Path -LiteralPath $resolveVenvLib -PathType Leaf)) {
    . $resolveVenvLib
    # ScriptDir for the clone-relative tier is the HOOKS dir (one up from
    # _lib), so its 2-up probe lands on the orchestrator root exactly as it
    # does for the hooks themselves.
    $PY = Resolve-VcoVenvPython -ScriptDir (Split-Path -Parent $PSScriptRoot)
}
if (-not $PY) {
    foreach ($candidate in @('python', 'py', 'python3')) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($cmd) {
            $PY = $cmd.Source
            break
        }
    }
}
# Make available to the dot-sourcing scope.
#
# `-Scope Script` (not `-Scope 1`): when this lib is dot-sourced (the only
# supported invocation, see header), its Script scope IS the caller's script
# scope, so `$PY` is visible to the caller exactly as before. `-Scope 1` worked
# only by coincidence of having a parent scope to count back to, and was wrapped
# in `-ErrorAction SilentlyContinue` — which silently swallowed any scope failure
# (e.g. if ever run with `-File`, where scope 1 does not exist). `-Scope Script`
# is valid under BOTH dot-source and `-File`, so a real failure surfaces instead
# of being masked.
Set-Variable -Name PY -Value $PY -Scope Script
