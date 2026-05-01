# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# ensure-code-embed-service.ps1
# Ensure the code embedding service container is running.
# Mirror of ensure-code-embed-service.sh. No flock on Windows; we use a
# best-effort lockfile (sentinel) instead.

$Port = if ($env:CODE_EMBED_PORT) { $env:CODE_EMBED_PORT } else { "11440" }
$ContainerName = if ($env:VCT_CODE_EMBED_CONTAINER) { $env:VCT_CODE_EMBED_CONTAINER } else { "code_embed" }
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$LockFile = Join-Path $Tmp "code_embed_service.lock"

$ScriptDir = $PSScriptRoot
$ComposeDir = if ($env:VCT_COMPOSE_DIR) { $env:VCT_COMPOSE_DIR } else { (Resolve-Path (Join-Path $ScriptDir "..\..\claude_mcp_servers")).Path }

# Cross-OS port probe via System.Net.Sockets.TcpClient.
function Test-PortOpen([int]$port, [int]$timeoutSec = 2) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect("localhost", $port, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($timeoutSec))
        if ($ok -and $client.Connected) { return $true }
        return $false
    } catch { return $false }
    finally { $client.Close() }
}

# Container runtime: prefer podman, fallback docker.
$Runtime = $env:VCT_CONTAINER_RUNTIME
if (-not $Runtime) {
    if (Get-Command podman -ErrorAction SilentlyContinue) { $Runtime = "podman" }
    elseif (Get-Command docker -ErrorAction SilentlyContinue) { $Runtime = "docker" }
    else { exit 0 }   # silent no-op
}

$ComposeCmd = $env:VCT_COMPOSE_CMD
if (-not $ComposeCmd) {
    if ($Runtime -eq "podman" -and (Get-Command podman-compose -ErrorAction SilentlyContinue)) {
        $ComposeCmd = "podman-compose"
    } elseif ($Runtime -eq "docker") {
        $ComposeCmd = "docker compose"
    }
}

# Best-effort lock: if another session set the file in last 30s, bail.
if (Test-Path $LockFile) {
    $age = ((Get-Date) - (Get-Item $LockFile).LastWriteTime).TotalSeconds
    if ($age -lt 30) {
        Write-Output "[code_embed] Another session is starting the service, skipping"
        exit 0
    }
}
New-Item -ItemType File -Path $LockFile -Force | Out-Null

try {
    # Already running?
    $status = $null
    try {
        $status = (& $Runtime container inspect $ContainerName --format '{{.State.Status}}' 2>$null | Out-String).Trim()
    } catch { }
    if ($status -eq "running") {
        if (Test-PortOpen -port ([int]$Port) -timeoutSec 3) {
            Write-Output "[code_embed] Already running on port $Port"
            exit 0
        }
        Write-Output "[code_embed] Container running but not responding, restarting..."
        try { & $Runtime restart $ContainerName 2>$null | Out-Null } catch { }
        exit 0
    }

    # Port in use by something else?
    if (Test-PortOpen -port ([int]$Port) -timeoutSec 2) {
        Write-Output "[code_embed] Port $Port already in use (external process)"
        exit 0
    }

    # Container exists but stopped?
    $exists = $false
    try { & $Runtime container inspect $ContainerName 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $exists = $true } } catch { }
    if ($exists) {
        Write-Output "[code_embed] Starting stopped container..."
        try { & $Runtime start $ContainerName 2>$null | Out-Null } catch { }
        Write-Output "[code_embed] Started container $ContainerName"
        exit 0
    }

    if ($ComposeCmd -and (Test-Path $ComposeDir)) {
        Write-Output "[code_embed] Starting code embedding service via $ComposeCmd..."
        Push-Location $ComposeDir
        try {
            $parts = $ComposeCmd -split '\s+'
            $cmdHead = $parts[0]
            $cmdRest = $parts[1..($parts.Length - 1)]
            $output = & $cmdHead @cmdRest up -d code_embed 2>&1
            $output | Select-Object -Last 3 | ForEach-Object { Write-Output $_ }
        } finally { Pop-Location }
        Write-Output "[code_embed] Started container $ContainerName on port $Port"
    }
} finally {
    # Touch the lock file mtime so it expires naturally.
    try { (Get-Item $LockFile).LastWriteTime = Get-Date } catch { }
}
exit 0
