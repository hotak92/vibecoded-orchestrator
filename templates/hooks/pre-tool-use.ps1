# Parity: kept in step with pre-tool-use.sh (this file's UTF-8 BOM serves
# Windows PowerShell 5.1 only; block-reason messages go to stderr on both
# sides; every behaviour must match the .sh sibling).
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# v0.2.104: this hook's clock. The harness kills it after the `timeout`
# registered for pre-tool-use (3 s in both settings templates) and a killed
# PreToolUse hook does NOT block -- so the Build Anchor's post-compaction
# check below may only spend what is LEFT of that budget. Measured from the
# PROCESS start when the OS reports it (it then includes the powershell
# cold start, the costly part on Windows), else from this line; the larger
# of the two counts. MUST MATCH pre-tool-use.sh's _ptu_clock_us.
$PtuClock = [Diagnostics.Stopwatch]::StartNew()
$PtuProcStart = $null
try { $PtuProcStart = [Diagnostics.Process]::GetCurrentProcess().StartTime } catch { }
function Get-PtuElapsedMs {
    $ms = [double]$PtuClock.Elapsed.TotalMilliseconds
    if ($PtuProcStart) {
        try {
            $fromProc = ([DateTime]::Now - $PtuProcStart).TotalMilliseconds
            if ($fromProc -gt $ms) { $ms = $fromProc }
        } catch { }
    }
    return [int][Math]::Ceiling($ms)
}

# Get-PtuBudgetS: the harness's kill timeout for this hook.
# VCO_PRE_TOOL_USE_TIMEOUT_S (whole seconds) MUST MATCH the `timeout`
# registered for pre-tool-use in .claude/settings.json. MUST MATCH
# pre-tool-use.sh's _ptu_budget_s: digits only, read as decimal, 1..999999;
# anything else -- empty, non-digit, 0, over-range -- reads as 3, silently
# (TryParse: an over-range value must not throw a red error line into the
# hook's stderr).
function Get-PtuBudgetS {
    $parsed = [long]0
    if ([string]$env:VCO_PRE_TOOL_USE_TIMEOUT_S -match '^[0-9]+$' -and
        [long]::TryParse([string]$env:VCO_PRE_TOOL_USE_TIMEOUT_S, [Globalization.NumberStyles]::None, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed) -and
        $parsed -ge 1 -and $parsed -le 999999) {
        return [int]$parsed
    }
    return 3
}

# VCO-CENTRALIZED-KG: NOT a KG consumer (v0.2.101 wave-3). The former
#   section-5 KG-suggestion path was retired (double emission with the
#   pre-edit/pre-write router wrappers -- review nit-6); every remaining
#   branch (SSRF guard, shell-injection scan, Build Anchor, file backup,
#   tool logging) is KG/codegraph-free. Marker kept (the centralization
#   audit classifies every hook); classification: no KG access.

# pre-tool-use.ps1
# Pre-tool-use hook: SSRF guard, shell injection scan, Build Anchor
# Protocol, file backup. (KG search suggestion RETIRED, v0.2.101 wave-3.)
#
# v0.2.77 9-bis: the per-tool-call TOUCAN dataset writer
# (.claude/logs/toucan_dataset.jsonl) was RETIRED here — a write-only
# collector with zero consumers (RL training telemetry lives in
# launcher.db rl_events + the citation drain, unaffected). MUST MATCH
# pre-tool-use.sh (same removal). The stdin parse below still decodes the
# full payload for the security + Build-Anchor branches.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# Source emit-context.ps1 ONLY if the file exists. If the helper is
# missing (partial install or just-after-clone before _lib/ is fully
# populated), the hook still runs its other branches (logging,
# security guards). The KG-suggestion branch below uses Get-Command
# to tolerate a missing Emit-AdditionalContext.
if (Test-Path "$PSScriptRoot/_lib/emit-context.ps1") {
    . "$PSScriptRoot/_lib/emit-context.ps1"
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($args) and $env:CLAUDE_TOOL_NAME etc. are EMPTY because
# Claude Code does NOT populate those env vars — verified empirically
# 2026-05-08 via stdin-capture diagnostic. Without this, the SSRF guard,
# shell-injection scan, and Build-Anchor branches would all see empty
# tool_name / tool_input.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
# v0.2.76 P5 (hook-latency parity): the bash sibling consolidated its stdin
# parse from SIX `python -c` spawns into ONE NUL-delimited decode (each
# interpreter cold-start cost ~15ms and this hook fires on the `*` matcher —
# every tool call). PowerShell already single-decodes here via one
# ConvertFrom-Json — no per-field re-parse ever existed — so the perf issue
# was bash-only. This touch keeps the OS-parity gate satisfied AND aligns the
# tool_input default with the bash side: default to a valid "{}" (not "") so a
# missing tool_input yields the same empty-object shape on both OSes.
$ToolArgs = "{}"
$UserMessage = ""
$SessionIdFromStdin = ""
# V52-L.2 Fix 1: parse subagent identity from stdin payload. Per A5
# audit, PreToolUse hooks DO fire for subagent tool calls and the
# payload carries agent_id + agent_type. Parsed for parity with the
# post-tool-security / post-file-edit siblings that still consume them;
# this hook's own former consumer (the TOUCAN row) was retired in
# v0.2.77 9-bis. Empty string when absent (parent context).
$AgentId = ""
$AgentType = ""
# WP-E (v0.2.92): transcript_path + prompt_id ride the shared parse.
# v0.2.101 wave-3: this hook no longer CONSUMES them (section 5 retired --
# the router wrappers resolve both from the payload themselves); the fields
# stay in the parse so the sibling contract is unchanged. transcript_path
# remains a PATH ONLY everywhere (R31). MUST MATCH pre-tool-use.sh's
# NUL-delimited parse.
$TranscriptPath = ""
$PromptId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)       { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_input)      { $ToolArgs = ($payload.tool_input | ConvertTo-Json -Compress -Depth 8) }
        if ($payload.user_message)    { $UserMessage = [string]$payload.user_message }
        if ($payload.session_id)      { $SessionIdFromStdin = [string]$payload.session_id }
        if ($payload.agent_id)        { $AgentId = [string]$payload.agent_id }
        if ($payload.agent_type)      { $AgentType = [string]$payload.agent_type }
        if ($payload.transcript_path) { $TranscriptPath = [string]$payload.transcript_path }
        if ($payload.prompt_id)       { $PromptId = [string]$payload.prompt_id }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}

