# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# PowerShell wrapper for spawn_background_agent.py (Windows sibling).
# PS 5.1 compatible. The script is pure-stdlib Python — no venv required.
#
# Usage: .\spawn_background_agent.ps1 --agent code-graph-updater --files "a.py b.py" [--priority 2]
#        .\spawn_background_agent.ps1 --list

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Target = Join-Path $ScriptDir "spawn_background_agent.py"

if (-not (Test-Path $Target)) {
    Write-Error "spawn_background_agent.py not found next to this wrapper ($ScriptDir)"
    exit 1
}

# Resolve a Python interpreter: python -> python3 -> py launcher.
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
