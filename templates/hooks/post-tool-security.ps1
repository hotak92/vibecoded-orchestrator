# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-tool-security.ps1
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans the text the call INTRODUCED for credentials and notifies.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
$EmitContextLib = Join-Path $PSScriptRoot "_lib/emit-context.ps1"
if (Test-Path $EmitContextLib) { . $EmitContextLib }

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($args) are EMPTY because $CLAUDE_TOOL_ARG_FILE_PATH and
# similar env vars don't exist — settings.json substitutes to "". Verified
# empirically 2026-05-08 via stdin-capture diagnostic.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$EditedFile = ""
# v0.2.76 P5 (hook-latency parity): the bash sibling consolidated its stdin
# parse from FOUR `python -c` spawns into ONE NUL-delimited decode (each
# interpreter cold-start cost ~15ms; this hook fires on every Edit|Write).
# PowerShell already single-decodes here via one ConvertFrom-Json - no
# per-field re-parse ever existed - so the perf issue was bash-only. This
# touch keeps the OS-parity modification gate satisfied.
# V52-L.2 Fix 2a: parse subagent identity + session_id so credential_
# alerts.jsonl rows carry enough context to be attributed to the agent
# that triggered the write. Empty string when absent (parent context).
$AgentId = ""
$AgentType = ""
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.file_path) {
        $EditedFile = [string]$payload.tool_input.file_path
    }
    if ($payload) {
        if ($payload.agent_id)   { $AgentId   = [string]$payload.agent_id }
        if ($payload.agent_type) { $AgentType = [string]$payload.agent_type }
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}

$ScriptDir = $PSScriptRoot
# v0.2.52 V52-L.2: prefer canonical $CLAUDE_PROJECT_DIR (active workspace
# handed in by the launcher) over the SCRIPT_DIR/../.. fallback. Aligns
# with pre-tool-use.ps1 and post-file-edit.ps1.
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$AlertLog = Join-Path $ProjectRoot ".claude/logs/credential_alerts.jsonl"
$AlertDir = Split-Path $AlertLog -Parent
if (-not (Test-Path $AlertDir)) {
    New-Item -ItemType Directory -Path $AlertDir -Force | Out-Null
}

if (-not $EditedFile -or -not (Test-Path -LiteralPath $EditedFile -PathType Leaf)) { exit 0 }

# WHAT IS SCANNED (v0.2.102 W6-N, owner ruling 2026-10-12): only what THIS
# call introduced -- Edit `new_string`, Write
# `content` -- never the whole file. Scanning the whole file re-alerted on
# every unrelated edit of a file that already held a test placeholder (the
# `ghp_...` fixtures in secrets.rs). A line carrying the explicit marker
# `vco-secret-scan: test-placeholder` is a declared placeholder; nothing is
# inferred from how a value looks. Findings name the project-relative path,
# the line(s) and the shape label -- never the value. Bash-written files are
# not covered: this hook is registered for Edit|Write only.
#
# ONE implementation, shared with the .sh sibling:
# .claude/scripts/credscan_introduced.py (resolved from this hook's own
# bundle, ../scripts, then $ProjectRoot/.claude/scripts). It owns the scope
# and the marker test. There is NO length / body floor: a PEM
# `BEGIN ... PRIVATE KEY` line alerts whatever follows it (owner ruling
# 2026-10-12: a secret may be short). Every vocabulary match is a finding
# unless its own line carries the marker.
#
# PATTERN SOURCE (read this before adding a regex): the shapes are NOT
# defined here. Both siblings read the SAME vocabulary from _lib/credshapes
# ('content_scan' context), whose SSOT is vco_lib/credential_shapes.py, and
# hand it to the scan helper; the test-placeholder marker comes from the same
# mirror (Get-CredShapesTestPlaceholderMarker). Add shapes to the SSOT, never
# here.
#
# `Hook leak-test marker` (VCT_HOOK_LEAK_PROBE_a3f7c2) is a smoke-test
# marker - keeps fixture credentials non-real-looking; users seeing this alert
# know it's a test, not a real leak. It ends in a 6-char random hex so it
# cannot accidentally appear in docs or prose.
$CredShapesLib = Join-Path $LibDir "credshapes.ps1"
if (Test-Path -LiteralPath $CredShapesLib -PathType Leaf) { . $CredShapesLib }

