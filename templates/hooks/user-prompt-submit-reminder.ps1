# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# user-prompt-submit-reminder.ps1
# Context preservation reminder triggered on output volume.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf
$ContextFile = Join-Path $ProjectDir ".claude/CONTEXT_STATE.md"
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$SessionLog = Join-Path $Tmp "claude-session-$ProjectName"

if (-not (Test-Path $SessionLog)) {
    Set-Content -Path $SessionLog -Value "0" -Encoding ascii
}

$WordsThisTurn = 0
if ($env:CLAUDE_TOOL_NAME) {
    switch ($env:CLAUDE_TOOL_NAME) {
        { $_ -in 'Read', 'Write', 'Edit' } { $WordsThisTurn = 1000 }
        'Bash' { $WordsThisTurn = 500 }
        'Task' { $WordsThisTurn = 2000 }
        default { $WordsThisTurn = 200 }
    }
}

$TotalWords = 0
try { $TotalWords = [int](Get-Content $SessionLog -Raw -ErrorAction Stop).Trim() } catch { $TotalWords = 0 }

if ($WordsThisTurn -gt 0) {
    $TotalWords += $WordsThisTurn
    Set-Content -Path $SessionLog -Value $TotalWords -Encoding ascii
}

if ($TotalWords -ge 200000) {
    $Marker = Join-Path $Tmp "claude-ctx-warn-$ProjectName"
    if (Test-Path $Marker) { exit 0 }
    Write-Output ""
    Write-Output "Session checkpoint (~300K tokens used)"
    Write-Output "   - /refresh-context - Update KG if topic shifted"
    Write-Output "   - Update CONTEXT_STATE.md with findings"
    Write-Output "   - Consider new session before context compression"
    Write-Output ""
    New-Item -ItemType File -Path $Marker -Force | Out-Null
    exit 0
}

# Also check CONTEXT_STATE.md staleness (every ~40K words)
if (($TotalWords % 40000) -lt 1500 -and (Test-Path $ContextFile)) {
    $Modified = (Get-Item $ContextFile).LastWriteTime
    $AgeMin = [int]([math]::Floor(((Get-Date) - $Modified).TotalMinutes))
    if ($AgeMin -gt 30) {
        Write-Output ""
        Write-Output "Update CONTEXT_STATE.md (last update: ${AgeMin}min ago)"
        Write-Output ""
    }
}
exit 0
