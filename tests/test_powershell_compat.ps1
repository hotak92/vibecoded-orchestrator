# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# tests/test_powershell_compat.ps1 — W-P1-4 (v0.2.53 Track H).
#
# Regression test for the PowerShell 5.1 / 7+ compatibility of the
# template scripts `vct_project_config.ps1` and `vct_access_check.ps1`.
# Pre-fix, both scripts used the `-SkipHttpErrorCheck` flag on
# Invoke-WebRequest. That flag is PS 7+ only — on stock Windows
# (PowerShell 5.1, which ships with every Win7+ install), both scripts
# parse-failed before the body even ran. The access-matrix gate
# silently degraded to "fail-open via empty-stdout caller fallback",
# leaving Windows users with NO policy enforcement until they manually
# `winget install Microsoft.PowerShell`.
#
# The fix wraps Invoke-WebRequest in a try/catch chain that handles
# both PS 5.1 (`System.Net.WebException` thrown on 4xx/5xx, with the
# response readable via $_.Exception.Response) and PS 7+
# (`Microsoft.PowerShell.Commands.HttpResponseException`).
#
# Test strategy:
#   1. Both scripts must be valid PowerShell — parse them with the
#      PowerShell tokenizer ([System.Management.Automation.Language.Parser]).
#      Pre-fix the `-SkipHttpErrorCheck` flag was a parameter ERROR,
#      not a parse error, but its presence indicates incompatibility.
#   2. Both scripts must NOT contain `-SkipHttpErrorCheck` after the
#      W-P1-4 fix (literal-string grep on the file content).
#   3. Both scripts must contain a catch block for both
#      `System.Net.WebException` AND
#      `Microsoft.PowerShell.Commands.HttpResponseException`.
#   4. Both scripts must contain a body-reading idiom (StreamReader OR
#      ErrorDetails.Message) — sanity check the catch arms aren't empty.
#
# Runner: any PowerShell (5.1 or 7+). On Linux dev machines with pwsh
# installed, this runs natively. On Windows CI, runs in installer-smoke.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$failures = @()

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$scripts = @(
    (Join-Path $repoRoot 'templates/scripts/vct_project_config.ps1'),
    (Join-Path $repoRoot 'templates/scripts/vct_access_check.ps1')
)

foreach ($scriptPath in $scripts) {
    if (-not (Test-Path -LiteralPath $scriptPath)) {
        $failures += "MISSING: $scriptPath"
        continue
    }

    $content = Get-Content -LiteralPath $scriptPath -Raw

    # Assertion 1: script must parse cleanly.
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseInput(
        $content, [ref]$tokens, [ref]$errors
    ) | Out-Null
    if ($errors -and $errors.Count -gt 0) {
        foreach ($e in $errors) {
            $failures += "PARSE ERROR in $($scriptPath): $($e.Message)"
        }
    }

    # Assertion 2: -SkipHttpErrorCheck must not appear OUTSIDE comments.
    # We strip comment-only lines (lines whose first non-whitespace char
    # is `#`) before checking — explanatory comments mentioning the flag
    # are fine, but a literal `-SkipHttpErrorCheck` argument on a code
    # line is the regression we are guarding against.
    $codeLines = $content -split "`r?`n" |
        Where-Object { $_ -notmatch '^\s*#' }
    $codeOnly = $codeLines -join "`n"
    if ($codeOnly -match '-SkipHttpErrorCheck') {
        $failures += "REGRESSION: $scriptPath still uses -SkipHttpErrorCheck on a code line (PS 5.1-incompatible)"
    }

    # Assertion 3: catch arms for both exception types.
    if ($content -notmatch '\[System\.Net\.WebException\]') {
        $failures += "MISSING WebException catch in $scriptPath (PS 5.1 path)"
    }
    if ($content -notmatch '\[Microsoft\.PowerShell\.Commands\.HttpResponseException\]') {
        $failures += "MISSING HttpResponseException catch in $scriptPath (PS 7+ path)"
    }

    # Assertion 4: body-reading idiom present in the catch chain.
    # Allow either StreamReader (PS 5.1 path) or ErrorDetails.Message
    # (PS 7 path); both must appear at least once.
    if ($content -notmatch 'StreamReader') {
        $failures += "MISSING StreamReader body-reading idiom in $scriptPath"
    }
    if ($content -notmatch 'ErrorDetails\.Message') {
        $failures += "MISSING ErrorDetails.Message body-reading idiom in $scriptPath"
    }
}

if ($failures.Count -gt 0) {
    Write-Host "FAIL: $($failures.Count) assertion(s) failed:" -ForegroundColor Red
    foreach ($f in $failures) {
        Write-Host "  - $f" -ForegroundColor Red
    }
    exit 1
}

Write-Host "OK: PS 5.1/7 compat assertions passed for vct_project_config.ps1 and vct_access_check.ps1" -ForegroundColor Green
exit 0