$content = $null
try { $content = Get-Content -LiteralPath $EditedFile -Raw -ErrorAction Stop } catch { exit 0 }

$ScanHelper = $null
foreach ($cand in @((Join-Path $ScriptDir "../scripts/credscan_introduced.py"),
                    (Join-Path $ProjectRoot ".claude/scripts/credscan_introduced.py"))) {
    if (Test-Path -LiteralPath $cand -PathType Leaf) { $ScanHelper = $cand; break }
}

$alerts = @()
$relFile = $EditedFile
$summary = ""
$findings = @()

if (-not (Get-Command Get-CredShapes -ErrorAction SilentlyContinue) -or
    -not (Get-Command Get-CredShapesTestPlaceholderMarker -ErrorAction SilentlyContinue)) {
    # A MISSING vocabulary must not look like a clean file: downstream reads an
    # empty $alerts as "nothing found", so a silent degrade would turn a broken
    # install into a permanent all-clear. Raise it as a real alert instead.
    $alerts += "credential scanner UNAVAILABLE (_lib/credshapes.ps1 missing)"
} elseif (-not $ScanHelper) {
    $alerts += "credential scanner UNAVAILABLE (scripts/credscan_introduced.py missing)"
} elseif (-not $PY) {
    $alerts += "credential scanner UNAVAILABLE (no Python interpreter)"
} else {
    $spec = [ordered]@{
        marker = (Get-CredShapesTestPlaceholderMarker)
        shapes = @(Get-CredShapes -Context 'content_scan' | ForEach-Object { ,@($_.Id, $_.Label, $_.Re) })
    }
    # The vocabulary rides an environment variable, not argv: Windows
    # PowerShell 5.1 mangles a native argument that holds a double quote, and
    # the generic-secret patterns hold one.
    $env:VCO_CREDSCAN_SHAPES = ($spec | ConvertTo-Json -Compress -Depth 5)
    $verdict = $null
    try {
        $prevEnc = $OutputEncoding
        $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $raw = ($HookStdin | & $PY $ScanHelper --root $ProjectRoot --shapes-env VCO_CREDSCAN_SHAPES --format json 2>$null | Out-String)
        $OutputEncoding = $prevEnc
        if ($LASTEXITCODE -eq 0 -and $raw.Trim()) { $verdict = $raw | ConvertFrom-Json -ErrorAction Stop }
    } catch { $verdict = $null }
    Remove-Item Env:VCO_CREDSCAN_SHAPES -ErrorAction SilentlyContinue
    if (-not $verdict) {
        $alerts += "credential scanner UNAVAILABLE (scripts/credscan_introduced.py failed)"
    } else {
        if ($verdict.path) { $relFile = [string]$verdict.path }
        $findings = @($verdict.findings)
        if ($findings.Count -gt 0) {
            $summary = [string]$verdict.summary
            $alerts += [string]$verdict.patterns
        }
    }
}

