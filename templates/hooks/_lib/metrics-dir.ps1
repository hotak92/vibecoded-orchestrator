# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# _lib/metrics-dir.ps1 — PowerShell sibling of _lib/metrics-dir.sh. Lockstep
# mirror: the resolution rule, the migration gate and the read order are the
# same, and `tests/test_v0292_wp8_metrics_shell_parity.py` executes BOTH files
# (when pwsh is present) and asserts they agree with `vco_lib/paths.py`.
#
# v0.2.92 W7. The metrics home moved OUT of `~/.claude` (Claude Code's own
# directory) into `$VCT_STATE_DIR` (default `~/.vct`), where every other piece
# of VCO state already lives.
#
# Python source of truth: `vco_lib/paths.py`
#   vct_metrics_dir()           -> $VCT_STATE_DIR/metrics  (default ~/.vct/metrics)
#   legacy_claude_metrics_dir() -> $VCT_CLAUDE_DIR/metrics (default ~/.claude/metrics)
#
# After dot-sourcing, the caller has the same four script-scope variables the
# .sh sibling exports:
#
#   $VcoMetricsDir        write target — the new home, OR the archive while a
#                         verified copy is still owed
#   $VcoMetricsHome       the new home, unconditionally
#   $VcoLegacyMetricsDir  the frozen archive, unconditionally
#   $VcoMetricsMigrated   $true when the copy is done / not needed
#
# **Writers switch only after a verified copy** — new home when the archive is
# absent, or holds no *.jsonl, or `<home>/.migrated-from-claude.json` records
# that `vco_lib.metrics_migration` copied AND verified it. Otherwise writers
# stay on the archive, where that machine's history already is.
#
# **The archive is never written to and never deleted** — this file only reads
# that path.
#
# Sourced, never executed. Not a hook; not registered in settings.json.

# Mirror of `pathlib.Path.home()`: .NET's UserProfile folder resolves to
# %USERPROFILE% on Windows and to $HOME on Linux/macOS, which is exactly what
# Python does. Falls back only if that returns empty.
function Get-VcoMetricsUserHome {
    $home_ = [System.Environment]::GetFolderPath('UserProfile')
    if (-not $home_) { $home_ = $env:USERPROFILE }
    if (-not $home_) { $home_ = $env:HOME }
    if (-not $home_) { $home_ = $HOME }
    return $home_
}

# Populate the four $Vco* variables in the CALLER's scope. Idempotent; never
# throws (a hook must not die because a metrics path could not be resolved).
function Resolve-VcoMetricsDirs {
    $userHome = Get-VcoMetricsUserHome

    $stateRoot = ""
    if ($env:VCT_STATE_DIR) {
        $stateRoot = $env:VCT_STATE_DIR
    } elseif ($userHome) {
        $stateRoot = Join-Path $userHome ".vct"
    }

    $legacy = ""
    if ($env:VCT_CLAUDE_DIR) {
        $legacy = Join-Path $env:VCT_CLAUDE_DIR "metrics"
    } elseif ($userHome) {
        $legacy = Join-Path (Join-Path $userHome ".claude") "metrics"
    }

    $metricsHome = ""
    if ($stateRoot) { $metricsHome = Join-Path $stateRoot "metrics" }

    # Migration state. "No archive" and "empty archive" both count as done:
    # there is nothing to copy, so there is nothing to wait for.
    $migrated = $true
    if ($legacy -and (Test-Path -LiteralPath $legacy -PathType Container)) {
        $sentinel = ""
        if ($metricsHome) { $sentinel = Join-Path $metricsHome ".migrated-from-claude.json" }
        if ($sentinel -and (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
            $migrated = $true
        } else {
            $jsonl = @()
            try {
                $jsonl = @(Get-ChildItem -LiteralPath $legacy -Filter "*.jsonl" -File -ErrorAction Stop)
            } catch {
                $jsonl = @()
            }
            if ($jsonl.Count -gt 0) { $migrated = $false }
        }
    }

    $writeDir = if ($migrated -and $metricsHome) { $metricsHome } else { $legacy }

    # Script scope, so the dot-sourcing hook sees them at top level and the
    # sibling functions below (`$script:...`) read the same values. Deliberately
    # NOT Global: hooks are one-shot processes, and a global would be the kind
    # of ambient state a future caller could set from somewhere else.
    Set-Variable -Name VcoMetricsHome      -Value $metricsHome -Scope Script
    Set-Variable -Name VcoLegacyMetricsDir -Value $legacy      -Scope Script
    Set-Variable -Name VcoMetricsMigrated  -Value $migrated    -Scope Script
    Set-Variable -Name VcoMetricsDir       -Value $writeDir    -Scope Script
}

# Create the write target and return it. Returns "" when it could not be
# resolved or created — callers treat that as "skip the metrics write", never
# as a reason to fail the hook.
function Get-VcoMetricsDir {
    if (-not $script:VcoMetricsDir) { Resolve-VcoMetricsDirs }
    if (-not $script:VcoMetricsDir) { return "" }
    try {
        if (-not (Test-Path -LiteralPath $script:VcoMetricsDir -PathType Container)) {
            New-Item -ItemType Directory -Force -Path $script:VcoMetricsDir -ErrorAction Stop | Out-Null
        }
    } catch {
        return ""
    }
    return $script:VcoMetricsDir
}

# Return the FIRST existing copy of a metrics file, new home before archive,
# so a reader on a machine that has not migrated (or whose user-modified hook
# still appends to the archive) still finds its data. "" when neither exists.
function Get-VcoMetricsReadFile {
    param([string]$Name)
    if (-not $Name) { return "" }
    if (-not $script:VcoMetricsHome -and -not $script:VcoLegacyMetricsDir) { Resolve-VcoMetricsDirs }
    foreach ($dir in @($script:VcoMetricsHome, $script:VcoLegacyMetricsDir)) {
        if (-not $dir) { continue }
        $candidate = Join-Path $dir $Name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return ""
}

# Best-effort trigger for the once-per-machine COPY. Costs one Test-Path on a
# fresh install and one after the first success — the Python is only spawned
# while a copy is genuinely owed. Soft-fails in every direction; a failure
# leaves writers on the archive, which is the safe state.
function Invoke-VcoMetricsMigrateOnce {
    param([string]$ScriptDir)
    if ($null -eq $script:VcoMetricsMigrated) { Resolve-VcoMetricsDirs }
    if ($script:VcoMetricsMigrated) { return }
    if (-not $script:VcoMetricsHome) { return }
    $resolver = Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1"
    if (-not (Test-Path -LiteralPath $resolver -PathType Leaf)) { return }
    try {
        . $resolver
        $py = Resolve-VcoVenvPython -ScriptDir $ScriptDir
        if (-not $py) { return }
        & $py -m vco_lib.metrics_migration --quiet 2>$null | Out-Null
    } catch {
        return
    } finally {
        # Re-resolve so this invocation's writes already use the new home when
        # the copy just succeeded.
        Resolve-VcoMetricsDirs
    }
}
