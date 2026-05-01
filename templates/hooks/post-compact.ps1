# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-compact.ps1
# Fires on PostCompact event — after context compaction completes.
# Logs the event to ~/.claude/metrics/compactions.jsonl and notifies.

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# Read stdin payload (may be empty).
$Payload = ""
try { $Payload = [Console]::In.ReadToEnd() } catch { }

$Trigger = "unknown"
if ($PY -and $Payload) {
    try {
        $Trigger = ($Payload | & $PY -c "import sys,json; d=json.load(sys.stdin); print(d.get('trigger','unknown'))" 2>$null)
        if (-not $Trigger) { $Trigger = "unknown" }
    } catch { $Trigger = "unknown" }
}

# Log compaction event under the user's home metrics dir.
$UserHome = [System.Environment]::GetFolderPath('UserProfile')
$LogDir = Join-Path $UserHome ".claude/metrics"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$line = "{""timestamp"":""$ts"",""project"":""$ProjectName"",""trigger"":""$Trigger""}"
try { Add-Content -Path (Join-Path $LogDir "compactions.jsonl") -Value $line } catch { }

# Cross-platform desktop notification.
$NotifyScript = Join-Path $ProjectDir ".claude/scripts/notify.py"
if ($PY -and (Test-Path $NotifyScript)) {
    try {
        & $PY $NotifyScript "Context compacted -- $ProjectName" "Trigger: $Trigger. Context re-injected." `
            --urgency low --icon dialog-information --expire-time 5000 2>$null | Out-Null
    } catch { }
}
exit 0
