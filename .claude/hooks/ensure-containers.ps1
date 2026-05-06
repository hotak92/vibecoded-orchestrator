# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# ensure-containers.ps1
# Ensure all required containers are running (background, non-blocking).
# Mirror of ensure-containers.sh.
#
# Compose-dir resolution order (PR-2 portability fix 2026-05-06):
#   1. $env:VCT_COMPOSE_DIR              — explicit override
#   2. $env:VCT_INFRASTRUCTURE_DIR       — orchestrator clone's infrastructure/
#   3. $env:VCT_ORCHESTRATOR_ROOT\infrastructure   — env-resolved orch root
#   4. <project>\infrastructure          — bundled compose copy (per-project)
#   5. <project>\claude_mcp_servers      — orchestrator clone fallback (legacy)
# Container names come from the shared `_lib\container-names.ps1` registry
# so the hook and the bundled docker-compose.yml cannot disagree.

$ScriptDir = $PSScriptRoot
$RepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

# Source canonical container-name registry. Supplies $VcoRequiredContainers.
$LibFile = Join-Path $ScriptDir "_lib\container-names.ps1"
if (Test-Path $LibFile) {
    . $LibFile
} else {
    # Fallback if _lib is missing (very old install pre-PR-2). Mirror the
    # current canonical defaults; users can still override via
    # VCT_REQUIRED_CONTAINERS.
    if ($env:VCT_REQUIRED_CONTAINERS) {
        $VcoRequiredContainers = $env:VCT_REQUIRED_CONTAINERS -split '\s+' | Where-Object { $_ }
    } else {
        $VcoRequiredContainers = @("vco_weaviate", "vco_ollama", "vct_code_embed")
    }
}

# Resolve compose dir. The bundled per-project install puts compose files
# in <project>\infrastructure; the orchestrator's own clone has a sibling
# claude_mcp_servers\ with a compose.yaml. Prefer the bundled location so
# the hook works in user projects.
$ComposeDir = $env:VCT_COMPOSE_DIR
if (-not $ComposeDir) {
    if ($env:VCT_INFRASTRUCTURE_DIR -and (Test-Path $env:VCT_INFRASTRUCTURE_DIR)) {
        $ComposeDir = $env:VCT_INFRASTRUCTURE_DIR
    } elseif ($env:VCT_ORCHESTRATOR_ROOT -and (Test-Path (Join-Path $env:VCT_ORCHESTRATOR_ROOT "infrastructure"))) {
        $ComposeDir = Join-Path $env:VCT_ORCHESTRATOR_ROOT "infrastructure"
    } elseif (Test-Path (Join-Path $RepoRoot "infrastructure")) {
        $ComposeDir = Join-Path $RepoRoot "infrastructure"
    } elseif (Test-Path (Join-Path $RepoRoot "claude_mcp_servers")) {
        # Legacy fallback — only the orchestrator clone has this layout.
        $ComposeDir = Join-Path $RepoRoot "claude_mcp_servers"
    } else {
        $ComposeDir = ""
    }
}

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
foreach ($container in $VcoRequiredContainers) {
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

if ($needsCompose -and $ComposeCmd -and $ComposeDir -and (Test-Path $ComposeDir)) {
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
} elseif ($needsCompose -and -not $ComposeDir) {
    [Console]::Error.WriteLine("ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT\infrastructure, $RepoRoot\infrastructure, $RepoRoot\claude_mcp_servers) -- set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude\env")
}

if ($started -gt 0) { Write-Output "Started $started container(s) via $Runtime" }
exit 0
