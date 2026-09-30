# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/ssrf-allowlist.ps1 - the pre-tool-use SSRF guard's caller of
# `python -m vco_lib.ssrf_url`, the ONE home of the WebFetch URL decision.
# POSIX sibling: _lib/ssrf-allowlist.sh (same contract; see its header and
# vco_lib/ssrf_url.py's docstring).
#
# v0.2.100 review R18F-04: this file used to hold a ~400-line URL parser that
# mirrored the bash one. It drifted where the shared case table could not see
# (R18F-02: Windows PowerShell 5.1 drops the double quotes inside a native
# command's argument, so the IDNA helper it spawned never ran and every
# non-ASCII host was blocked on Windows only). The decision is now Python;
# this file only finds the interpreter, hands it the URL and passes back its
# answer.
#
# The URL is passed as the HEX of its UTF-8 bytes (`--url-hex`): an argument
# of [0-9a-f] only survives every PowerShell version's native-argument
# quoting, and no console / pipe encoding can alter it. It never reaches code.
#
# Interpreter: the VCO venv (_lib/resolve-vco-venv.ps1), else the hooks' $PY
# (_lib/find-python.ps1). When the install root is known ($env:VCT_INSTALL_ROOT
# holding vco_lib/), it goes first on PYTHONPATH for this one call.
#
# Usage:
#     . (Join-Path $LibDir "ssrf-allowlist.ps1")
#     $r = Invoke-VcoSsrfCheck -Url $url -HooksDir $ScriptDir
#     $r.Verdict  allow | block | pass - the module's line 1. Anything else
#                 (empty when Python or vco_lib is missing) MUST be treated
#                 as block by the caller.
#     $r.Pairs    after `block`: the allowed host:port pairs (line 2)
#     $r.Error    why the module gave no answer (empty when it did)

# The interpreter that runs vco_lib.ssrf_url ("" when none is found).
function Get-VcoSsrfPython {
    param([string]$HooksDir)
    if (-not (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue)) {
        $venvLib = Join-Path $HooksDir "_lib/resolve-vco-venv.ps1"
        if (Test-Path -LiteralPath $venvLib) { . $venvLib }
    }
    $py = ""
    if (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue) {
        $py = Resolve-VcoVenvPython -ScriptDir $HooksDir
    }
    if (-not $py) {
        if (-not (Get-Variable -Name PY -ErrorAction SilentlyContinue) -or -not $PY) {
            $findPy = Join-Path $HooksDir "_lib/find-python.ps1"
            if (Test-Path -LiteralPath $findPy) { . $findPy }
        }
        if ((Get-Variable -Name PY -ErrorAction SilentlyContinue) -and $PY) { $py = [string]$PY }
    }
    if ($null -eq $py) { return "" }
    return [string]$py
}

# Run the module once; returns @{ Verdict; Pairs; Error }.
function Invoke-VcoSsrfCheck {
    param([string]$Url, [string]$HooksDir)
    $r = @{ Verdict = ""; Pairs = ""; Error = "" }
    $py = Get-VcoSsrfPython -HooksDir $HooksDir
    if (-not $py) {
        $r.Error = "no Python interpreter was found"
        return $r
    }
    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes([string]$Url)
    $hex = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    $prev = $env:PYTHONPATH
    $root = $env:VCT_INSTALL_ROOT
    if ($root -and (Test-Path -LiteralPath (Join-Path $root "vco_lib/__init__.py") -PathType Leaf)) {
        if ($prev) {
            $env:PYTHONPATH = "$root$([System.IO.Path]::PathSeparator)$prev"
        } else {
            $env:PYTHONPATH = $root
        }
    }
    $errFile = $null
    try {
        $errFile = [System.IO.Path]::GetTempFileName()
        $out = @(& $py -m vco_lib.ssrf_url verdict --url-hex $hex 2>$errFile)
        $rc = $LASTEXITCODE
        if ($rc -eq 0 -and $out.Count -ge 1 -and $out[0]) {
            $r.Verdict = ([string]$out[0]).Trim()
            if ($out.Count -ge 2) { $r.Pairs = ([string]$out[1]).Trim() }
        } else {
            $tail = @(Get-Content -LiteralPath $errFile -ErrorAction SilentlyContinue | Select-Object -Last 3) -join ' '
            if ($tail) { $r.Error = $tail } else { $r.Error = "$py -m vco_lib.ssrf_url gave no answer (exit $rc)" }
        }
    } catch {
        $r.Error = "$py -m vco_lib.ssrf_url could not be started: $($_.Exception.Message)"
    } finally {
        $env:PYTHONPATH = $prev
        if ($errFile) { Remove-Item -LiteralPath $errFile -Force -ErrorAction SilentlyContinue }
    }
    return $r
}
