# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# compact-context-reinject.ps1
# Fires on SessionStart matchers "compact" and "startup"
# (v0.2.102 W6-F: the ORCHESTRATOR template documented "SessionStart
# (compact/resume)" while only compact was registered, and
# diff-context-inject's "full injection" comment had no backing hook at
# startup). Re-injects critical session context to stdout (becomes
# additionalContext); at startup injects CONTEXT_STATE.md (in full,
# inside the per-section budgets and the shared cap). The COMPACTION
# ARTEFACT (pre-compact snapshot) is emitted only for source=compact or an
# absent/unknown source (the pre-W6-F shape). v0.2.102 W6-K: the "resume"
# matcher was withdrawn (owner 2026-10-11 -- a resumed chat already has
# CONTEXT_STATE in context) and the pruned-activity summary section was
# removed with precompact_prune.py.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# Shared session_id parse + path-safety sanitise (Get-VcoHookSessionId). One
# implementation for all four context hooks; see _lib/session-id.ps1.
. "$PSScriptRoot/_lib/session-id.ps1"

# Project root: the ONE rule (_lib/project-root.ps1, must match
# vco_lib/project_root.py): CLAUDE_PROJECT_DIR, else the nearest marked VCO
# project above the current location. None -> nothing to do (no state is written).
. (Join-Path $PSScriptRoot "_lib/project-root.ps1")
$ProjectDir = Get-VcoProjectRoot
if (-not $ProjectDir) { exit 0 }

# Track C (v0.2.65): must match templates/hooks/compact-context-reinject.sh.
# SessionStart payload carries session_id on stdin; the shared
# Get-VcoHookSessionId parses it (ConvertFrom-Json → session_id) so we can
# locate this session's own CONTEXT_STATE file. Empty when the payload is
# absent/malformed → the per-session block below is skipped. Defense-in-depth
# (review C-1): the helper also sanitises the id to [A-Za-z0-9_-] before it
# reaches the file path below (hostile `/`/`..` → "default"). Line-budget
# split: shared CONTEXT_STATE.md uncapped at START, per-session file (if
# present) injected right after with a 120-line sub-cap.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$SessionId = Get-VcoHookSessionId -Stdin $HookStdin

# v0.2.102 W6-F (items 5+6): which SessionStart source fired us. The
# compaction artefact section (snapshot) is emitted only
# for compactions; an absent/unknown source keeps the pre-W6-F shape.
# MUST MATCH the .sh sibling's _IS_COMPACT_SOURCE.
$HookSource = ""
if ($HookStdin) {
    try {
        $srcPayload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
        if ($srcPayload -and $srcPayload.source) { $HookSource = [string]$srcPayload.source }
    } catch { }
}
$IsCompactSource = ($HookSource -eq "" -or $HookSource -eq "compact")

# v0.2.104: after a compaction Claude Code itself re-injects a fresh git
# status (which carries the recent commits) -- vendor page
# https://code.claude.com/docs/en/context-window, "What survives compaction".
# Repeating it here only spends the 10 000-char hook budget twice, so when
# the payload POSITIVELY says source=compact (an absent/unknown source keeps
# the full pre-v0.2.104 shape -- repeating is the safe direction) the Recent
# Commits section and the snapshot's git-status block are dropped. Every
# other section (CONTEXT_STATE, the session state, the plan) is re-injected
# in full: whether the harness also re-attached one of those files is only
# flushed to the transcript around this hook's own start, so it is not
# knowable reliably here. The snapshot trimming is
# `python -m vco_lib.compact_reattach snapshot-body`; when that is
# unavailable the plain first 50 lines are replayed.
# MUST MATCH compact-context-reinject.sh.
$IsHarnessCompact = ($HookSource -eq "compact")
$RcPy = ""
if ($IsHarnessCompact) {
    $FindPy = Join-Path $PSScriptRoot "_lib/find-python.ps1"
    if (Test-Path -LiteralPath $FindPy) { . $FindPy }
    if ($PY) {
        $RcPy = $PY
        $VenvLib = Join-Path $PSScriptRoot "_lib/resolve-vco-venv.ps1"
        if (Test-Path -LiteralPath $VenvLib) {
            . $VenvLib
            try {
                $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $PSScriptRoot
                if ($VcoVenvPython -and (Test-Path $VcoVenvPython)) { $RcPy = $VcoVenvPython }
            } catch { }
        }
    }
}