$ScriptDir = $PSScriptRoot
# v0.2.29: prefer canonical $CLAUDE_PROJECT_DIR (the active workspace
# the launcher hands us). Fall back to SCRIPT_DIR/../.. for ad-hoc
# invocations.
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# v0.2.70 Streams C+E: shared helpers (canonical session-id, unified seen-store
# for the reads ledgers written in the Read branch below; v0.2.101 §C2/§C6
# retired this hook's code-graph INJECTION branches — read-context-inject /
# grep-context-inject are their homes now).
$SessionIdLib = Join-Path $LibDir "session-id.ps1"
if (Test-Path $SessionIdLib) { . $SessionIdLib }
$SeenStoreLib = Join-Path $LibDir "seen-store.ps1"
if (Test-Path $SeenStoreLib) { . $SeenStoreLib }
# (v0.2.101 wave-3: the query-cache.ps1 sourcing was retired with section 5 --
# its last caller. The lib itself was deleted; the router owns caching now.)
$script:ProjectRoot = $ProjectRoot

# v0.2.70 Stream E: unify session-id (parse+sanitise) with the other hooks.
# $SessionIdRaw preserves the trustworthy-vs-untrustworthy distinction for the
# unified reads store the injectors consult. $SessionId keeps the date fallback
# so the Build Anchor reads_*.txt still has a stable per-hour key.
if (Get-Command Get-VcoHookSessionId -ErrorAction SilentlyContinue) {
    $SessionIdRaw = Get-VcoHookSessionId -Stdin $HookStdin
} else {
    $SessionIdRaw = ($SessionIdFromStdin -replace '[^A-Za-z0-9_-]', '')
}
$SessionId = if ($SessionIdFromStdin) { $SessionIdFromStdin } elseif ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { (Get-Date).ToString("yyyyMMdd_HH") }
# v0.2.102 W6-C1 (F11): the Build-Anchor reads ledger is per AGENT. The
# payload's $AgentId (parsed above) is set exactly when this tool call
# belongs to a SUBAGENT -- whose context never saw the parent's Reads (and
# vice versa), so sharing reads_<sid>.txt let a subagent overwrite with
# Write a file only the parent Read (the gate failed open across agents).
# The composite key comes from the ONE session-id helper; the main agent's
# key is byte-identical to the pre-W6-C1 name (this branch only adds the
# __<agent_id> component when $AgentId is non-empty). MUST MATCH pre-tool-use.sh.
$AnchorSessionKey = $SessionId
if ($AgentId -and (Get-Command Get-VcoAgentKeyFromIds -ErrorAction SilentlyContinue)) {
    $AnchorSessionKey = Get-VcoAgentKeyFromIds -SessionId $SessionId -AgentId $AgentId
}
# Per-session dedup state lives under the project's .claude/state/ rather
# than $env:TMPDIR / $env:TEMP so it survives reboots + launcher restarts
# (Claude Code persists session_id across restarts via the resume feature).
# Windows TEMP may be cleared on reboot too. .claude/state/ is gitignored
# and wiped only by PostCompact (correct semantic — context truly resets
# at compaction).
$SessionStateDir = Join-Path $ProjectRoot ".claude/state"
$SessionReadsFile = Join-Path $SessionStateDir "reads_$AnchorSessionKey.txt"
# v0.2.104: written by post-compact (same key as the reads ledger it wipes)
# when THIS session/agent has compacted. Its absence means nothing was
# re-attached, so a Write miss blocks without asking compact_reattach.
# MUST MATCH post-compact.{sh,ps1} + pre-tool-use.sh.
$SessionCompactedMarker = Join-Path $SessionStateDir "compacted_$AnchorSessionKey.txt"
$BackupDir = Join-Path $SessionStateDir "tool_backups"
$SecurityLog = Join-Path $ProjectRoot ".claude/logs/security_events.jsonl"

$LogsDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}
New-Item -ItemType Directory -Force -Path $SessionStateDir -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Force -Path $BackupDir -ErrorAction SilentlyContinue | Out-Null
# Security-log timestamp (UTC, second precision) for every line this run
# writes. MUST MATCH pre-tool-use.sh's `date -u +%Y-%m-%dT%H:%M:%SZ`.
$ts = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", [Globalization.CultureInfo]::InvariantCulture)

function Write-SecurityLine([string]$json) {
    try { Add-Content -Path $SecurityLog -Value $json -ErrorAction Stop } catch { }
}

