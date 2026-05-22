# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_retrieval_tuning_set.ps1 — write a single retrieval tuning value
# (or reset the whole block to defaults) into
# <vct_root_dir>/retrieval-tuning.toml.
#
# v0.2.22 Item #13 (2026-05-20). PowerShell 7+ required. Sibling of
# vct_retrieval_tuning_set.sh — same validation invariant, same atomic
# tmp+rename write posture, same exit codes.
#
# Usage:
#   vct_retrieval_tuning_set.ps1 -Field NAME -Value V
#   vct_retrieval_tuning_set.ps1 -Reset
#
# Exit codes:
#   0  success
#   1  disk error
#   2  validation failed (out-of-range / ordering)
#   4  unknown field
#   64 usage error
#
# The hub does NOT expose a write endpoint for retrieval tuning in
# v0.2.22 — the launcher's Tauri command is the only authenticated
# writer. Headless callers update the file directly; the hub re-reads
# on every /config response (no in-memory cache for these values), so
# the next resolver call observes the change immediately.

[CmdletBinding(DefaultParameterSetName = 'Field')]
param(
    [Parameter(ParameterSetName = 'Field', Mandatory = $true)]
    [ValidateSet(
        'code_graph_score_floor',
        'kg_tier_min',
        'kg_tier_single_chunk',
        'kg_tier_three_chunks',
        'kg_tier_full'
    )]
    [string]$Field,

    [Parameter(ParameterSetName = 'Field', Mandatory = $true)]
    [double]$Value,

    [Parameter(ParameterSetName = 'Reset')]
    [switch]$Reset
)

$ErrorActionPreference = 'Stop'

# Calibrated defaults — must match the Rust struct, the Svelte panel,
# the bash sibling, and the get.ps1 fallback. DO NOT CHANGE.
$DefaultTuning = [ordered]@{
    code_graph_score_floor = 0.35
    kg_tier_min            = 0.42
    kg_tier_single_chunk   = 0.55
    kg_tier_three_chunks   = 0.65
    kg_tier_full           = 0.75
}

function Write-VctWarn([string]$Message) {
    [Console]::Error.WriteLine("[vct-retrieval-tuning] $Message")
}

function Resolve-VctStateDir {
    if ($env:VCT_STATE_DIR) {
        return $env:VCT_STATE_DIR
    }
    if ($IsWindows) {
        return Join-Path $env:USERPROFILE '.vct'
    }
    return Join-Path $env:HOME '.vct'
}

function Resolve-TomlPath {
    return Join-Path (Resolve-VctStateDir) 'retrieval-tuning.toml'
}

function Read-ExistingTuning {
    # Returns an ordered hash with every field populated — missing
    # values come from $DefaultTuning so callers can swap one knob and
    # always have a complete block to validate + write.
    $out = [ordered]@{}
    foreach ($k in $DefaultTuning.Keys) {
        $out[$k] = $DefaultTuning[$k]
    }
    $path = Resolve-TomlPath
    if (Test-Path -LiteralPath $path) {
        try {
            foreach ($line in Get-Content -LiteralPath $path -ErrorAction Stop) {
                $trimmed = $line.Trim()
                if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
                $eq = $trimmed.IndexOf('=')
                if ($eq -lt 1) { continue }
                $name = $trimmed.Substring(0, $eq).Trim()
                $rawValue = $trimmed.Substring($eq + 1).Trim()
                if ($out.Contains($name)) {
                    [double]$parsed = 0
                    if ([double]::TryParse(
                        $rawValue,
                        [System.Globalization.NumberStyles]::Float,
                        [System.Globalization.CultureInfo]::InvariantCulture,
                        [ref]$parsed)) {
                        $out[$name] = $parsed
                    }
                }
            }
        } catch {
            Write-VctWarn "could not read existing $path ($_); rebuilding from defaults"
        }
    }
    return $out
}

function Test-TuningInvariant {
    param([System.Collections.Specialized.OrderedDictionary]$Tuning)

    foreach ($k in $Tuning.Keys) {
        $v = [double]$Tuning[$k]
        if ([double]::IsNaN($v) -or [double]::IsInfinity($v)) {
            Write-VctWarn "$k is not finite ($v)"
            return $false
        }
        if ($v -lt 0.0 -or $v -gt 1.0) {
            Write-VctWarn "$k = $v not in [0, 1]"
            return $false
        }
    }
    if (-not ($Tuning['kg_tier_min'] -lt $Tuning['kg_tier_single_chunk'])) {
        Write-VctWarn ("kg_tier_min ({0}) must be < kg_tier_single_chunk ({1})" `
            -f $Tuning['kg_tier_min'], $Tuning['kg_tier_single_chunk'])
        return $false
    }
    if (-not ($Tuning['kg_tier_single_chunk'] -lt $Tuning['kg_tier_three_chunks'])) {
        Write-VctWarn ("kg_tier_single_chunk ({0}) must be < kg_tier_three_chunks ({1})" `
            -f $Tuning['kg_tier_single_chunk'], $Tuning['kg_tier_three_chunks'])
        return $false
    }
    if (-not ($Tuning['kg_tier_three_chunks'] -lt $Tuning['kg_tier_full'])) {
        Write-VctWarn ("kg_tier_three_chunks ({0}) must be < kg_tier_full ({1})" `
            -f $Tuning['kg_tier_three_chunks'], $Tuning['kg_tier_full'])
        return $false
    }
    return $true
}

function Write-TuningBlock {
    param([System.Collections.Specialized.OrderedDictionary]$Tuning)

    if (-not (Test-TuningInvariant -Tuning $Tuning)) {
        exit 2
    }

    $target = Resolve-TomlPath
    $parent = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    $sb = [System.Text.StringBuilder]::new()
    foreach ($k in $Tuning.Keys) {
        # Use invariant culture so locales with comma decimal separators
        # don't write `0,42` into the TOML (TOML requires `.`).
        [void]$sb.AppendLine(("{0} = {1}" -f $k, ([double]$Tuning[$k]).ToString($culture)))
    }

    $tmp = "$target.tmp"
    try {
        # Write with no BOM. The Rust reader's toml crate handles BOMs
        # but we want byte-identity with the bash sibling for diff-easy.
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($tmp, $sb.ToString(), $utf8NoBom)
        # Atomic-ish rename. On Windows -Force overwrites; on POSIX it's
        # rename(2).
        Move-Item -LiteralPath $tmp -Destination $target -Force
    } catch {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
        }
        Write-VctWarn "write failed: $_"
        exit 1
    }
}

# ── Main ────────────────────────────────────────────────────────────────
if ($PSCmdlet.ParameterSetName -eq 'Reset') {
    $block = [ordered]@{}
    foreach ($k in $DefaultTuning.Keys) {
        $block[$k] = $DefaultTuning[$k]
    }
    Write-TuningBlock -Tuning $block
    exit 0
}

# Field/Value mode: load existing values, swap the named field,
# validate the whole block, write.
$existing = Read-ExistingTuning
$existing[$Field] = [double]$Value
Write-TuningBlock -Tuning $existing
exit 0