# v0.2.100 WP-17: the WHOLE output goes through the shared injection cap
# (_lib/emit-context.ps1 Limit-VcoHookContext): Claude Code shows the model
# only a file path + a 2 000-character preview past 10 000 characters, and
# this hook exceeded that on 223 of 235 measured runs. The cut keeps the
# START (CONTEXT_STATE) and ends with a marker naming the files to Read.
# A missing helper (broken install) re-injects uncapped rather than nothing.
#
# v0.2.102 W6-D: each section now gets a CHARACTER budget inside the cap --
# the committed table _lib/reinject-budgets.txt (A>B>C tier B: one table
# both OS siblings parse) -- and is truncated WITHIN itself with a pointer
# naming its file, so an oversized CONTEXT_STATE.md can no longer crowd the
# plan / commits / snapshot sections out of the model's context entirely.
# CONTEXT_STATE is the flex section (cap minus overhead minus what the
# present fixed sections take); the whole-stream cap stays as the backstop.
# A snapshot older than the current session's compaction is NOT replayed
# (defence in depth -- see the staleness guard below).
# MUST MATCH compact-context-reinject.sh.
$EmitLib = Join-Path $PSScriptRoot "_lib/emit-context.ps1"
if (Test-Path -LiteralPath $EmitLib) { . $EmitLib }

# -- the per-section budget table (one committed table both siblings parse) --
$RB = @{}
$BudgetFile = Join-Path $PSScriptRoot "_lib/reinject-budgets.txt"
if (Test-Path -LiteralPath $BudgetFile) {
    foreach ($line in (Get-Content -LiteralPath $BudgetFile)) {
        $trimmed = "$line".Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $parts = $trimmed -split '\s+', 3
        if ($parts.Count -lt 2) { continue }
        if ($parts[1] -notmatch '^[0-9]+$') { continue }
        # v0.2.102 W6-D review F3: rows must be POSITIVE integers. A 0 row is
        # rejected with one stderr line and its section runs uncapped -- the
        # same semantics the .sh sibling enforces.
        if ([int]$parts[1] -le 0) {
            [Console]::Error.WriteLine("[VCO] reinject-budgets.txt: ignoring non-positive budget row '$($parts[0]) $($parts[1])' (rows must be positive integers; the section runs uncapped)")
            continue
        }
        $RB[$parts[0]] = [int]$parts[1]
    }
} else {
    [Console]::Error.WriteLine("[VCO broken install] .claude/hooks/_lib/reinject-budgets.txt is missing; the compact re-inject falls back to the whole-stream cap (a large CONTEXT_STATE.md can again crowd out the later sections).")
}
# A budget of 0 means "uncapped" (row absent / table missing).
function Get-RbBudget([string]$Key) {
    if ($RB.ContainsKey($Key)) { return [int]$RB[$Key] }
    return 0
}
function Get-RbAlloc([string]$Text, [int]$Budget) {
    if ($Budget -le 0) { $Budget = 1000000000 }
    return [Math]::Min($Text.Length, $Budget)
}

# -- staleness guard for the pre-compact snapshot (item 1, defence in depth) --
# pre-compact-save.ps1 now fires on manual compacts too (PreCompact matcher
# "*"), so the snapshot should be as fresh as the compaction that triggered
# this re-inject. When it is NOT, replaying it would show days-old git
# status as if current -- say so in one line instead. Unknown mtime =>
# replay, never a false "stale". Default tolerance 30 min.
$SnapshotMaxAge = 1800
if ($env:VCO_SNAPSHOT_MAX_AGE -match '^[0-9]+$') { $SnapshotMaxAge = [int]$env:VCO_SNAPSHOT_MAX_AGE }
function Test-VcoSnapshotStale([string]$Path) {
    try {
        $age = ((Get-Date) - (Get-Item -LiteralPath $Path).LastWriteTime).TotalSeconds
        return ($age -gt $SnapshotMaxAge)
    } catch { return $false }
}
function Get-VcoSnapshotSavedLine([string]$Path) {
    try {
        $hit = Get-Content -LiteralPath $Path -TotalCount 5 |
            Where-Object { $_ -like 'Saved:*' } | Select-Object -First 1
        if ($hit) { return "$hit" }
    } catch { }
    return "(unknown save time)"
}

