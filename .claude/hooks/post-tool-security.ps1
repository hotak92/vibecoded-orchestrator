# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-tool-security.ps1
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans for accidentally committed credentials and notifies.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

param(
    [Parameter(Position=0)]
    [string]$EditedFile = ""
)

$ScriptDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

$AlertLog = Join-Path $ProjectRoot ".claude/logs/credential_alerts.jsonl"
$AlertDir = Split-Path $AlertLog -Parent
if (-not (Test-Path $AlertDir)) {
    New-Item -ItemType Directory -Path $AlertDir -Force | Out-Null
}

if (-not $EditedFile -or -not (Test-Path -LiteralPath $EditedFile -PathType Leaf)) { exit 0 }

# Credential patterns mirrored from post-tool-security.sh.
$patterns = @(
    @{ Label = "Anthropic/OpenAI API key"; Re = 'sk-(ant-api03|[a-zA-Z0-9]{30,})-[a-zA-Z0-9]' },
    @{ Label = "AWS access key";          Re = 'AKIA[A-Z0-9]{16}' },
    @{ Label = "GitHub token";            Re = 'gh[pousr]_[a-zA-Z0-9]{36}' },
    @{ Label = "PEM private key";         Re = 'BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY' },
    @{ Label = "Generic secret";          Re = '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["''][a-zA-Z0-9+/=_\-]{32,}' }
)

$content = $null
try { $content = Get-Content -LiteralPath $EditedFile -Raw -ErrorAction Stop } catch { exit 0 }

$alerts = @()
foreach ($p in $patterns) {
    if ($content -match $p.Re) { $alerts += $p.Label }
}

if ($alerts.Count -gt 0) {
    $base = Split-Path $EditedFile -Leaf
    $msg = "Possible credential in ${base}: $($alerts -join ' ')"
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $fileEsc = $EditedFile -replace '\\', '\\\\' -replace '"', '\"'
    $line = "{""timestamp"":""$ts"",""file"":""$fileEsc"",""patterns"":""$($alerts -join ' ')""}"
    try { Add-Content -Path $AlertLog -Value $line -ErrorAction Stop } catch { }

    $NotifyScript = Join-Path $ProjectRoot ".claude/scripts/notify.py"
    if ($PY -and (Test-Path $NotifyScript)) {
        try {
            & $PY $NotifyScript "Claude Code Security Alert" $msg `
                --urgency critical --icon dialog-warning 2>$null | Out-Null
        } catch { }
    }
    Write-Output "WARNING: $msg"
    Write-Output "   Review: $EditedFile"
}
exit 0
