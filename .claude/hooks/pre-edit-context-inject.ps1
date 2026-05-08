# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# pre-edit-context-inject.ps1
# Pre-edit context injection — KG + code graph context for the file being edited.
# Always exit 0 (never block the edit).

. "$PSScriptRoot/_lib/stderr-cap.ps1"

param(
    [Parameter(Position=0)] [string]$ToolName = "",
    [Parameter(Position=1)] [string]$ToolArgs = ""
)

if ($ToolName -ne "Edit") { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$SessionId = if ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { "default" }
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$CacheBase = Join-Path $Tmp "claude_edit_cache_$SessionId"
$CacheTtl = 600

$SeenNodesFile = Join-Path $Tmp "claude_seen_nodes_$SessionId"
$CompactFlag = Join-Path $Tmp "claude_ctx_snapshots\compact_flag_$SessionId"
if ((Test-Path $CompactFlag) -and (Test-Path $SeenNodesFile)) {
    Remove-Item $SeenNodesFile -Force -ErrorAction SilentlyContinue
}

function Get-JsonField([string]$field) {
    if (-not $PY -or -not $ToolArgs) { return "" }
    try {
        $code = "import sys, json`ntry:`n    d = json.loads(sys.stdin.read())`n    print(d.get('$field', ''))`nexcept Exception:`n    print('')"
        $r = $ToolArgs | & $PY -c $code 2>$null
        if ($r) { return $r.Trim() }
    } catch { }
    return ""
}

$FilePath = Get-JsonField "file_path"
$NewString = Get-JsonField "new_string"
if (-not $FilePath) { exit 0 }

# Cache key from file path (md5 via .NET).
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($FilePath)
$hash = ($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
$CacheDir = $CacheBase
$CacheFile = Join-Path $CacheDir $hash
if (-not (Test-Path $CacheDir)) {
    New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
}

# Check cache (10-min TTL) — uses .NET file mtime, no cross-OS stat issues.
if (Test-Path $CacheFile) {
    $mtime = (Get-Item $CacheFile).LastWriteTime
    $age = ((Get-Date) - $mtime).TotalSeconds
    if ($age -lt $CacheTtl) {
        Get-Content $CacheFile -Raw
        exit 0
    }
}

# Auto-detect project for multi-codebase support — best-effort, optional.
$DetectScriptPs1 = Join-Path $ProjectRoot ".claude/scripts/detect-project.ps1"
$DetectedProject = ""
if (Test-Path $DetectScriptPs1) {
    try {
        $DetectedProject = (& pwsh -NoProfile -File $DetectScriptPs1 $FilePath $ProjectRoot 2>$null).Trim()
    } catch { }
}
$CodeGraphProjectArg = if ($DetectedProject) { @('--project', $DetectedProject) } else { @() }

$Basename = Split-Path $FilePath -Leaf
$ModuleName = [System.IO.Path]::GetFileNameWithoutExtension($Basename)
$NewSnippet = if ($NewString.Length -gt 200) { $NewString.Substring(0,200) } else { $NewString }
$Query = "$ModuleName $NewSnippet".Trim()

# Run searches sequentially (PowerShell parallel jobs add overhead worse than 5s budget).
$KgTmp = New-TemporaryFile
$CodeTmp = New-TemporaryFile

$VenvPy = Join-Path (if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }) "claude_mcp_servers\.venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    $VenvPy = Join-Path (if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }) "claude_mcp_servers/.venv/bin/python"
}
$RlScript = Join-Path $ProjectRoot "claude_mcp_servers/scripts/rl_kg_search.py"
if ((Test-Path $VenvPy) -and (Test-Path $RlScript)) {
    try {
        & $VenvPy $RlScript $Query --limit 1 2>$null | Select-Object -First 40 | Set-Content -Path $KgTmp.FullName
    } catch { }
}

$IsCode = $false
if ($FilePath -match '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$') {
    $IsCode = $true
    $cgQueryPs1 = Join-Path $ProjectRoot ".claude/scripts/code-graph-query.ps1"
    $cgQuerySh = Join-Path $ProjectRoot ".claude/scripts/code-graph-query"
    try {
        if (Test-Path $cgQueryPs1) {
            $out = & pwsh -NoProfile -File $cgQueryPs1 search $Query @CodeGraphProjectArg --limit 2 2>$null |
                Where-Object { $_ -notlike "*$FilePath*" } |
                Select-Object -First 20
            $out | Set-Content -Path $CodeTmp.FullName
        } elseif ((Test-Path $cgQuerySh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            $out = & bash $cgQuerySh search $Query @CodeGraphProjectArg --limit 2 2>$null |
                Where-Object { $_ -notlike "*$FilePath*" } |
                Select-Object -First 20
            $out | Set-Content -Path $CodeTmp.FullName
        }
    } catch { }
}

$KgResult = ""
$CodeResult = ""
try { $KgResult = (Get-Content -Path $KgTmp.FullName -Raw -ErrorAction Stop) } catch { }
if ($IsCode) {
    try { $CodeResult = (Get-Content -Path $CodeTmp.FullName -Raw -ErrorAction Stop) } catch { }
}
Remove-Item $KgTmp.FullName, $CodeTmp.FullName -Force -ErrorAction SilentlyContinue

# Dedup against this session's seen nodes.
# Note (audit fix 2026-05-07): the .ps1 sibling already uses a hashtable
# (`$seen = @{}`) for O(1) lookups, so the bash-side bug (per-line grep
# scaling O(input × seen)) does not exist here. Parity-touch only.
function Filter-Seen([string]$input) {
    if (-not $input) { return "" }
    $filtered = New-Object System.Text.StringBuilder
    if (-not (Test-Path $SeenNodesFile)) { New-Item -ItemType File -Path $SeenNodesFile -Force | Out-Null }
    $seen = @{}
    foreach ($l in Get-Content $SeenNodesFile -ErrorAction SilentlyContinue) { $seen[$l] = $true }
    foreach ($line in $input -split "`n") {
        if (-not $line) { continue }
        $title = ($line -split ' \| ')[0]
        if ($title.Length -gt 100) { $title = $title.Substring(0, 100) }
        if ($title -and -not $seen.ContainsKey($title)) {
            [void]$filtered.AppendLine($line)
            Add-Content -Path $SeenNodesFile -Value $title -ErrorAction SilentlyContinue
            $seen[$title] = $true
        }
    }
    return $filtered.ToString()
}

if ($KgResult)   { $KgResult   = Filter-Seen $KgResult }
if ($CodeResult) { $CodeResult = Filter-Seen $CodeResult }

$HasKg   = [bool]$KgResult
$HasCode = [bool]$CodeResult
if (-not $HasKg -and -not $HasCode) { exit 0 }

$out = [System.Text.StringBuilder]::new()
[void]$out.AppendLine("[Pre-edit context for ${Basename}]:")
[void]$out.AppendLine("")
if ($HasKg) {
    [void]$out.AppendLine("KG: $KgResult")
}
if ($HasCode) {
    [void]$out.AppendLine("Related code: $CodeResult")
}

$outStr = $out.ToString()
try { Set-Content -Path $CacheFile -Value $outStr -Encoding UTF8 -NoNewline } catch { }
Write-Output $outStr
exit 0
