# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# stop-failure-notify.ps1
# Fires on StopFailure event — when a turn ends due to API error.
# Sends urgent desktop notification and logs the failure.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$Payload = ""
try { $Payload = [Console]::In.ReadToEnd() } catch { }

$ErrorType = "unknown"
$ErrorMsg = "No details"
$SessionId = ""
if ($PY -and $Payload) {
    try {
        $ErrorType = ($Payload | & $PY -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('type','unknown'))" 2>$null)
        if (-not $ErrorType) { $ErrorType = "unknown" }
    } catch { }
    try {
        $ErrorMsg = ($Payload | & $PY -c "import sys,json; d=json.load(sys.stdin); print(d.get('error',{}).get('message','No details')[:120])" 2>$null)
        if (-not $ErrorMsg) { $ErrorMsg = "No details" }
    } catch { }
    try {
        $SessionId = ($Payload | & $PY -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id','')[:8])" 2>$null)
        if (-not $SessionId) { $SessionId = "" }
    } catch { }
}

# Log the failure under the user's home metrics dir.
$UserHome = [System.Environment]::GetFolderPath('UserProfile')
$LogDir = Join-Path $UserHome ".claude/metrics"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}
$ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$line = "{""timestamp"":""$ts"",""project"":""$ProjectName"",""session_id"":""$SessionId"",""error_type"":""$ErrorType"",""error_message"":""$ErrorMsg""}"
try { Add-Content -Path (Join-Path $LogDir "failures.jsonl") -Value $line } catch { }

# Urgent cross-platform notification.
$NotifyScript = Join-Path $ProjectDir ".claude/scripts/notify.py"
if ($PY -and (Test-Path $NotifyScript)) {
    try {
        & $PY $NotifyScript "Claude API Error -- $ProjectName" "${ErrorType}: $ErrorMsg" `
            --urgency critical --icon dialog-error --expire-time 15000 2>$null | Out-Null
    } catch { }
}
exit 0