# -- build the sections (text incl. their own header) --
$S_ctx = ""; $S_sess = ""; $S_plan = ""; $S_commits = ""; $S_snap = ""

# 1. CONTEXT_STATE.md -- START position (the FLEX section). The header
# names WHY we fired (compact vs startup/resume) so the model does not read
# a fresh session's injection as post-compaction recovery. MUST MATCH the
# .sh sibling.
$CtxState = Join-Path $ProjectDir ".claude/CONTEXT_STATE.md"
if (Test-Path -LiteralPath $CtxState) {
    if ($IsCompactSource) {
        $S_ctx = "## Current Task State (re-injected after compaction)`n" + ([IO.File]::ReadAllText($CtxState).TrimEnd())
    } else {
        $S_ctx = "## Current Task State (re-injected at session start: $HookSource)`n" + ([IO.File]::ReadAllText($CtxState).TrimEnd())
    }
}

# 1b. Per-session task state (Track C). IF this session has written its own
# .claude/context/CONTEXT_STATE_<session_id>.md, reinject it right after the
# shared rollup (most task-relevant for THIS chat), line-capped at 120, then
# char-capped at its table budget. Gated on both a resolved session_id AND
# file existence.
if ($SessionId) {
    $SessionCtxFile = Join-Path $ProjectDir ".claude/context/CONTEXT_STATE_$SessionId.md"
    if (Test-Path -LiteralPath $SessionCtxFile) {
        $S_sess = "## This Session's Task State (re-injected after compaction)`n" + ((Get-Content -LiteralPath $SessionCtxFile -TotalCount 120) -join "`n")
    }
}

# 2. Active plan summary (line cap 30)
$LatestPlan = $null
$PlansDir = Join-Path $ProjectDir ".claude/context/plans"
if (Test-Path -LiteralPath $PlansDir) {
    $LatestPlan = Get-ChildItem -Path $PlansDir -Filter "*.md" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '[/\\]archive[/\\]' } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($LatestPlan) {
        $S_plan = "## Active Plan: $($LatestPlan.Name)`n" + ((Get-Content -LiteralPath $LatestPlan.FullName -TotalCount 30) -join "`n")
    }
}

# 3. Recent git activity (cap 8 commits). Not after a compaction
#    (v0.2.104): the harness re-injects a fresh git status with the recent
#    commits itself.
if (-not $IsHarnessCompact) {
    $gitLog = ""
    try { $gitLog = (@(& git -C $ProjectDir log --oneline -8 2>$null) | ForEach-Object { "$_" }) -join "`n" } catch { }
    $S_commits = "## Recent Commits`n$gitLog"
}

# 4. Pre-compaction snapshot (line cap 50); stale => one line, NOT replayed.
#    A compaction artefact: only emitted for source=compact / unknown (W6-F).
$Snapshot = Join-Path $ProjectDir ".claude/context/pre-compact-snapshot.md"
if ($IsCompactSource -and (Test-Path -LiteralPath $Snapshot)) {
    if (Test-VcoSnapshotStale -Path $Snapshot) {
        $S_snap = "## Pre-Compaction Snapshot (stale -- NOT replayed)`nLast saved: $(Get-VcoSnapshotSavedLine -Path $Snapshot). This snapshot is older than the current session's compaction, so pre-compact-save.ps1 did not run for it (it should, on every compact since v0.2.102). Read .claude/context/pre-compact-snapshot.md only if you need it."
    } else {
        # v0.2.104: minus its git-status block (the harness re-injects a
        # fresh one); the plain first 50 lines when the module is unavailable.
        $SnapBody = ""
        if ($RcPy) {
            try {
                $snapOut = @(& $RcPy -m vco_lib.compact_reattach snapshot-body --path $Snapshot 2>$null)
                if ($LASTEXITCODE -eq 0) { $SnapBody = ($snapOut | ForEach-Object { "$_" }) -join "`n" }
            } catch { }
        }
        if (-not $SnapBody) { $SnapBody = (Get-Content -LiteralPath $Snapshot -TotalCount 50) -join "`n" }
        $S_snap = "## Pre-Compaction Snapshot`n" + $SnapBody
    }
}