if ($alerts.Count -gt 0) {
    # Test-mode: smoke tests set $env:VCT_HOOK_LEAK_PROBE OR include
    # the literal sentinel "VCT_HOOK_LEAK_PROBE_a3f7c2" in the test
    # fixture. Suppress the desktop notify, JSONL log, and model
    # envelope; tests harvest stdout directly. Real production alerts
    # are unaffected. (Sentinel renamed 2026-05-18; see pattern table.)
    $isLeakTest = $false
    if ($env:VCT_HOOK_LEAK_PROBE) { $isLeakTest = $true }
    if (-not $isLeakTest) {
        try {
            if ($content -match 'VCT_HOOK_LEAK_PROBE_a3f7c2') { $isLeakTest = $true }
        } catch { }
    }
    if ($isLeakTest) {
        Write-Output "[leak-test] would-emit alert for: $relFile :: $($alerts -join ' ')"
        exit 0
    }
    # Path + line(s) + shape label; NEVER the value (owner ruling 2026-10-12).
    $detail = if ($summary) { $summary } else { $alerts -join ' ' }
    $msg = "Possible credential in ${relFile}: $detail"
    # Build the JSONL line via ConvertTo-Json so all fields are properly
    # escaped (paths with quotes, future labels with metacharacters, etc.).
    # -Compress keeps it on one line. `findings` = [{line, id, label}] --
    # never a value. Audit fix 2026-05-07.
    $findingRows = @($findings | ForEach-Object {
        [ordered]@{ line = $_.line; id = [string]$_.id; label = [string]$_.label }
    })
    $entry = [ordered]@{
        timestamp  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        file       = $relFile
        patterns   = ($alerts -join ' ')
        findings   = $findingRows
        # V52-L.2 Fix 2a: include session_id + agent_id + agent_type so
        # post-hoc forensics can attribute the credential alert back to
        # the agent that triggered the write.
        session_id = $SessionId
        agent_id   = $AgentId
        agent_type = $AgentType
    }
    $line = $entry | ConvertTo-Json -Compress -Depth 5
    try { Add-Content -Path $AlertLog -Value $line -ErrorAction Stop } catch { }

    # v0.2.82: desktop-toast DEDUP - same (file, findings) key notifies at
    # most once per 6h. JSONL log + model envelope stay per-event (forensics
    # and agent awareness are never rate-limited — only the human toast).
    # MUST MATCH the .sh sibling's dedup (same file, same key derivation).
    $dedupFile = Join-Path $ProjectRoot ".claude/logs/.cred_alert_notify_dedup"
    $ttlSecs = 21600
    $shouldNotify = $true
    $dedupKey = $null
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $keyBytes = [System.Text.Encoding]::UTF8.GetBytes("$relFile|$msg")
        $dedupKey = ([System.BitConverter]::ToString($sha.ComputeHash($keyBytes)) -replace '-', '').Substring(0, 16).ToLower()
    } catch { }
    $nowEpoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    if ($dedupKey -and (Test-Path $dedupFile)) {
        try {
            $lastLine = Select-String -Path $dedupFile -Pattern "^$dedupKey " | Select-Object -Last 1
            if ($lastLine) {
                $lastTs = [int64](($lastLine.Line -split ' ')[1])
                if (($nowEpoch - $lastTs) -lt $ttlSecs) { $shouldNotify = $false }
            }
        } catch { }
    }
    $NotifyScript = Join-Path $ProjectRoot ".claude/scripts/notify.py"
    if ($shouldNotify -and $PY -and (Test-Path $NotifyScript)) {
        try {
            & $PY $NotifyScript "Claude Code Security Alert" $msg `
                --urgency critical --icon dialog-warning 2>$null | Out-Null
        } catch { }
        if ($dedupKey) {
            try { Add-Content -Path $dedupFile -Value "$dedupKey $nowEpoch" -ErrorAction Stop } catch { }
        }
    }
    # Surface the alert to the model via the PostToolUse JSON envelope
    # (`hookSpecificOutput.additionalContext`). Plain stdout from
    # PostToolUse hooks is silently dropped — the desktop notification
    # above reaches the user, but without this envelope path the model
    # never saw credential alerts on files it had just written.
    # The marker text comes from the vocabulary mirror (one home), never a
    # literal here.
    $markerText = ""
    if (Get-Command Get-CredShapesTestPlaceholderMarker -ErrorAction SilentlyContinue) {
        $markerText = Get-CredShapesTestPlaceholderMarker
    }
    $reminder = "[Security alert] $msg`nReview the lines named above before continuing: the credential pattern is in`ntext this edit introduced (or on a line it changed) in $relFile. If it is`na deliberate test placeholder, mark its line with the comment`n``$markerText``; if it leaked from real input, redact`nit before any further git operations or external sharing."
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $reminder PostToolUse
    }
}
exit 0
