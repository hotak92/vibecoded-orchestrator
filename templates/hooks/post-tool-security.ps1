# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-tool-security.ps1
# Fires after Write/Edit. Non-blocking (always exits 0).
# Scans for accidentally committed credentials and notifies.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($args) are EMPTY because $CLAUDE_TOOL_ARG_FILE_PATH and
# similar env vars don't exist — settings.json substitutes to "". Verified
# empirically 2026-05-08 via stdin-capture diagnostic.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$EditedFile = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.file_path) {
        $EditedFile = [string]$payload.tool_input.file_path
    }
} catch {
    # Empty/malformed stdin — keep $EditedFile at default
}

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
    # Build the JSONL line via ConvertTo-Json so all fields are properly
    # escaped (paths with quotes, future labels with metacharacters, etc.).
    # -Compress keeps it on one line; -Depth 5 is plenty for this flat shape.
    # Audit fix 2026-05-07.
    $entry = [ordered]@{
        timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        file      = $EditedFile
        patterns  = ($alerts -join ' ')
    }
    $line = $entry | ConvertTo-Json -Compress -Depth 5
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
