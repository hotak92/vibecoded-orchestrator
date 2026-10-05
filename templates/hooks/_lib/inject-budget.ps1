# _lib/inject-budget.ps1
# Per-turn injection budget + kill switch for the v0.2.101 injection redesign
# (PLAN-V02101 section 2.1 "Additional bounds"). The PowerShell sibling of
# _lib/inject-budget.sh, dot-sourced by the .ps1 injection wrappers.
#
# The ENFORCEMENT home is the router (hook_context_router.py counts chars at
# emit time through vco_lib/inject_intent.py's constants); this lib gives the
# shell side the same answers: the kill-switch check the wrappers run BEFORE
# spawning anything, the budget-file path convention, a read-only probe, and
# the 1-day GC.
#
# Fail-open discipline (mirrors the seen-store's inject-blind guard): an
# untrustworthy session id ("" / "default" / hostile chars) or a MISSING
# prompt id yields an EMPTY path -- no enforcement, no shared bucket.
#
# MUST MATCH: _lib/inject-budget.sh AND vco_lib/inject_intent.py::
# budget_state_path (the Python one-home the router enforces). Parity is
# pinned behaviourally by tests/test_v02101_inject_gates.py -- change one,
# change all three.
#
# Plain ASCII only. Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoInjectBudgetSourced) { return }
$script:VcoInjectBudgetSourced = $true

# Test-VcoInjectProfileOff -- $true when the kill switch is engaged
# (VCO_INJECT_PROFILE=off, any case). The router re-checks this itself; the
# wrapper check is the fast path that avoids the spawn entirely.
function Test-VcoInjectProfileOff {
    $v = "$env:VCO_INJECT_PROFILE"
    if (-not $v) { return $false }
    return ($v.ToLowerInvariant() -eq "off")
}

function Test-VcoInjectStateComponent {
    param([string]$Value)
    if (-not $Value) { return $false }
    return ($Value -cmatch '^[A-Za-z0-9_-]+$')
}

# Get-VcoInjectBudgetPath <SessionId> <PromptId> [ProjectRoot]
# The per-turn budget counter file, or "" when enforcement must fail open.
function Get-VcoInjectBudgetPath {
    param([string]$SessionId, [string]$PromptId, [string]$ProjectRoot = "")
    if (-not (Test-VcoInjectStateComponent $SessionId)) { return "" }
    if ($SessionId -eq "default") { return "" }
    if (-not (Test-VcoInjectStateComponent $PromptId)) { return "" }
    $root = $ProjectRoot
    if (-not $root) { $root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } elseif ($script:ProjectRoot) { $script:ProjectRoot } else { "" } }
    if (-not $root) { return "" }
    return (Join-Path (Join-Path (Join-Path $root ".claude") "state") "inject_budget_${SessionId}_${PromptId}")
}

# Get-VcoInjectBudgetUsed <Path> -- chars already emitted this turn (0 on a
# missing/unreadable/non-numeric file -- soft-fail, never an error).
function Get-VcoInjectBudgetUsed {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $raw = (Get-Content -LiteralPath $Path -Raw -ErrorAction Stop)
        if ($null -eq $raw) { return 0 }
        $raw = "$raw".Trim()
        if ($raw -notmatch '^\d+$') { return 0 }
        return [int]$raw
    } catch { return 0 }
}

# Remove-VcoInjectBudgetStale [ProjectRoot] -- delete budget files older than
# 1 day (BUDGET_GC_AGE_S in the Python home). Opportunistic, bounded,
# soft-fail.
function Remove-VcoInjectBudgetStale {
    param([string]$ProjectRoot = "")
    $root = $ProjectRoot
    if (-not $root) { $root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } elseif ($script:ProjectRoot) { $script:ProjectRoot } else { "" } }
    if (-not $root) { return }
    $state = Join-Path (Join-Path $root ".claude") "state"
    if (-not (Test-Path -LiteralPath $state)) { return }
    $cutoff = (Get-Date).AddSeconds(-86400)
    try {
        Get-ChildItem -LiteralPath $state -File -Filter 'inject_budget_*' -ErrorAction SilentlyContinue |
            Select-Object -First 200 |
            Where-Object { $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}
