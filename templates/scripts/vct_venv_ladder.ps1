# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# PowerShell sibling of `vct_venv_ladder.sh` - the ONE home for the
# dependency-gated orchestrator-venv ladder the KG wrappers use.
# Dot-sourced, never executed directly.
#
# WHY THIS FILE (v0.2.94): the ladder was copied into `kg-sync.ps1` and
# `kg-dedup.ps1`, `kg-duplicates.ps1` carried a WEAKER module gate than its
# script needs, and the bash `kg-duplicates` had no ladder at all. Four
# near-copies of one decision across two OSes is exactly how a cross-OS
# divergence survives. Each wrapper now declares only what is genuinely ITS
# own: tool name, import probe, exit code, refusal tail.
#
# Ships as a top-level `templates/scripts/*.ps1`, so
# `bundle_globs.script_patterns()` already copies it into `.claude\scripts\`
# beside the wrappers that dot-source it.
#
# PARITY: `vct_venv_ladder.sh` is the POSIX sibling and must stay in lockstep
# - same tiers, same order, same refusal text, same module gating.
#
# Usage:
#
#     $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
#     . (Join-Path $ScriptDir "vct_venv_ladder.ps1")
#     $VenvPython = Resolve-VctLadderPython -ScriptDir $ScriptDir `
#         -ProjectRoot $ProjectRoot -ImportProbe "import weaviate, vco_lib"
#     if (-not $VenvPython) {
#         Write-VctLadderRefusal -Tool "kg-duplicates" -ImportProbe $probe `
#             -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
#         [Console]::Error.WriteLine("kg-duplicates: <tool-specific tail>")
#         exit 1
#     }
#
# The IMPORT PROBE is passed verbatim as the code the candidate interpreter
# must run, so a wrapper cannot claim one dependency and gate on another.

# Validate a candidate interpreter by RUNNING the probe in it.
#
# The probe is EXPECTED to fail for unqualified candidates - that is the whole
# point - so it must not be allowed to abort the ladder. Every wrapper sets
# `$ErrorActionPreference = "Stop"`, and on Windows PowerShell 5.1 a native
# command that writes to stderr under `Stop` raises `NativeCommandError`: the
# FIRST deps-less candidate would terminate the script instead of moving to the
# next rung, and the user would see a PowerShell exception rather than the
# refusal that names every tier. The preference is therefore restored to
# `Continue` for the duration of the probe (function-scoped: PowerShell
# restores the caller's value on return) and the exit code is read explicitly.
function Test-VctLadderVenvHasDeps {
    param([string]$PythonExe, [string]$ImportProbe)
    if (-not (Test-Path $PythonExe)) { return $false }
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try {
        & $PythonExe -c $ImportProbe 2>$null | Out-Null
    } catch {
        # A candidate that cannot even be launched is simply not a candidate.
        return $false
    }
    return ($LASTEXITCODE -eq 0)
}

# v0.2.92 (W3 section 4bis): read `VCT_ORCHESTRATOR_ROOT` out of the project's
# own `.claude\env` - the DURABLE tier that needs no env inheritance at all,
# because the value is file-backed and written by the canonical env projection
# on every install and update.
# PARITY: same line rule as `vco_lib/envfile.py::parse_env_lines` and the bash
# sibling - first assignment wins, `export ` prefix tolerated, one quote pair
# stripped.
function Get-VctLadderOrchestratorRootFromProjectEnv {
    param([string]$ScriptDir)
    $envFile = Join-Path $ScriptDir "..\env"
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in (Get-Content -LiteralPath $envFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*(export\s+)?VCT_ORCHESTRATOR_ROOT\s*=\s*(.*)$') {
            $val = $Matches[2].Trim()
            if ($val.Length -ge 2 -and (
                    ($val.StartsWith('"') -and $val.EndsWith('"')) -or
                    ($val.StartsWith("'") -and $val.EndsWith("'")))) {
                $val = $val.Substring(1, $val.Length - 2)
            }
            if ($val) { return $val }
            return $null
        }
    }
    return $null
}

# Is this a real VCO orchestrator clone (not the user's project that merely
# happens to own a `.venv`)? Same discriminator as
# `templates/hooks/_lib/resolve-vco-venv.ps1` and `install.py::validate_source_repo`.
function Test-VctLadderVcoClone {
    param([string]$Candidate)
    if (-not $Candidate) { return $false }
    if (-not (Test-Path $Candidate)) { return $false }
    if (-not (Test-Path (Join-Path $Candidate "install.py"))) { return $false }
    if (-not (Test-Path (Join-Path $Candidate "first-install.sh"))) { return $false }
    return $true
}