# === 0. INCOMPLETE-RUN WARNING (v0.2.104) ===================================
# A PreToolUse hook the harness kills on its `timeout` (or that crashes) does
# NOT block: the tool call proceeds and this hook's output is discarded, so
# the Build Anchor, SSRF guard and shell-injection scan silently did not
# apply to that call. Owner ruling 2026-10-18: no `onFailure: "block"` --
# tell the model instead. A killed hook cannot speak, so the NEXT run does:
#   * every run leaves an in-progress marker
#       ptu_inflight_<key>.<pid>.<start-epoch-ms>
#     keyed like the Build-Anchor reads ledger (session + agent), holding
#     only tool names (never command text -- it may carry secrets);
#   * Exit-PtuHook removes it on every normal exit (allow AND block), unless
#     the run ended past the harness timeout (its answer was discarded) or
#     with an exit code the harness treats as a hook error;
#   * a marker of the same key left by a PREVIOUS run is stale when its
#     owner is gone (pid + start-time check, _lib/pid-live.ps1) or it is
#     older than the timeout plus a margin -- never while a concurrent
#     sibling run (parallel tool calls) is still working. A stale marker is
#     claimed by an atomic delete (one reporter even under concurrency),
#     logged to the security log, and reported through this run's answer:
#     the additionalContext envelope on an allow, stderr on a block (the two
#     channels Claude Code shows the model). Its tool names are copied into
#     this run's own marker first, so a report lost to THIS run being killed
#     is carried to the next one.
# The start stamp is the PROCESS start when the OS reports it (the
# powershell cold start counts against the harness timeout too).
# "In time" (Exit-PtuHook) errs toward CLEARING: a run that completed
# normally keeps its marker only when its elapsed time (from the process
# start, else from the hook's first line) reaches the budget. What holds
# exactly: a run whose own clock reads under the budget always clears its
# marker, and a run the harness killed never gets to clear it. Grey zone, a
# few ms wide on both sides of the budget: the harness counts from the
# spawn, slightly before the process start, and its kill timer can also fire
# late (event-loop latency). So a run finishing just under the budget by its
# own clock may have been killed yet clears its marker (left unreported),
# and a run finishing just over it may have had its answer accepted yet
# keeps its marker (a warning for a call whose checks did apply). Outside
# that band the marker is exact. MUST MATCH pre-tool-use.sh.
# A marker of ANOTHER key older than a day belongs to a session or subagent
# that never made another call (a kill on its last call): the GC below logs
# it as `session_ended` before deleting it.
# MUST MATCH pre-tool-use.sh's section 0.
$PtuStaleMarginMs = 2000
$PtuGcAgeMs = 86400000
$PtuCarryMax = 20
$script:PtuBudgetS = Get-PtuBudgetS
$script:PtuMarker = ""
$script:PtuPriorMsg = ""
$ptuKey = [string]$AnchorSessionKey
if (-not $ptuKey -or $ptuKey -match '[^A-Za-z0-9_-]') { $ptuKey = "default" }
$ptuTool = ([string]$ToolName) -replace '[^A-Za-z0-9_.:-]', ''
if ($ptuTool.Length -gt 100) { $ptuTool = $ptuTool.Substring(0, 100) }
if (-not $ptuTool) { $ptuTool = "unknown" }
$ptuT0 = if ($PtuProcStart) { $PtuProcStart } else { [DateTime]::Now.AddMilliseconds(-$PtuClock.Elapsed.TotalMilliseconds) }
$ptuT0Ms = ([DateTimeOffset]$ptuT0).ToUnixTimeMilliseconds()
# .NET calls over cmdlets throughout this section: it runs on every tool
# call, and each cmdlet's first use costs PowerShell milliseconds.
if ([IO.Directory]::Exists($SessionStateDir)) {
    $m = [IO.Path]::Combine($SessionStateDir, ("ptu_inflight_{0}.{1}.{2}" -f $ptuKey, $PID, $ptuT0Ms))
    try {
        [IO.File]::WriteAllText($m, "$ptuTool`n")
        $script:PtuMarker = $m
    } catch { }
}
if ($script:PtuMarker) {
    $priorTools = [System.Collections.Generic.List[string]]::new()
    $prefix = "ptu_inflight_$ptuKey."
    # .NET enumeration, not Get-ChildItem: this runs on every tool call. The
    # key holds no search-pattern wildcard ([A-Za-z0-9_-]); the exact
    # prefix is re-checked ordinally below all the same.
    $candidates = @()
    try { $candidates = @([IO.Directory]::GetFiles($SessionStateDir, "$prefix*")) } catch { }
    $pidLiveLoaded = $false
    foreach ($cPath in $candidates) {
        if ($cPath -eq $script:PtuMarker) { continue }
        $cName = [IO.Path]::GetFileName($cPath)
        if (-not $cName.StartsWith($prefix, [StringComparison]::Ordinal)) { continue }
        if ($cName.Substring($prefix.Length) -notmatch '^([0-9]+)\.([0-9]+)$') { continue }
        $mPid = 0
        $mMs = [long]0
        if (-not [int]::TryParse($Matches[1], [ref]$mPid)) { continue }
        if (-not [long]::TryParse($Matches[2], [ref]$mMs)) { continue }
        $ageMs = $ptuT0Ms - $mMs
        $why = ""
        if ($ageMs -gt ($script:PtuBudgetS * 1000 + $PtuStaleMarginMs)) {
            $why = "overdue"
        } else {
            # Loaded only when a same-key marker exists (rare): not on the
            # every-tool-call path. Missing lib -> treated as live (no warn).
            if (-not $pidLiveLoaded) {
                $pidLiveLoaded = $true
                $pidLiveLib = Join-Path $LibDir "pid-live.ps1"
                if (Test-Path -LiteralPath $pidLiveLib) { . $pidLiveLib }
            }
            if (Get-Command Test-VcoPidLive -ErrorAction SilentlyContinue) {
                $stamp = [DateTimeOffset]::FromUnixTimeMilliseconds($mMs).LocalDateTime
                if (-not (Test-VcoPidLive -OwnerPid $mPid -Stamp $stamp)) { $why = "owner_gone" }
            }
        }
        if (-not $why) { continue }   # a sibling still at work: leave it
        $tools = @()
        try {
            foreach ($t in [IO.File]::ReadAllLines($cPath)) {
                $t = $t -replace '[^A-Za-z0-9_.:-]', ''
                if ($t.Length -gt 100) { $t = $t.Substring(0, 100) }
                if ($t) { $tools += $t }
            }
        } catch { }
        # The delete IS the claim: of two concurrent runs, one reports.
        try { Remove-Item -LiteralPath $cPath -Force -ErrorAction Stop } catch { continue }
        if ($tools.Count -eq 0) { $tools = @("unknown") }
        foreach ($t in $tools) {
            # Bounded: at most $PtuCarryMax names are reported and carried,
            # so a run of killed hooks cannot grow a marker without limit.
            if ($priorTools.Count -ge $PtuCarryMax) { break }
            $priorTools.Add($t)
            try { [IO.File]::AppendAllText($script:PtuMarker, "$t`n") } catch { }
        }
        Write-SecurityLine ("{{""timestamp"":""{0}"",""event"":""pre_tool_use_incomplete"",""tools"":""{1}"",""session_key"":""{2}"",""age_ms"":{3},""reason"":""{4}""}}" -f $ts, ($tools -join ' '), $ptuKey, $ageMs, $why)
    }
    if ($priorTools.Count -gt 0) {
        $script:PtuPriorMsg = "[VCO safety check incomplete] VCO's pre-tool-use safety check did not finish for $($priorTools.Count) earlier tool call(s) in this session ($($priorTools -join ', ')): it timed out or crashed, so its checks (Build Anchor, SSRF guard, shell-injection scan) did not apply to those calls. If one of them wrote or ran something risky, review it."
    }
}

