# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/compose-dir.ps1 — WHICH compose directory the container hooks may run
# compose from, and the refusal when it is not the orchestrator's own.
# POSIX mirror: _lib/compose-dir.sh (MUST stay logically identical).
#
# Resolution order (the first candidate that is a directory):
#   1. $env:VCT_COMPOSE_DIR                     - explicit override
#   2. $env:VCT_INFRASTRUCTURE_DIR              - the orchestrator's infrastructure\
#   3. $env:VCT_ORCHESTRATOR_ROOT\infrastructure
#   4. <repo_root>\infrastructure
#   5. <repo_root>\claude_mcp_servers           - the orchestrator clone's legacy home
#
# v0.2.100 (review L1-F17, owner ruling Q3): the directory is used ONLY when
# its PARENT is the orchestrator clone - it carries `vct-module.json` whose
# "id" is "orchestrator". A project's copy of the compose files (no .env data
# knobs, override or build context) is REFUSED with a message naming
# VCT_ORCHESTRATOR_ROOT, and nothing is composed from it.
#
# Usage:
#     . (Join-Path $PSScriptRoot "_lib\compose-dir.ps1")
#     $cd = Resolve-VcoComposeDir -RepoRoot $RepoRoot
#     # -> $cd.Dir (usable dir or "") and $cd.Refusal ("" or the line to print)

function Test-VcoOrchestratorRoot {
    param([string]$Dir)
    $manifest = Join-Path $Dir "vct-module.json"
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $false }
    try {
        $doc = Get-Content -LiteralPath $manifest -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        return ($doc.id -eq 'orchestrator')
    } catch {
        return $false
    }
}

function Resolve-VcoComposeDir {
    param([string]$RepoRoot)
    $candidates = @(
        $env:VCT_COMPOSE_DIR,
        $env:VCT_INFRASTRUCTURE_DIR,
        $(if ($env:VCT_ORCHESTRATOR_ROOT) { Join-Path $env:VCT_ORCHESTRATOR_ROOT "infrastructure" } else { "" }),
        (Join-Path $RepoRoot "infrastructure"),
        (Join-Path $RepoRoot "claude_mcp_servers")
    )
    $found = ""
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Container)) { $found = $c; break }
    }
    if (-not $found) { return [pscustomobject]@{ Dir = ""; Refusal = "" } }
    $parent = Split-Path -Parent ((Resolve-Path -LiteralPath $found).Path)
    if ($parent -and (Test-VcoOrchestratorRoot -Dir $parent)) {
        return [pscustomobject]@{ Dir = $found; Refusal = "" }
    }
    return [pscustomobject]@{
        Dir = ""
        Refusal = "refusing to run compose from ${found}: it is not the VCO orchestrator's own infrastructure/ (no vct-module.json with id `"orchestrator`" beside it), so compose could create containers on EMPTY default volumes. Set VCT_ORCHESTRATOR_ROOT to your VCO clone in .claude/env; nothing was created."
    }
}