# LAYOUTS and INTERPRETER NAMES, both IN ORDER. The two lists and their order
# are pinned across all four ladders (this file, `vct_venv_ladder.sh`,
# `vco_lib/python_exe.py`, `vct-launcher-core/src/python_resolve.rs`) by
# tests/test_v0294_python_exe_parity.py.
#
# v0.2.94: this list used to be layout-major with the names in the order
# `Scripts\python.exe`, `bin\python` and NO `bin\python3` — a different answer
# from the other three ladders on any venv carrying both shapes (an MSYS /
# Git-Bash venv on Windows has `bin/`, not `Scripts/`).
function Get-VctLadderVenvPythonCandidates {
    param([string]$Root)
    return @(
        (Join-Path $Root ".venv\bin\python"),
        (Join-Path $Root ".venv\bin\python3"),
        (Join-Path $Root ".venv\Scripts\python.exe"),
        (Join-Path $Root "claude_mcp_servers\.venv\bin\python"),
        (Join-Path $Root "claude_mcp_servers\.venv\bin\python3"),
        (Join-Path $Root "claude_mcp_servers\.venv\Scripts\python.exe")
    )
}

# Candidate interpreter locations, canonical-first: explicit override, then
# the launcher-provided install root ($env:VCT_INSTALL_ROOT), then the
# file-backed orchestrator root, then clone-relative paths - the last GATED so
# a user project's venv can never be selected.
function Get-VctLadderCandidates {
    param([string]$ScriptDir, [string]$ProjectRoot)
    $projectEnvRoot = Get-VctLadderOrchestratorRootFromProjectEnv -ScriptDir $ScriptDir

    # $VCT_ORCHESTRATOR_ROOT from the ENVIRONMENT (v0.2.94 review item 2a),
    # resolved and VALIDATED here so the candidate region below holds nothing
    # but the tier appends, in tier order - which is what the cross-flavour
    # order gate reads. Unlike the file-backed tier, an exported value survives
    # a moved/deleted clone, so it is accepted only when the path still looks
    # like an orchestrator clone. PARITY: same tier, same position, same
    # validation as the bash sibling.
    $envOrchRoot = $env:VCT_ORCHESTRATOR_ROOT
    if ($envOrchRoot -and -not (Test-VctLadderVcoClone $envOrchRoot)) { $envOrchRoot = $null }
    if ($envOrchRoot -and ($envOrchRoot -eq $projectEnvRoot)) { $envOrchRoot = $null }

    $candidates = @()
    if ($env:VCT_VENV) {
        $candidates += @(
            (Join-Path $env:VCT_VENV "bin\python"),
            (Join-Path $env:VCT_VENV "bin\python3"),
            (Join-Path $env:VCT_VENV "Scripts\python.exe")
        )
        # $VCT_VENV given as the INTERPRETER itself rather than a venv dir -
        # the RT-4 tier `code-graph-analyze` shipped and
        # `resolve-vco-venv.ps1` tier 1 honours. `-PathType Leaf` is
        # load-bearing: a DIRECTORY would otherwise "resolve" and every spawn
        # built on it dies with a message about the wrong thing.
        if (Test-Path -LiteralPath $env:VCT_VENV -PathType Leaf) {
            $candidates += $env:VCT_VENV
        }
    }
    if ($env:VCT_INSTALL_ROOT) {
        $candidates += Get-VctLadderVenvPythonCandidates $env:VCT_INSTALL_ROOT
    }
    if ($envOrchRoot) {
        $candidates += Get-VctLadderVenvPythonCandidates $envOrchRoot
    }
    if ($projectEnvRoot) {
        $candidates += Get-VctLadderVenvPythonCandidates $projectEnvRoot
    }
    if (Test-VctLadderVcoClone $ProjectRoot) {
        $candidates += Get-VctLadderVenvPythonCandidates $ProjectRoot
    }
    return ,$candidates
}

# Resolve the interpreter, or $null. Publishes the probed list as
# $script:VctLadderCandidates so the refusal can name every one of them.
#
# On success it also exports VIRTUAL_ENV, like the bash sibling
# (`export VIRTUAL_ENV="$venv_path"`): invoking the venv python binary
# directly activates the interpreter, but subprocess libraries that probe
# $VIRTUAL_ENV need it set to the venv ROOT. Both venv layouts
# (`<root>\Scripts\python.exe`, `<root>/bin/python`) place the binary exactly
# one directory inside the root, so two Split-Path -Parent hops recover the
# root in either shape. Pre-v0.2.94 only kg-sync.ps1 did this - kg-dedup.ps1
# did not, which is one more way two copies of one decision drift.
function Resolve-VctLadderPython {
    param([string]$ScriptDir, [string]$ProjectRoot, [string]$ImportProbe)
    $script:VctLadderCandidates = Get-VctLadderCandidates -ScriptDir $ScriptDir -ProjectRoot $ProjectRoot
    foreach ($cand in $script:VctLadderCandidates) {
        if ($cand -and (Test-Path $cand) -and (Test-VctLadderVenvHasDeps $cand $ImportProbe)) {
            $venvBinDir = Split-Path -Parent $cand
            $env:VIRTUAL_ENV = Split-Path -Parent $venvBinDir
            return $cand
        }
    }
    return $null
}