# -- budget the flex section, then emit every section under its budget --
$HaveCap = [bool](Get-Command Limit-VcoHookContext -ErrorAction SilentlyContinue)
$RbMinSection = 300   # a section's floor: its header plus a cut marker
$RbFlexFloor = 200
$Flex = 0
if ($HaveCap -and $RB.ContainsKey('overhead')) {
    $cap = Get-VcoHookContextCap
    # v0.2.102 W6-D review F2: a user-lowered VCO_HOOK_CONTEXT_CAP below what
    # the table asks for (overhead + sum of fixed budgets + the flex floor)
    # scales the FIXED budgets down proportionally, each floored at
    # $RbMinSection chars, so every section stays represented at any cap down
    # to the sane floor 1650 (4*300 + 200 flex floor + 250 headroom;
    # v0.2.102 W6-K removed the fifth fixed section with precompact_prune.py).
    # Below that no budgeting can keep five headers, and the whole-stream backstop
    # cuts inside the later sections -- documented in the table.
    $fixedTotal = 0
    foreach ($k in 'session_state','plan','commits','snapshot') {
        if ($RB.ContainsKey($k)) { $fixedTotal += [int]$RB[$k] }
    }
    if ([int]$RB['overhead'] + $fixedTotal + $RbFlexFloor -gt $cap) {
        $avail = $cap - [int]$RB['overhead'] - $RbFlexFloor
        if ($avail -lt ($RbMinSection * 4)) {
            $avail = $cap - 250 - $RbFlexFloor   # bare-minimum headroom
        }
        if ($avail -gt 0 -and $fixedTotal -gt 0) {
            foreach ($k in 'session_state','plan','commits','snapshot') {
                if ($RB.ContainsKey($k)) {
                    $RB[$k] = [Math]::Max($RbMinSection, [int][Math]::Floor([int]$RB[$k] * $avail / $fixedTotal))
                }
            }
        }
    }
    $fixed = 0
    if ($S_sess)    { $fixed += Get-RbAlloc $S_sess    (Get-RbBudget 'session_state') }
    if ($S_plan)    { $fixed += Get-RbAlloc $S_plan    (Get-RbBudget 'plan') }
    if ($S_commits) { $fixed += Get-RbAlloc $S_commits (Get-RbBudget 'commits') }
    if ($S_snap)    { $fixed += Get-RbAlloc $S_snap    (Get-RbBudget 'snapshot') }
    $Flex = $cap - [int]$RB['overhead'] - $fixed
    if ($Flex -lt $RbFlexFloor) { $Flex = $RbFlexFloor }
}

function Emit-RbSection([string]$Text, [int]$Budget, [string]$Pointer) {
    if (-not $Text) { return @() }
    $out = $Text
    if ($Budget -gt 0 -and $HaveCap) {
        $out = Limit-VcoHookContext -Text $Text -Pointer $Pointer -Limit $Budget
    }
    return @($out, "")
}

$Parts = @()
$Parts += Emit-RbSection $S_ctx $Flex "rest is on disk: Read .claude/CONTEXT_STATE.md"
$Parts += Emit-RbSection $S_sess (Get-RbBudget 'session_state') "rest is on disk: Read .claude/context/CONTEXT_STATE_$SessionId.md"
$PlanPointer = ""
if ($LatestPlan) { $PlanPointer = "rest is on disk: Read .claude/context/plans/$($LatestPlan.Name)" }
$Parts += Emit-RbSection $S_plan (Get-RbBudget 'plan') $PlanPointer
$Parts += Emit-RbSection $S_commits (Get-RbBudget 'commits') "run 'git log --oneline -20' for more"
$Parts += Emit-RbSection $S_snap (Get-RbBudget 'snapshot') "rest is on disk: Read .claude/context/pre-compact-snapshot.md"

# v0.2.102 W6-D review F4: strip the trailing newlines, cap, then write the
# text + exactly ONE newline -- byte-for-byte what the .sh sibling's
# vco_cap_context_stream emits (Write-Output would add a second trailing
# newline and the two siblings' outputs would differ only by that byte).
$ReinjectText = ((@($Parts) | ForEach-Object { [string]$_ }) -join "`n").TrimEnd("`r", "`n")
if ($HaveCap) {
    $ReinjectText = Limit-VcoHookContext -Text $ReinjectText -Pointer "The rest is on disk: Read .claude/CONTEXT_STATE.md (and .claude/context/CONTEXT_STATE_<session>.md, the active plan under .claude/context/plans/, .claude/context/pre-compact-snapshot.md)."
}
if ($ReinjectText) { [Console]::Out.Write($ReinjectText + "`n") }
exit 0
