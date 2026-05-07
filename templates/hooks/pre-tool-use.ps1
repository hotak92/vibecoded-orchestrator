# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# pre-tool-use.ps1
# Pre-tool-use hook: SSRF guard, shell injection scan, tool logging,
# Build Anchor Protocol, file backup, KG search suggestion.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

param(
    [Parameter(Position=0)] [string]$ToolName = "",
    [Parameter(Position=1)] [string]$UserMessage = "",
    [Parameter(Position=2)] [string]$ToolArgs = ""
)

$ScriptDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$SessionId = if ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { (Get-Date).ToString("yyyyMMdd_HH") }
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$SessionReadsFile = Join-Path $Tmp ".claude_reads_$SessionId"
$BackupDir = Join-Path $Tmp ".claude_backups"
$SecurityLog = Join-Path $ProjectRoot ".claude/logs/security_events.jsonl"

$LogsDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}

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
}
$line = $entry | ConvertTo-Json -Compress -Depth 8
try { Add-Content -Path $ToucanLog -Value $line -ErrorAction Stop } catch { }

# === 1. SSRF GUARD ===
if ($ToolName -eq "WebFetch" -or $ToolName -eq "mcp__search__fetch_page") {
    $url = Get-Field "url"
    if ($url) {
        $whitelisted = $url -match '(localhost:(8081|8082|11435|7860|8888)|127\.0\.0\.1:(8081|8082|11435|7860|8888))'
        if (-not $whitelisted -and $url -match '(localhost|127\.|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2[0-9]|3[01])\.\d+\.|192\.168\.\d+\.|169\.254\.\d+\.|0\.0\.0\.0|::1)') {
            Write-Output "SSRF guard: '$url' targets a private/internal network address."
            Write-Output "   Whitelisted localhost services: Weaviate (:8081), Ollama (:11435), SearXNG (:8888), Gradio (:7860)"
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

# Try kg-search via the project's wrapper scripts directory. The wrapper
# itself is bash on Linux; on Windows we just skip the suggestion.
$kgSearchPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-search.ps1"
$kgSearchSh = Join-Path $ProjectRoot ".claude/scripts/kg-search"
$matchOutput = ""
if (Test-Path $kgSearchPs1) {
    try {
        $matchOutput = & pwsh -NoProfile -File $kgSearchPs1 search $concepts --limit 3 --files-only 2>$null | Where-Object { $_ -like 'knowledge/*' }
    } catch { }
} elseif ((Test-Path $kgSearchSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
    try {
        $matchOutput = & bash $kgSearchSh search $concepts --limit 3 --files-only 2>$null | Where-Object { $_ -like 'knowledge/*' }
    } catch { }
}

if ($matchOutput) {
    $arr = @($matchOutput)
    if ($arr.Count -ge 2) {
        Write-Output ""
        Write-Output "Found $($arr.Count) related patterns for: $concepts"
        foreach ($m in $arr) { Write-Output "   $m" }
        Write-Output ""
        Write-Output "   Search more: 'Search knowledge graph for [concept]'"
        Write-Output ""
    }
}
exit 0
