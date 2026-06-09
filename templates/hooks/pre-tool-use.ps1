# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: read-side delegator on the KG-suggestion path (PR #171 / 0.1.7).
#   The "KG search suggestion" branch (Edit/Write only, see section 5
#   below) calls .claude/scripts/kg-search.ps1 (or kg-search via bash);
#   that wrapper invokes search_knowledge.py which honors
#   VCT_KG_ACCESS_LIST through the shared helper. Other branches (SSRF
#   guard, shell-injection scan, Build Anchor, file backup, tool logging)
#   do not touch KG/codegraph. Env propagation: & / Start-Process
#   inherit env by default. No centralization needed in this hook itself.

# pre-tool-use.ps1
# Pre-tool-use hook: SSRF guard, shell injection scan, tool logging,
# Build Anchor Protocol, file backup, KG search suggestion.

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
# 2026-05-08 via stdin-capture diagnostic. Without this, every toucan log
# entry is {"query":"","chosen_tool":"","tool_args":null} — silently broken.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
$ToolArgs = ""
$UserMessage = ""
$SessionIdFromStdin = ""
# V52-L.2 Fix 1: parse subagent identity from stdin payload. Per A5
# audit, PreToolUse hooks DO fire for subagent tool calls and the
# payload carries agent_id + agent_type so handlers can differentiate.
# Empty string when absent (parent context) — TOUCAN consumers expect
# the field to always be present.
$AgentId = ""
$AgentType = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)    { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_input)   { $ToolArgs = ($payload.tool_input | ConvertTo-Json -Compress -Depth 8) }
        if ($payload.user_message) { $UserMessage = [string]$payload.user_message }
        if ($payload.session_id)   { $SessionIdFromStdin = [string]$payload.session_id }
        if ($payload.agent_id)     { $AgentId = [string]$payload.agent_id }
        if ($payload.agent_type)   { $AgentType = [string]$payload.agent_type }
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

$SessionId = if ($SessionIdFromStdin) { $SessionIdFromStdin } elseif ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { (Get-Date).ToString("yyyyMMdd_HH") }
# Per-session dedup state lives under the project's .claude/state/ rather
# than $env:TMPDIR / $env:TEMP so it survives reboots + launcher restarts
# (Claude Code persists session_id across restarts via the resume feature).
# Windows TEMP may be cleared on reboot too. .claude/state/ is gitignored
# and wiped only by PostCompact (correct semantic — context truly resets
# at compaction).
$SessionStateDir = Join-Path $ProjectRoot ".claude/state"
$SessionReadsFile = Join-Path $SessionStateDir "reads_$SessionId.txt"
$BackupDir = Join-Path $SessionStateDir "tool_backups"
$SecurityLog = Join-Path $ProjectRoot ".claude/logs/security_events.jsonl"

$LogsDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}
New-Item -ItemType Directory -Force -Path $SessionStateDir -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Force -Path $BackupDir -ErrorAction SilentlyContinue | Out-Null