# Exit-PtuHook <code>: every exit of this hook from here on. Removes this
# run's marker when the run completed in time with an answer the harness
# acts on (0 allow, 2 block), and delivers a pending stale-marker report on
# that answer's channel -- together with any queued broken-install notice
# in the ONE additionalContext envelope of an allow (a second envelope on
# stdout would break the hook's JSON contract). MUST MATCH pre-tool-use.sh's
# _ptu_exit.
function Exit-PtuHook([int]$Code) {
    $prior = ""
    if ($script:PtuMarker -and ($Code -eq 0 -or $Code -eq 2)) {
        # Not late = cleared (MUST MATCH pre-tool-use.sh's _ptu_finished_late;
        # this clock is never coarse: Stopwatch + process start).
        if ((Get-PtuElapsedMs) -lt ($script:PtuBudgetS * 1000)) {
            try { [IO.File]::Delete($script:PtuMarker) } catch { }
            $prior = $script:PtuPriorMsg
        }
    }
    if ($Code -eq 0) {
        $parts = @(@($script:VcoModelNotice, $prior) | Where-Object { $_ })
        if ($parts.Count -gt 0) {
            $ctx = $parts -join "`n`n"
            if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
                Emit-AdditionalContext $ctx 'PreToolUse'
            } else {
                $envelope = [ordered]@{ hookSpecificOutput = [ordered]@{ hookEventName = 'PreToolUse'; additionalContext = $ctx } }
                Write-Output ($envelope | ConvertTo-Json -Compress -Depth 4)
            }
        }
    } elseif ($Code -eq 2 -and $prior) {
        [Console]::Error.WriteLine($prior)
    }
    exit $Code
}

# Best-effort 14-day GC of stale per-session reads files. Sessions that
# haven't been touched in two weeks are almost certainly abandoned;
# keeping them around just wastes inodes. Errors suppressed: housekeeping
# pass, not a correctness step.
# HK-4 (v0.2.75) accepted-scatter: one of 4 per-hook GC sweeps (uniform 14d);
# a shared sweeper is optional and deliberately SKIPPED to keep hooks
# single-file. MUST MATCH the .sh sibling. See pre-edit-context-inject.ps1.
# v0.2.102 W6-C1 (F1/F11): the per-agent names reads_<sid>__<agent>.txt and
# seen_reads_<sid>__<agent>.txt are already covered by these two filters
# (the wildcard spans the '__<agent_id>' component), so the GC needs no new
# pattern.
try {
    Get-ChildItem -Path $SessionStateDir -Filter 'reads_*.txt' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # v0.2.70 Stream E (SF-1): same GC for the INJECTOR reads store.
    Get-ChildItem -Path $SessionStateDir -Filter 'seen_reads_*.txt' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # v0.2.104: same GC for the post-compact markers (compacted_*.txt).
    Get-ChildItem -Path $SessionStateDir -Filter 'compacted_*.txt' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # v0.2.104: in-progress markers (ptu_inflight_*, section 0) of any key
    # whose start stamp is over a day old: no later run of that session /
    # subagent came to claim it, so it is LOGGED (reason session_ended, tools
    # from the file) and deleted -- R13 LOW-1, a killed hook on a session's
    # last call must still leave its audit line. Age from the file name's
    # start stamp. MUST MATCH pre-tool-use.sh's GC loop.
    $gcFiles = @()
    try { $gcFiles = @([IO.Directory]::GetFiles($SessionStateDir, "ptu_inflight_*")) } catch { }
    foreach ($g in $gcFiles) {
        $gn = [IO.Path]::GetFileName($g)
        if ($gn -notmatch '^ptu_inflight_([A-Za-z0-9_-]+)\.([0-9]+)\.([0-9]+)$') { continue }
        $gKey = $Matches[1]
        $gMs = [long]0
        if (-not [long]::TryParse($Matches[3], [ref]$gMs)) { continue }
        $gAge = $ptuT0Ms - $gMs
        if ($gAge -le $PtuGcAgeMs) { continue }
        $gTools = @()
        try {
            foreach ($t in [IO.File]::ReadAllLines($g)) {
                $t = $t -replace '[^A-Za-z0-9_.:-]', ''
                if ($t.Length -gt 100) { $t = $t.Substring(0, 100) }
                if ($t) { $gTools += $t }
            }
        } catch { }
        try { Remove-Item -LiteralPath $g -Force -ErrorAction Stop } catch { continue }   # the delete is the claim
        $gToolText = if ($gTools.Count -gt 0) { $gTools -join ' ' } else { 'unknown' }
        Write-SecurityLine ("{{""timestamp"":""{0}"",""event"":""pre_tool_use_incomplete"",""tools"":""{1}"",""session_key"":""{2}"",""age_ms"":{3},""reason"":""session_ended""}}" -f $ts, $gToolText, $gKey, $gAge)
    }
    # Backstop (MUST MATCH the .sh find): a ptu_inflight_* file untouched
    # for two days that the loop could not parse is removed silently.
    Get-ChildItem -Path $SessionStateDir -Filter 'ptu_inflight_*' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-2) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { }

# v0.2.100 WP-17: read a tool_input field NATIVELY from the payload the
# ConvertFrom-Json above already decoded. The old Get-Field piped $ToolArgs
# through a `python -c` child, so with no Python on PATH it returned "" and
# every branch reading a field -- the Bash shell-injection scan above all --
# was silently skipped on Windows (and under Windows PowerShell 5.1 the pipe's
# ASCII $OutputEncoding turned non-ASCII into `?`). No interpreter is needed
# to read a decoded object. MUST MATCH pre-tool-use.sh's `_get_field` (a
# missing / null field is "", a value is its string form, trimmed).
function Get-Field([string]$field) {
    if (-not $payload -or -not $payload.tool_input) { return "" }
    try {
        $value = $payload.tool_input.$field
        if ($null -eq $value) { return "" }
        return ([string]$value).Trim()
    } catch { }
    return ""
}

# v0.2.100 WP-17: the ONE place a branch that genuinely needs Python says so,
# loudly, instead of skipping itself. Every time it goes to stderr for the
# human; once per session (a sentinel under .claude/state) it is queued in
# $script:VcoModelNotice, which the hook emits as its ONE additionalContext
# envelope at the hook's exit (the retired section-5 gate's successor) -- PreToolUse
# stderr on exit 0 is not shown to the model, and a second envelope on stdout
# would break the hook's JSON contract.
# MUST MATCH pre-tool-use.sh's _vco_report_no_python.
$script:VcoModelNotice = ""
function Write-VcoNoPythonNotice([string]$what) {
    $msg = "[VCO broken install] $what did NOT run: no Python interpreter was found (python / py / python3 on PATH). Put Python 3 on PATH or re-run the orchestrator's install / update (``python install.py --update`` in the orchestrator root, or the launcher's Update), then retry."
    [Console]::Error.WriteLine($msg)
    $key = if ($SessionIdRaw) { $SessionIdRaw } else { "default" }
    $sentinel = Join-Path $SessionStateDir "no_python_notice_$key"
    if (Test-Path -LiteralPath $sentinel) { return }
    try { Set-Content -LiteralPath $sentinel -Value "" -ErrorAction Stop } catch { }
    $script:VcoModelNotice = $msg
}