# Print the SHARED half of the refusal. STDERR, not Write-Host: the host
# stream is invisible to 2>nul / CI capture / the launcher's error scraping,
# which made a refused run indistinguishable from a silent success
# (v0.2.92 delivery audit m8). The caller appends its tool-specific tail and
# chooses the exit code - a shipped component never falls back to a bare
# interpreter (standing rule), so every caller must exit non-zero after this.
function Write-VctLadderRefusal {
    param([string]$Tool, [string]$ImportProbe, [string]$ScriptDir, [string]$ProjectRoot)
    # "import weaviate, vco_lib" -> 'weaviate' + 'vco_lib'. Deriving the prose
    # from the probe is what keeps the message from drifting off the gate.
    # One, two and three-plus module gates all ship (kg-search needs
    # `weaviate` alone; kg-migrate needs three), so the sentence adapts.
    # PARITY: same three shapes as the bash sibling.
    $modules = @(($ImportProbe -replace '^import\s+', '') -split ',' |
        ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $last = $modules.Count - 1
    if ($modules.Count -le 1) {
        $head = "A candidate qualifies only when '$($modules[0])' imports from it."
    } elseif ($modules.Count -eq 2) {
        $head = "A candidate qualifies only when BOTH '$($modules[0])' and"
    } else {
        $joined = ($modules[0..($last - 1)] | ForEach-Object { "'$_'" }) -join ", "
        $head = "A candidate qualifies only when ALL of $joined and"
    }
    [Console]::Error.WriteLine("${Tool}: ERROR - no Python environment with VCO's KG dependencies.")
    [Console]::Error.WriteLine("${Tool}: $head")
    if ($modules.Count -le 1) {
        [Console]::Error.WriteLine("${Tool}: Probed, in order:")
    } else {
        [Console]::Error.WriteLine("${Tool}: '$($modules[$last])' import from it. Probed, in order:")
    }
    if ($script:VctLadderCandidates.Count -eq 0) {
        [Console]::Error.WriteLine("${Tool}:   (none - no VCT_VENV, no VCT_INSTALL_ROOT, no")
        [Console]::Error.WriteLine("${Tool}:    VCT_ORCHESTRATOR_ROOT in $ScriptDir\..\env, and")
        [Console]::Error.WriteLine("${Tool}:    $ProjectRoot is not a VCO orchestrator clone)")
    } else {
        foreach ($cand in $script:VctLadderCandidates) {
            [Console]::Error.WriteLine("${Tool}:   - $cand")
        }
    }
    [Console]::Error.WriteLine("${Tool}: Fix by any ONE of:")
    [Console]::Error.WriteLine("${Tool}:   * run this from a launcher-managed session (it exports")
    [Console]::Error.WriteLine("${Tool}:     VCT_INSTALL_ROOT);")
    [Console]::Error.WriteLine("${Tool}:   * `$env:VCT_VENV = 'C:\path\to\orchestrator\.venv';")
    [Console]::Error.WriteLine("${Tool}:   * `$env:VCT_INSTALL_ROOT = 'C:\path\to\orchestrator';")
    [Console]::Error.WriteLine("${Tool}:   * `$env:VCT_ORCHESTRATOR_ROOT = 'C:\path\to\orchestrator';")
    [Console]::Error.WriteLine("${Tool}:   * re-run the orchestrator install so this project's")
    [Console]::Error.WriteLine("${Tool}:     .claude\env carries VCT_ORCHESTRATOR_ROOT.")
}

# The LAST line of every refusal: one warning-prefixed sentence, after the
# tool-specific tail (v0.2.94 review item 3). PARITY: the bash sibling emits
# the identical sentence, and its docstring carries the full rationale - the
# marker is what `post-file-edit` greps for, so without it a refused scan is a
# silent no-op on exactly the installs where it has something to say.
function Write-VctLadderRefusalSummary {
    param([string]$Tool, [int]$ExitCode)
    [Console]::Error.WriteLine(
        "$([char]0x26A0)$([char]0xFE0F)  ${Tool} did NOT run (exit ${ExitCode}): " +
        "no Python environment with its dependencies. See the lines above.")
}
