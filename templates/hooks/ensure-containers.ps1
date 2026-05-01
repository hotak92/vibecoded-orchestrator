# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# ensure-containers.ps1
# Ensure all required containers are running (background, non-blocking).
# Mirror of ensure-containers.sh.

$ScriptDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$ComposeDir = if ($env:VCT_COMPOSE_DIR) { $env:VCT_COMPOSE_DIR } else { Join-Path $RepoRoot "claude_mcp_servers" }

$reqEnv = if ($env:VCT_REQUIRED_CONTAINERS) { $env:VCT_REQUIRED_CONTAINERS } else { "weaviate_claude ollama_claude code_embed_claude" }
$Required = $reqEnv -split '\s+' | Where-Object { $_ }

# Container runtime: prefer podman, fallback docker.
$Runtime = $env:VCT_CONTAINER_RUNTIME
if (-not $Runtime) {
    if (Get-Command podman -ErrorAction SilentlyContinue) { $Runtime = "podman" }
    elseif (Get-Command docker -ErrorAction SilentlyContinue) { $Runtime = "docker" }
    else {
        [Console]::Error.WriteLine("ensure-containers: neither podman nor docker found, skipping")
        exit 0
    }
}

# Compose binary: detect both v2 plugin and v1 standalone.
$ComposeCmd = $env:VCT_COMPOSE_CMD
if (-not $ComposeCmd) {
    if ($Runtime -eq "podman") {
        try {
            & podman compose version 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ComposeCmd = "podman compose" }
        } catch { }
        if (-not $ComposeCmd -and (Get-Command podman-compose -ErrorAction SilentlyContinue)) {
            $ComposeCmd = "podman-compose"
        }
    } elseif ($Runtime -eq "docker") {
        try {
            & docker compose version 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $ComposeCmd = "docker compose" }
        } catch { }
        if (-not $ComposeCmd -and (Get-Command docker-compose -ErrorAction SilentlyContinue)) {
            $ComposeCmd = "docker-compose"
        }
    }
}

$started = 0
$needsCompose = $false
foreach ($container in $Required) {
    $status = "missing"
    try {
        $status = (& $Runtime inspect $container --format '{{.State.Status}}' 2>$null | Out-String).Trim()
        if (-not $status) { $status = "missing" }
    } catch { $status = "missing" }

    if ($status -eq "running") { continue }
    elseif ($status -eq "missing") { $needsCompose = $true }
    else {
        try {
            & $Runtime start $container 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $started++ }
        } catch { }
    }
}

if ($needsCompose -and $ComposeCmd -and (Test-Path $ComposeDir)) {
    Push-Location $ComposeDir
    try {
        $parts = $ComposeCmd -split '\s+'
        $cmdHead = $parts[0]
        $cmdRest = $parts[1..($parts.Length - 1)]
        & $cmdHead @cmdRest up -d
    } finally { Pop-Location }
    Write-Output "Ran '$ComposeCmd up -d' in $ComposeDir (missing containers detected)"
} elseif ($needsCompose -and -not $ComposeCmd) {
    [Console]::Error.WriteLine("ensure-containers: $Runtime has no compose available (tried '$Runtime compose' and standalone) -- install $Runtime-compose or the compose plugin")
}

if ($started -gt 0) { Write-Output "Started $started container(s) via $Runtime" }
exit 0
