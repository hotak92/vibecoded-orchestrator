# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# PowerShell wrapper for generate_workflow.py (Windows sibling).
# PS 5.1 compatible. Pure-stdlib Python — no venv required.

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = Join-Path $ScriptDir "generate_workflow.py"

if (-not (Test-Path $Target)) {
    Write-Error "generate_workflow.py not found next to this wrapper ($ScriptDir)"
    exit 1
}

$Python = $null
foreach ($cand in @("python", "python3", "py")) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $Python = $cand; break }
}
if (-not $Python) {
    Write-Error "No Python interpreter found on PATH (need python, python3, or py)"
    exit 1
}

& $Python $Target @args
exit $LASTEXITCODE