# Best-effort 14-day GC of stale per-session reads files. Sessions that
# haven't been touched in two weeks are almost certainly abandoned;
# keeping them around just wastes inodes. Errors suppressed: housekeeping
# pass, not a correctness step.
try {
    Get-ChildItem -Path $SessionStateDir -Filter 'reads_*.txt' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { }

function Get-Field([string]$field) {
    if (-not $PY) { return "" }
    if (-not $ToolArgs) { return "" }
    try {
        $code = "import sys, json`ntry:`n    d = json.loads(sys.stdin.read())`n    print(d.get('$field', ''))`nexcept Exception:`n    print('')"
        $result = $ToolArgs | & $PY -c $code 2>$null
        if ($result) { return $result.Trim() }
    } catch { }
    return ""
}

function Write-SecurityLine([string]$json) {
    try { Add-Content -Path $SecurityLog -Value $json -ErrorAction Stop } catch { }
}

# === Tool call logging ===
# UserMessage may contain newlines and metacharacters that the previous
# manual escape (only \ and ") didn't cover. Use ConvertTo-Json so every
# field round-trips correctly. ToolArgs is parsed when possible to keep
# structured logging; falls back to string on parse failure.
# Audit fix 2026-05-07.
$ToucanLog = Join-Path $ProjectRoot ".claude/logs/toucan_dataset.jsonl"
$toolArgsVal = $null
if ($ToolArgs) {
    try { $toolArgsVal = $ToolArgs | ConvertFrom-Json -ErrorAction Stop } catch { $toolArgsVal = $ToolArgs }
}
$entry = [ordered]@{
    timestamp   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    query       = $UserMessage
    chosen_tool = $ToolName
    tool_args   = $toolArgsVal
    # V52-L.2 Fix 1: include agent_id / agent_type / session_id so TOUCAN
    # consumers can differentiate parent vs subagent rows. Empty string
    # when absent (parent context).
    session_id  = $SessionId
    agent_id    = $AgentId
    agent_type  = $AgentType
}
$line = $entry | ConvertTo-Json -Compress -Depth 8
try { Add-Content -Path $ToucanLog -Value $line -ErrorAction Stop } catch { }

# === 1. SSRF GUARD ===
if ($ToolName -eq "WebFetch") {
    $url = Get-Field "url"
    if ($url) {
        # Whitelisted local services (Weaviate, Ollama, code-embed, Gradio).
        # SearXNG (:8888) and the mcp__search__fetch_page tool both
        # removed in v0.2.11 (see PR-14a). Search MCP now exposes only
        # `search_papers` which uses OpenAlex+arXiv HTTP directly — its
        # outbound HTTP doesn't go through this WebFetch SSRF guard.
        $whitelisted = $url -match '(localhost:(8081|8082|11435|11440|7860)|127\.0\.0\.1:(8081|8082|11435|11440|7860))'
        if (-not $whitelisted -and $url -match '(localhost|127\.|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2[0-9]|3[01])\.\d+\.|192\.168\.\d+\.|169\.254\.\d+\.|0\.0\.0\.0|::1)') {
            Write-Output "SSRF guard: '$url' targets a private/internal network address."
            Write-Output "   Whitelisted localhost services: Weaviate (:8081), Ollama (:11435), code-embed (:11440), Gradio (:7860)"
            Write-Output "   To allow additional services, add to whitelist in .claude/hooks/pre-tool-use.ps1"
            $urlEsc = $url -replace '\\', '\\\\' -replace '"', '\"'
            Write-SecurityLine "{""timestamp"":""$ts"",""event"":""ssrf_blocked"",""url"":""$urlEsc""}"
            exit 2
        }
    }
}

# === 2. SHELL INJECTION SCAN ===
if ($ToolName -eq "Bash") {
    $cmd = Get-Field "command"
    $injection = ""
    if ($cmd -match '(?i)(curl|wget)\s[^|]+\|\s*(ba)?sh\b') { $injection = "network fetch piped to shell" }
    elseif ($cmd -match '(?i)eval\s+["\$(]*(curl|wget)') { $injection = "eval + network fetch" }
    elseif ($cmd -match '(?i)base64\s+-d.*\|\s*(ba)?sh\b') { $injection = "base64-decoded pipe to shell" }

    if ($injection) {
        Write-Output "Shell injection guard: detected '$injection' in Bash command."
        $preview = if ($cmd.Length -gt 120) { $cmd.Substring(0, 120) } else { $cmd }
        Write-Output "   Blocked command preview: $preview"
        Write-Output "   If this is intentional, run the command manually in a terminal."
        $previewEsc = ($preview -replace '\\', '\\\\' -replace '"', '\"')
        Write-SecurityLine "{""timestamp"":""$ts"",""event"":""shell_injection_blocked"",""pattern"":""$injection"",""cmd_preview"":""$previewEsc""}"
        exit 2
    }

    # Extended security scan via bash_security.py if available.
    $SecurityScript = Join-Path $ProjectRoot ".claude/scripts/bash_security.py"
    if ((Test-Path $SecurityScript) -and $PY) {
        try {
            $secOut = $cmd | & $PY $SecurityScript 2>&1
            $secExit = $LASTEXITCODE
            if ($secExit -eq 2) {
                Write-Output "Bash security scanner blocked this command:"
                Write-Output "   $secOut"
                $detail = if ("$secOut".Length -gt 200) { "$secOut".Substring(0,200) } else { "$secOut" }
                $detailEsc = $detail -replace '\\', '\\\\' -replace '"', '\"'
                $cmdPreview = if ($cmd.Length -gt 80) { $cmd.Substring(0,80) } else { $cmd }
                $cmdPreviewEsc = $cmdPreview -replace '\\', '\\\\' -replace '"', '\"'
                Write-SecurityLine "{""timestamp"":""$ts"",""event"":""bash_security_blocked"",""detail"":""$detailEsc"",""cmd_preview"":""$cmdPreviewEsc""}"
                exit 2
            }
        } catch { }
    }
}

# === 3. BUILD ANCHOR PROTOCOL: track reads ===
if ($ToolName -eq "Read") {
    $filePath = Get-Field "file_path"
    if ($filePath) {
        try { Add-Content -Path $SessionReadsFile -Value $filePath -ErrorAction Stop } catch { }
    }
    exit 0
}

# === 4. BUILD ANCHOR + FILE BACKUP: Write/Edit checks ===
if ($ToolName -eq "Write" -or $ToolName -eq "Edit") {
    $filePath = Get-Field "file_path"
    if ($filePath) {
        if (Test-Path -LiteralPath $filePath -PathType Leaf) {
            $alreadyRead = $false
            if (Test-Path $SessionReadsFile) {
                try {
                    foreach ($l in Get-Content $SessionReadsFile -ErrorAction Stop) {
                        if ($l -eq $filePath) { $alreadyRead = $true; break }
                    }
                } catch { }
            }
            if (-not $alreadyRead) {
                $bn = Split-Path $filePath -Leaf
                Write-Output "Build Anchor Protocol: '$bn' has not been Read this session."
                Write-Output "    Use the Read tool on this file before modifying it."
                $fpEsc = $filePath -replace '\\', '\\\\' -replace '"', '\"'
                Write-SecurityLine "{""timestamp"":""$ts"",""event"":""anchor_blocked"",""file"":""$fpEsc""}"
                exit 2
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

# === 5. KG SEARCH SUGGESTION (Edit/Write only) ===
if ($ToolName -ne "Edit" -and $ToolName -ne "Write") { exit 0 }

$conceptRe = '(caching|authentication|database|API|search|optimization|validation|testing|deployment|VRAM|quantization|inference|embedding|MCP|agent|workflow|pattern)'
$matchesList = [regex]::Matches($UserMessage, $conceptRe, 'IgnoreCase')
if ($matchesList.Count -lt 1) { exit 0 }
$concepts = ($matchesList | Select-Object -First 3 | ForEach-Object { $_.Value }) -join ' '
if (-not $concepts) { exit 0 }

# V52-J (v0.2.52): switched from kg-search → rl_kg_search.py so this
# hook shares the canonical chokepoint with the pre-edit-context-inject
# hook + the MCP hybrid_search tool. Same Weaviate fan-out, same RL
# rerank, same v3 retrieval-event emit. Pre-V52-J this branch called
# kg-search (search_knowledge.py CLI), which until Edit B produced zero
# telemetry — switching here closes the redundancy at the same time as
# Edit B closes the silent hole.
#
# rl_kg_search.py --hook-format emits headers of the shape
#   "KG: <title> | <node_type> | score=<n.nn> | <body...>"
# Title (not file_path) is what we surface; the pre-edit hook's dedup
# logic also keys on title.
#
# Venv resolution mirrors pre-edit-context-inject.ps1 — uses the shared
# _lib/resolve-vco-venv.ps1 helper so we never accidentally activate the
# USER's project venv (which lacks weaviate-client).
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
$VenvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
$RlScript = Join-Path $ProjectRoot "claude_mcp_servers/scripts/rl_kg_search.py"
$matchOutput = ""
if ($VenvPy -and (Test-Path $VenvPy) -and (Test-Path $RlScript)) {
    try {
        # VCT_SESSION_ID is set into the subprocess env so the canonical
        # 3-layer session_id resolution in telemetry_emit sees the same
        # session as the rest of the hook chain, instead of falling
        # through to the empty CLAUDE_SESSION_ID.
        $prevSessionEnv = $env:VCT_SESSION_ID
        try {
            $env:VCT_SESSION_ID = $SessionId
            $rawOutput = & $VenvPy $RlScript $concepts --limit 3 --hook-format 2>$null
        } finally {
            if ($null -eq $prevSessionEnv) {
                Remove-Item Env:VCT_SESSION_ID -ErrorAction SilentlyContinue
            } else {
                $env:VCT_SESSION_ID = $prevSessionEnv
            }
        }
        # Extract only the per-result HEADER lines (start with "KG: " and
        # carry the " | " separator) — strips body chunks. Filter out the
        # "no-results" sentinel rl_kg_search emits when nothing matched.
        $matchOutput = $rawOutput |
            Where-Object { $_ -like 'KG: *' } |
            Where-Object { $_ -notlike 'KG: no-results*' } |
            Select-Object -First 3
    } catch { }
}

if ($matchOutput) {
    $arr = @($matchOutput)
    if ($arr.Count -ge 2) {
        # PreToolUse hooks must wrap LLM-bound stdout in
        # `hookSpecificOutput.additionalContext` — plain stdout is silently
        # discarded by Claude Code's hook runner. Pre-fork-sweep this
        # branch printed plaintext that never reached the LLM on either
        # OS. Same fix class as pre-edit-context-inject (PR #168). The
        # shared helper in _lib/emit-context.ps1 handles the JSON envelope,
        # the 10k char cap, and (defense-in-depth) the whitespace-only-
        # content guard.
        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("Found $($arr.Count) related patterns for: $concepts")
        foreach ($m in $arr) { [void]$sb.AppendLine("   $m") }
        [void]$sb.AppendLine("")
        [void]$sb.AppendLine("   Search more: 'Search knowledge graph for [concept]'")
        [void]$sb.AppendLine("")
        # Defense: if the helper failed to load (file missing at hook
        # startup), skip emission rather than crash. Other branches of
        # this hook are unaffected.
        if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
            Emit-AdditionalContext $sb.ToString() 'PreToolUse'
        }
    }
}
exit 0
