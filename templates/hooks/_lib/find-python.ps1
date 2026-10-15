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
# v0.2.103 (Windows field) - MUST MATCH the .sh sibling: the PATH fallback
# skips the Microsoft Store app-execution-alias stubs
# (`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe` and friends), which only
# print "Python was not found" (localized) and exit 9009. A WindowsApps
# candidate is kept only if it actually runs Python (a real Store install
# also lives there). On Windows the `py -3` launcher is preferred before
# PATH names and resolved to the real interpreter path it starts. When the
# ONLY candidates are alias stubs, one stderr line says so and $PY stays
# empty. On non-Windows pwsh the order (python, py, python3) is unchanged.
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
# True when $Path is a Microsoft Store app-execution alias location
# (-match is case-insensitive).
function _FindPython-IsStoreAlias([string]$Path) {
    return ($Path -match '[\\/]Microsoft[\\/]WindowsApps[\\/]')
}
$script:_FindPythonAliasSeen = $false
# The first REAL interpreter among the PATH matches of the names given, in
# order; a skipped stub sets $script:_FindPythonAliasSeen.
function _FindPython-OnPath([string[]]$Names) {
    foreach ($name in $Names) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $src = $cmd.Source
            if (-not $src) { continue }
            if (_FindPython-IsStoreAlias $src) {
                $ok = $false
                try {
                    & $src -c "import sys" *> $null
                    $ok = ($LASTEXITCODE -eq 0)
                } catch { $ok = $false }
                if (-not $ok) {
                    $script:_FindPythonAliasSeen = $true
                    continue
                }
            }
            return $src
        }
    }
    return $null
}
$_fpOnWindows = ($PSVersionTable.PSEdition -eq 'Desktop') -or ($IsWindows -eq $true)
if (-not $PY -and $_fpOnWindows) {
    # Windows: the py launcher first, resolved to the interpreter it runs.
    $_fpLauncher = _FindPython-OnPath @('py')
    if ($_fpLauncher) {
        try {
            $_fpExe = & $_fpLauncher -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $_fpExe) {
                $_fpExe = ([string]($_fpExe | Select-Object -First 1)).Trim()
                if ($_fpExe -and (Test-Path -LiteralPath $_fpExe -PathType Leaf)) { $PY = $_fpExe }
            }
        } catch { }
    }
}
if (-not $PY) {
    $PY = _FindPython-OnPath @('python', 'py', 'python3')
}
if (-not $PY -and $script:_FindPythonAliasSeen) {
    [Console]::Error.WriteLine("VCO: no Python interpreter found - only the Microsoft Store 'python' alias stub is on PATH. Install Python (python.org or 'winget install Python.Python.3.12'), or turn the alias off in Settings > Apps > Advanced app settings > App execution aliases.")
}
Remove-Item -Path Function:\_FindPython-IsStoreAlias, Function:\_FindPython-OnPath -ErrorAction SilentlyContinue
Remove-Variable -Name _FindPythonAliasSeen -Scope Script -ErrorAction SilentlyContinue
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