# === 1. SSRF GUARD ===
if ($ToolName -eq "WebFetch") {
    # The URL comes from the payload ConvertFrom-Json already decoded, NOT
    # from Get-Field: that helper pipes the JSON through a `python -c` child,
    # which returns nothing when no Python is found (the guard would then be
    # skipped — the R18F-08 hole) and, under Windows PowerShell 5.1, pipes
    # with the ASCII $OutputEncoding, turning a non-ASCII host into `?`.
    $url = ""
    if ($payload -and $payload.tool_input -and $null -ne $payload.tool_input.url) {
        $url = ([string]$payload.tool_input.url).Trim()
    }
    if (-not $url) {
        # v0.2.102 NB-H1 (FRR:64): an empty URL here means the payload's url
        # field could not be extracted (malformed tool_input, a parser
        # error) — a WebFetch the guard cannot judge went out UNGUARDED
        # before this arm. A security guard that cannot read its input fails
        # CLOSED (the no-Python arm's contract). MUST MATCH the .sh sibling.
        # Non-WebFetch tools are unaffected (this branch is WebFetch-only).
        [Console]::Error.WriteLine("SSRF guard: the WebFetch payload's 'url' field could not be extracted (malformed payload or a parser error), so the call cannot be checked and is blocked.")
        [Console]::Error.WriteLine("   Retry; if it persists this is a broken VCO install - re-run the orchestrator's update (``python install.py --update`` in the orchestrator root, or the launcher's Update).")
        Write-SecurityLine "{""timestamp"":""$ts"",""event"":""ssrf_blocked"",""url"":"""",""reason"":""url_extraction_failed""}"
        Exit-PtuHook 2
    }
    if ($url) {
        # Allowed local services (Weaviate, Ollama, code-embed, vct-hub, :8082,
        # Gradio). SearXNG (:8888) and the mcp__search__fetch_page tool both
        # removed in v0.2.11 (see PR-14a); the search MCP itself was deleted
        # in v0.2.101, so nothing else reaches the network outside WebFetch.
        # v0.2.100: the decision is `python -m vco_lib.ssrf_url` (one
        # implementation for every OS; its docstring is the contract), run
        # once through _lib/ssrf-allowlist.ps1. MUST MATCH the .sh sibling.
        # The allowed pairs are DERIVED from the projected env, so a moved
        # service_endpoints port is allowed and nothing asks the user to
        # hand-edit this hook. FAIL CLOSED: only the exact words `allow` /
        # `pass` let the call through. The lib missing (partial install), no
        # interpreter, vco_lib not importable, or any other output blocks,
        # and says why.
        $ssrfLib = Join-Path $LibDir "ssrf-allowlist.ps1"
        $ssrfVerdict = ""
        $ssrfPairs = ""
        $ssrfWhy = "hooks/_lib/ssrf-allowlist.ps1 is missing - run the bundle update to restore it"
        if (Test-Path -LiteralPath $ssrfLib) {
            . $ssrfLib
            $ssrfCheck = Invoke-VcoSsrfCheck -Url $url -HooksDir $ScriptDir
            $ssrfVerdict = [string]$ssrfCheck.Verdict
            $ssrfPairs = [string]$ssrfCheck.Pairs
            $ssrfErr = if ($ssrfCheck.Error) { [string]$ssrfCheck.Error } else { "unrecognised answer '$ssrfVerdict'" }
            $ssrfWhy = "the guard could not run ($ssrfErr) - a broken VCO install: re-run the orchestrator's update (``python install.py --update`` in the orchestrator root, or the launcher's Update), which reinstalls vco_lib into the VCO venv"
        }
        if ($ssrfVerdict -cne "allow" -and $ssrfVerdict -cne "pass") {
            # Route the block message to STDERR (matches pre-tool-use.sh).
            # Claude Code's PreToolUse runner discards plain stdout - an
            # exit-2 hook with only-stdout renders as "hook error: No
            # stderr output". [Console]::Error.WriteLine goes to the true
            # stderr stream (Write-Output / the PS error stream would not).
            if ($ssrfVerdict -ceq "block") {
                if (-not $ssrfPairs) { $ssrfPairs = "none" }
                [Console]::Error.WriteLine("SSRF guard: '$url' targets a private/internal network address (or one the guard cannot read).")
                [Console]::Error.WriteLine("   Allowed local services on this machine: $ssrfPairs")
                [Console]::Error.WriteLine("   They follow WEAVIATE_URL / OLLAMA_URL / CODE_EMBED_SERVICE_URL and the hub port - a moved service is")
                [Console]::Error.WriteLine("   changed with the launcher's Services page or ``python -m vco_lib.service_endpoints move``, never by editing this hook.")
            } else {
                [Console]::Error.WriteLine("SSRF guard: '$url' was blocked because $ssrfWhy.")
            }
            $urlEsc = $url -replace '\\', '\\\\' -replace '"', '\"'
            Write-SecurityLine "{""timestamp"":""$ts"",""event"":""ssrf_blocked"",""url"":""$urlEsc""}"
            Exit-PtuHook 2
        }
    }
}

# === 2. SHELL INJECTION SCAN (Bash, and the PowerShell tool -- v0.2.102 W5-D:
# the only shell tool on Windows without Git Bash; the scanner's
# `--shell powershell` rule set reads its syntax) ===
if ($ToolName -eq "Bash" -or $ToolName -eq "PowerShell") {
    $cmd = Get-Field "command"
    $SecurityScript = Join-Path $ProjectRoot ".claude/scripts/bash_security.py"

    # v0.2.104 R6: every Python call below is time-bounded. The harness kills
    # this hook after its 3 s registration and a killed PreToolUse hook does
    # NOT block, so a slow scan must end as "no verdict" while there is still
    # time to exit. Returns @{Exit; Out; Err}, or $null on timeout / any
    # failure (= no verdict). The input is written as UTF-8 bytes, exactly.
    # MUST MATCH pre-tool-use.sh's _ptu_bounded (1 s classifier, 2 s
    # scanner). v0.2.104 R7 N3: ONE deadline covers start-up, the stdin write
    # and the exit, as `timeout` does on the sh side; the write and the exit
    # waits used to get a full budget each (a scanner that never reads a large
    # command took 2x the bound).
    function Invoke-PtuBoundedPython([string]$ArgTail, [string]$InputText, [int]$TimeoutMs) {
        $proc = $null
        $clock = [System.Diagnostics.Stopwatch]::StartNew()
        $left = { [int][Math]::Max(0, $TimeoutMs - $clock.ElapsedMilliseconds) }
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = [string]$PY
            $psi.Arguments = '"' + $SecurityScript + '" ' + $ArgTail
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardInput = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
            $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
            $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
            # v0.2.105 rm guard: the scanner remembers a "retry to confirm"
            # rm per chat session + subagent under .claude/state/rm-confirm/,
            # so it gets the payload's ids and the state dir (always set, so
            # an inherited value never leaks in). MUST MATCH pre-tool-use.sh.
            $psi.EnvironmentVariables["VCO_GUARD_SESSION_ID"] = [string]$SessionIdFromStdin
            $psi.EnvironmentVariables["VCO_GUARD_AGENT_ID"] = [string]$AgentId
            $psi.EnvironmentVariables["VCO_GUARD_STATE_DIR"] = [string]$SessionStateDir
            $proc = [System.Diagnostics.Process]::Start($psi)
            $outTask = $proc.StandardOutput.ReadToEndAsync()
            $errTask = $proc.StandardError.ReadToEndAsync()
            $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($InputText)
            $writeTask = $proc.StandardInput.BaseStream.WriteAsync($bytes, 0, $bytes.Length)
            if ($writeTask.Wait((& $left))) { $proc.StandardInput.Close() }
            if (-not $proc.WaitForExit((& $left))) {
                try { $proc.Kill() } catch { }
                return $null
            }
            $proc.WaitForExit()
            return @{ Exit = $proc.ExitCode; Out = $outTask.Result; Err = $errTask.Result }
        } catch {
            if ($proc) { try { $proc.Kill() } catch { } }
            return $null
        }
    }

    # The three raw-text patterns ALWAYS read the whole command (the v0.2.103
    # guarantee) and run first. v0.2.104 R7 N2: .NET backtracks where GNU
    # grep (pre-tool-use.sh) does not, so the spans are tempered -- the span
    # after a fetch/decode stops at the next one that could itself reach the
    # pipe. Same match set as the plain `[^|]+` / `.*` forms (pinned in
    # tests/test_v02104_scanner_linear_rules.py); 40k `base64 -d f;` took 58 s
    # here before, past the hook's 3 s, and a killed hook checks nothing.
    function Get-PtuRawInjection([string]$Text) {
        if ($Text -match '(?i)(curl|wget)\s(?:(?!(?:curl|wget)\s[^|])[^|])+\|\s*(ba)?sh\b') { return "network fetch piped to shell" }
        if ($Text -match '(?i)eval\s+["\$(]*(curl|wget)') { return "eval + network fetch" }
        if ($Text -match '(?i)base64\s+-d(?:(?!base64\s+-d).)*\|\s*(ba)?sh\b') { return "base64-decoded pipe to shell" }
        return ""
    }
    $injection = Get-PtuRawInjection $cmd

    # v0.2.104 E-3: the ONE exception (MUST MATCH pre-tool-use.sh). A Bash
    # command that bash_security.py positively recognises as a pure DATA
    # here-doc write (`cat > f <<'EOF'`, `tee f <<'EOF'`, a plain
    # `python3 - <<'EOF'` edit script -- the strict allowlist is documented at
    # data_heredoc_raw_text) has its BODY skipped: the patterns re-read only
    # the opener line it hands back, and only when that line is exactly the
    # command's first line. No interpreter, a timeout, any error or any doubt
    # keeps the whole-text verdict. Asked only when a pattern matched.
    if ($injection -and $ToolName -eq "Bash" -and $PY -and (Test-Path $SecurityScript)) {
        $dataRes = Invoke-PtuBoundedPython "--data-heredoc" $cmd 1000
        if ($dataRes -and $dataRes.Exit -eq 0) {
            $opener = [string]$dataRes.Out
            if ($opener -and $cmd.StartsWith($opener + "`n", [System.StringComparison]::Ordinal)) {
                $injection = Get-PtuRawInjection $opener
            }
        }
    }

    if ($injection) {
        # Route the block message to STDERR (matches pre-tool-use.sh) --
        # see the SSRF-guard branch above for why plain stdout is dropped.
        [Console]::Error.WriteLine("Shell injection guard: detected '$injection' in $ToolName command.")
        $preview = if ($cmd.Length -gt 120) { $cmd.Substring(0, 120) } else { $cmd }
        [Console]::Error.WriteLine("   Blocked command preview: $preview")
        [Console]::Error.WriteLine("   This check reads the whole command text. Only a plain heredoc file write (cat > file <<'EOF', tee file <<'EOF') or a plain python edit script (python3 - <<'EOF'), with a quoted delimiter and nothing else in the command, has its body exempted.")
        [Console]::Error.WriteLine("   To write a file whose content mentions this text, use the Write tool or such a heredoc. If the command is intentional, run it manually in a terminal.")
        $previewEsc = ($preview -replace '\\', '\\\\' -replace '"', '\"')
        Write-SecurityLine "{""timestamp"":""$ts"",""event"":""shell_injection_blocked"",""pattern"":""$injection"",""cmd_preview"":""$previewEsc""}"
        Exit-PtuHook 2
    }

    # Extended security scan via bash_security.py if available (bounded: a
    # timeout is no verdict and a small command proceeds -- the raw patterns
    # above already ran on it). It is Python, so with no interpreter it
    # cannot run -- and says so (v0.2.100 WP-17) instead of being skipped in
    # silence.
    if ((Test-Path $SecurityScript) -and -not $PY) {
        Write-VcoNoPythonNotice "The Bash security scanner (.claude/scripts/bash_security.py)"
    }
    # v0.2.104 R10 F1: a command over $PtuScanSizeCeilingBytes that the
    # scanner could not finish in time ($null = timed out / killed) is
    # BLOCKED instead (fail-closed for that size class only): before the 2 s
    # bound, an unbounded scan of a ~1.9-2.7 MB command still delivered its
    # verdict inside the 3 s hook registration. A small command keeps "no
    # verdict = proceeds". MUST MATCH pre-tool-use.sh's
    # PTU_SCAN_SIZE_CEILING_BYTES.
    $PtuScanSizeCeilingBytes = 1048576
    if ((Test-Path $SecurityScript) -and $PY) {
        $secShell = if ($ToolName -eq "PowerShell") { "powershell" } else { "bash" }
        $secRes = Invoke-PtuBoundedPython "--shell $secShell" $cmd 2000
        if ($null -eq $secRes) {
            $cmdBytes = [System.Text.Encoding]::UTF8.GetByteCount([string]$cmd)
            if ($cmdBytes -gt $PtuScanSizeCeilingBytes) {
                # MUST MATCH pre-tool-use.sh's message.
                [Console]::Error.WriteLine("$ToolName security check: this command is $cmdBytes bytes, over the 1 MiB limit, and was too large to check in time.")
                [Console]::Error.WriteLine("   Write large content with the Write tool, or split the command into smaller ones.")
                Write-SecurityLine "{""timestamp"":""$ts"",""event"":""bash_security_too_large_unchecked"",""bytes"":$cmdBytes}"
                Exit-PtuHook 2
            }
        }
        if ($secRes -and $secRes.Exit -eq 2) {
            $secOut = ("" + $secRes.Err + $secRes.Out).TrimEnd()
            # Route to STDERR (matches pre-tool-use.sh's bash-security
            # branch) — same stdout-discard rationale as the SSRF /
            # injection guards above.
            [Console]::Error.WriteLine("$ToolName security scanner blocked this command:")
            [Console]::Error.WriteLine("   $secOut")
            $detail = if ("$secOut".Length -gt 200) { "$secOut".Substring(0,200) } else { "$secOut" }
            $detailEsc = $detail -replace '\\', '\\\\' -replace '"', '\"'
            $cmdPreview = if ($cmd.Length -gt 80) { $cmd.Substring(0,80) } else { $cmd }
            $cmdPreviewEsc = $cmdPreview -replace '\\', '\\\\' -replace '"', '\"'
            Write-SecurityLine "{""timestamp"":""$ts"",""event"":""bash_security_blocked"",""detail"":""$detailEsc"",""cmd_preview"":""$cmdPreviewEsc""}"
            Exit-PtuHook 2
        }
    }
}

# === v0.2.70 Stream C — RETIRED (v0.2.101 §C2/§C6) ============================
# The shared code-graph injection helper (Invoke-CgInject) and its two call
# surfaces — Read(code) and Grep(symbol) — were REMOVED. Their home is the
# router-backed hooks read-context-inject.ps1 (PostToolUse Read) and
# grep-context-inject.ps1 (PreToolUse Grep): exact-symbol lookups, gates,
# seen-store dedupe and the per-turn budget all live in
# claude_mcp_servers/scripts/hook_context_router.py + vco_lib/inject_intent.
# This hook keeps ONLY the PreToolUse concerns that must stay here: the
# security guards, the Build-Anchor reads ledger and the unified seen_reads
# ledger (both written below), file backup, and the §5 Edit/Write KG
# suggestion. MUST MATCH pre-tool-use.sh.

# === 3. BUILD ANCHOR PROTOCOL: track reads (the Read(code) code-graph inject
# moved to read-context-inject.ps1, v0.2.101 §C2) ===
if ($ToolName -eq "Read") {
    $filePath = Get-Field "file_path"
    if ($filePath) {
        # Build Anchor ledger (unchanged path/shape — harness exact-match gate).
        try { Add-Content -Path $SessionReadsFile -Value $filePath -ErrorAction Stop } catch { }
        # v0.2.70 Stream E (SF-1 fix): record a REPO-RELATIVE path into the
        # INJECTOR reads store (seen_reads_<sid>.txt, DISTINCT from the
        # Build-Anchor reads_<sid>.txt) so it matches the producers' repo-relative
        # "| src=<path>" trailer. An absolute ledger entry would NEVER match the
        # exact suppression. The abs->relative conversion is the ONE shared
        # ConvertTo-VcoRepoRelative helper (no inline copy). Skipped when the
        # session id is untrustworthy.
        $relFp = $filePath
        if (Get-Command ConvertTo-VcoRepoRelative -ErrorAction SilentlyContinue) {
            $relFp = ConvertTo-VcoRepoRelative -Path $filePath -ProjectRoot $ProjectRoot
        }
        if (Get-Command Get-VcoSeenStorePath -ErrorAction SilentlyContinue) {
            # v0.2.102 W6-C1 (F1): the injector reads-ledger is per AGENT --
            # a subagent's Read must not mark sources "already in context"
            # for the parent (whose window never saw them), nor be silenced
            # by the parent's Reads. Same composite key as the router's.
            # MUST MATCH pre-tool-use.sh.
            $storeSid = $SessionIdRaw
            if ($AgentId -and (Get-Command Get-VcoAgentKeyFromIds -ErrorAction SilentlyContinue)) {
                $storeSid = Get-VcoAgentKeyFromIds -SessionId $SessionIdRaw -AgentId $AgentId
            }
            $unifiedReads = Get-VcoSeenStorePath -Kind "reads" -SessionId $storeSid -ProjectRoot $ProjectRoot
            if ($unifiedReads) {
                try { Add-Content -Path $unifiedReads -Value $relFp -ErrorAction Stop } catch { }
            }
        }
        # v0.2.101 injection redesign (§C2): the Read(code) code-graph
        # injection branch that used to live here was REMOVED — its home is
        # now the PostToolUse(Read) hook read-context-inject.ps1 (one
        # concern, one home; the kickoff probe measured the old branch as
        # always-killed by its 3 s settings timeout, so it never injected).
        # The ledger writes ABOVE stay: they must happen PreToolUse so
        # same-turn Write-anchor checks and seen-store suppression see them.
        # MUST MATCH pre-tool-use.sh.
    }
    Exit-PtuHook 0
}

# === v0.2.70 Stream C Surface 4 (Grep) — RETIRED (v0.2.101 §C6) ============
# The Grep(symbol) code-graph injection branch was REMOVED: grep-context-
# inject.ps1 (PreToolUse(Grep), router surface `grep`) is its one home now.
# A Grep call falls through to the Write/Edit gate below (no-op for Grep)
# and exits at the §5 tool-name gate. MUST MATCH pre-tool-use.sh.

# === 4. BUILD ANCHOR + FILE BACKUP: Write/Edit checks ===
if ($ToolName -eq "Write" -or $ToolName -eq "Edit") {
    $filePath = Get-Field "file_path"
    if ($filePath) {
        if (Test-Path -LiteralPath $filePath -PathType Leaf) {
            # Existing file: check Build Anchor — WRITE ONLY.
            #
            # MUST MATCH templates/hooks/pre-tool-use.sh (section 4): the anchor
            # gate is enforced for `Write` (blind whole-file overwrite of an
            # unseen file) but NOT for `Edit` (Claude Code's built-in
            # read-before-edit rule already covers it; re-enforcing here was
            # redundant and a false-positive source vs the harness's own
            # file-state tracking). Defer Edit's read-before-edit to the harness.
            if ($ToolName -eq "Write") {
                $alreadyRead = $false
                if (Test-Path $SessionReadsFile) {
                    try {
                        foreach ($l in Get-Content $SessionReadsFile -ErrorAction Stop) {
                            if ($l -eq $filePath) { $alreadyRead = $true; break }
                        }
                    } catch { }
                }
                # v0.2.104: a miss right after a compaction may be a file
                # Claude Code itself re-attached (post-compact wiped the
                # ledger). Ask the ONE home, vco_lib/compact_reattach.py,
                # which reads the exact set from the transcript (it also
                # normalises Windows case and separators); only the exact
                # answer `reattached` allows -- no interpreter, a broken
                # vco_lib, any error or doubt keeps the block (fail closed).
                # The allowed path is appended to the ledger below, so later
                # Writes do not rescan. MUST MATCH pre-tool-use.sh.
                # Asked ONLY when post-compact left this session's marker: a
                # session that never compacted has nothing re-attached, and
                # this hook's 3 s registration timeout must not be spent on a
                # transcript scan -- a hook killed on timeout does not block,
                # so it would turn this block into an allow. The module
                # answers `no` past the deadline this hook hands it.
                if (-not $alreadyRead -and $TranscriptPath -and (Test-Path -LiteralPath $SessionCompactedMarker -PathType Leaf)) {
                    $raPy = ""
                    $venvLib = Join-Path $LibDir "resolve-vco-venv.ps1"
                    if (Test-Path -LiteralPath $venvLib) {
                        try {
                            . $venvLib
                            $raVenv = Resolve-VcoVenvPython -ScriptDir $ScriptDir
                            if ($raVenv -and (Test-Path -LiteralPath $raVenv)) { $raPy = [string]$raVenv }
                        } catch { }
                    }
                    if (-not $raPy -and $PY) { $raPy = [string]$PY }
                    # The check gets only what is left of the registration
                    # timeout: budget - elapsed since this hook started - a
                    # margin for the interpreter's start and this hook's
                    # exit. Under the floor there is no time for an answer:
                    # skip the spawn and block. The module caps the value at
                    # its own 1 s default. VCO_PRE_TOOL_USE_TIMEOUT_S (whole
                    # seconds) MUST MATCH the `timeout` registered for
                    # pre-tool-use in .claude/settings.json -- set it when you
                    # change that registration (parsed by Get-PtuBudgetS).
                    $ptuBudgetS = Get-PtuBudgetS
                    $ptuLeftMs = $ptuBudgetS * 1000 - (Get-PtuElapsedMs) - 700
                    if ($raPy -and $ptuLeftMs -ge 200) {
                        $ptuDeadline = ([double]$ptuLeftMs / 1000).ToString("0.000", [Globalization.CultureInfo]::InvariantCulture)
                        try {
                            $raOut = @(& $raPy -m vco_lib.compact_reattach check "--transcript=$TranscriptPath" "--path=$filePath" "--agent-id=$AgentId" "--deadline=$ptuDeadline" 2>$null)
                            if ($LASTEXITCODE -eq 0 -and $raOut.Count -ge 1 -and ([string]$raOut[0]).Trim() -ceq "reattached") {
                                $alreadyRead = $true
                            }
                        } catch { }
                    }
                }
                if (-not $alreadyRead) {
                    $bn = Split-Path $filePath -Leaf
                    # Route to STDERR (matches pre-tool-use.sh's Build Anchor
                    # branch) — same stdout-discard rationale as the guards
                    # above; an exit-2 hook with only-stdout renders as
                    # "hook error: No stderr output".
                    [Console]::Error.WriteLine("Build Anchor Protocol: '$bn' has not been Read this session.")
                    [Console]::Error.WriteLine("    Use the Read tool on this file before overwriting it with Write.")
                    $fpEsc = $filePath -replace '\\', '\\\\' -replace '"', '\"'
                    Write-SecurityLine "{""timestamp"":""$ts"",""event"":""anchor_blocked"",""file"":""$fpEsc"",""tool"":""Write""}"
                    Exit-PtuHook 2
                }
            }
            # Backup existing file before modification.
            if (-not (Test-Path $BackupDir)) {
                New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
            }
            $stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
            $encoded = ($filePath -replace '[/\\]', '__') -replace ' ', '_'
            try {
                Copy-Item -LiteralPath $filePath -Destination (Join-Path $BackupDir "${stamp}__${encoded}") -Force -ErrorAction Stop
            } catch { }
            # Cleanup backups older than 24h.
            try {
                $cutoff = (Get-Date).AddMinutes(-1440)
                Get-ChildItem -Path $BackupDir -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.LastWriteTime -lt $cutoff } |
                    Remove-Item -Force -ErrorAction SilentlyContinue
            } catch { }
        }
        try { Add-Content -Path $SessionReadsFile -Value $filePath -ErrorAction Stop } catch { }
    }
}

# === 5. KG SEARCH SUGGESTION -- RETIRED (v0.2.101 wave-3, review nit-6) ====
# This branch emitted a "Found N related patterns" KG suggestion on every
# Edit/Write -- the SAME tool calls pre-edit-context-inject.ps1 /
# pre-write-context-inject.ps1 inject gated, deduped, budgeted KG + code-graph
# context for: two context emissions per edit, no score floor on the
# suggestion path. The injection redesign made those wrappers (via
# hook_context_router.py) the ONE home for edit-time KG context (PLAN-V02101
# section 2.1 Edit/Write rows). Retiring this branch also retired the last
# shell caller of _lib/query-cache.ps1, deleted with it (the router keeps its
# own Python cache under the disjoint kgi/cgi namespaces). The
# pre_tool_use_kg_search task type stays registered in rl_kg_search's
# KNOWN_TASK_TYPES for the historical RL corpus.
# MUST MATCH pre-tool-use.sh's retirement tombstone.

# v0.2.100 WP-17 (kept from the retired section-5 tool gate): the Bash
# branch's queued broken-install notice leaves in this tool call's one
# envelope -- Exit-PtuHook emits it (with any section-0 report).
Exit-PtuHook 0
