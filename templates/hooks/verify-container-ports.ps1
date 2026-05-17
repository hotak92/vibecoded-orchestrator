# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# verify-container-ports.ps1 — host-side container-port watchdog (2026-05-08).
#
# PowerShell sibling of verify-container-ports.sh. Engine-agnostic:
# detects "container says running but host port doesn't answer" for
# both podman (state-DB desync) and docker (silent app-level crash).
# Recovery is engine-specific: podman → rm -f + compose up; docker
# → restart.
#
# Engine selection: $env:VCT_CONTAINER_RUNTIME wins; otherwise prefer
# podman (project convention), fall back to docker.
#
# Bypass: $env:VCT_SKIP_PORT_WATCHDOG = "1"
# Verbose: $env:VCT_PORT_WATCHDOG_VERBOSE = "1"

. "$PSScriptRoot/_lib/stderr-cap.ps1"

if ($env:VCT_SKIP_PORT_WATCHDOG -eq "1") { return }

# Engine selection.
$runtime = $env:VCT_CONTAINER_RUNTIME
if (-not $runtime) {
    if (Get-Command podman -ErrorAction SilentlyContinue) {
        $runtime = "podman"
    } elseif (Get-Command docker -ErrorAction SilentlyContinue) {
        $runtime = "docker"
    } else {
        return
    }
}
if (-not (Get-Command $runtime -ErrorAction SilentlyContinue)) { return }

# Compose driver (`podman-compose` / `podman compose` / `docker compose`).
if ($runtime -eq "podman") {
    if (Get-Command podman-compose -ErrorAction SilentlyContinue) {
        $composeArgs = @("podman-compose")
    } else {
        $composeArgs = @("podman", "compose")
    }
} else {
    $composeArgs = @("docker", "compose")
}

# Container | host_port | probe_kind | probe_endpoint
#
# v0.2.15 maintainer-leak fix: stopped hardcoding weaviate_claude /
# ollama_claude / code_embed_claude — those names only ever existed
# on the maintainer's own pre-VCO machine. Real VCO installs use
# vco_*. We row-expand each service across every known historical name
# (canonical → v0.1.x unprefixed → maintainer-era), and the
# Test-ContainerRunning check below skips rows whose container doesn't
# exist. This makes the hook portable across all generations of VCO
# install without removing recovery support for users on legacy names.
#
# Authoritative registry lives in vco_lib/containers.py (Python) and
# templates/hooks/_lib/container-names.{sh,ps1} (shell). Sync this list
# when those change — the test_pr2_templates_portability tests pin
# them together.
$watch = @(
    # Weaviate — canonical first
    @{ Name = "vco_weaviate";        Port = 8081;  Kind = "http"; Endpoint = "/v1/meta" }
    @{ Name = "weaviate";            Port = 8081;  Kind = "http"; Endpoint = "/v1/meta" }
    @{ Name = "weaviate_claude";     Port = 8081;  Kind = "http"; Endpoint = "/v1/meta" }
    # Ollama
    @{ Name = "vco_ollama";          Port = 11435; Kind = "http"; Endpoint = "/api/tags" }
    @{ Name = "ollama";              Port = 11435; Kind = "http"; Endpoint = "/api/tags" }
    @{ Name = "ollama_claude";       Port = 11435; Kind = "http"; Endpoint = "/api/tags" }
    # Code-embedding service
    @{ Name = "vco_code_embed";      Port = 11440; Kind = "tcp";  Endpoint = "" }
    @{ Name = "vct_code_embed";      Port = 11440; Kind = "tcp";  Endpoint = "" }
    @{ Name = "code_embed";          Port = 11440; Kind = "tcp";  Endpoint = "" }
    @{ Name = "code_embed_claude";   Port = 11440; Kind = "tcp";  Endpoint = "" }
    # Model router (sibling service; not in containers registry — single name)
    @{ Name = "model_router_claude"; Port = 11436; Kind = "tcp";  Endpoint = "" }
)

$verbose = ($env:VCT_PORT_WATCHDOG_VERBOSE -eq "1")

function Test-PortHttp {
    param([int]$Port, [string]$Endpoint)
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$Port$Endpoint" -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return $resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400
    } catch {
        return $false
    }
}

function Test-PortTcp {
    param([int]$Port)
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync("localhost", $Port)
        if ($task.Wait(3000)) {
            $client.Close()
            return $true
        }
        return $false
    } catch {
        return $false
    }
}

function Test-ContainerRunning {
    param([string]$Name)
    $names = & $runtime ps --filter "name=^$Name`$" --format "{{.Names}}" 2>$null
    return $names -contains $Name
}

function Test-ContainerPidAlive {
    param([string]$Name)
    # Docker has no zombie state-DB issue (centralised daemon manages
    # state honestly). Always treat docker containers' "running" as
    # truthful. Same for any runtime where the container PID lives in
    # a VM (Docker Desktop on macOS/Windows, Podman Machine).
    if ($runtime -ne "podman") { return $true }
    if (-not $IsLinux) { return $true }
    $pidStr = & $runtime inspect $Name --format "{{.State.Pid}}" 2>$null
    if (-not $pidStr -or $pidStr -eq "0") { return $false }
    return Test-Path "/proc/$pidStr"
}

$zombies = @()
$healthy = 0
$absent = 0

foreach ($entry in $watch) {
    $name = $entry.Name
    $port = $entry.Port
    $kind = $entry.Kind
    $endpoint = $entry.Endpoint

    if (-not (Test-ContainerRunning $name)) {
        $absent++
        if ($verbose) { Write-Output "verify-container-ports: $name not running (skip)" }
        continue
    }

    $ok = if ($kind -eq "http") { Test-PortHttp $port $endpoint } else { Test-PortTcp $port }

    if ($ok) {
        $healthy++
        if ($verbose) { Write-Output "verify-container-ports: $name :$port OK" }
        continue
    }

    if (Test-ContainerPidAlive $name) {
        if ($verbose) { Write-Output "verify-container-ports: $name :$port slow (PID alive, starting up?)" }
        continue
    }

    $zombies += @{ Name = $name; Port = $port }
}

if ($zombies.Count -eq 0) {
    if ($verbose) { Write-Output "verify-container-ports: $healthy healthy, $absent absent, 0 zombies" }
    return
}

Write-Output "🩺 Container port-binding watchdog: $($zombies.Count) zombie state(s) detected"
Write-Output "   (container says 'running' but host port is unbound AND container PID is dead)"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$composeDir = $null
foreach ($candidate in @("claude_mcp_servers", "infrastructure", ".")) {
    $path = Join-Path $projectRoot $candidate
    if ((Test-Path (Join-Path $path "compose.yaml")) -or `
        (Test-Path (Join-Path $path "compose.yml")) -or `
        (Test-Path (Join-Path $path "docker-compose.yml"))) {
        $composeDir = $path
        break
    }
}

foreach ($z in $zombies) {
    $name = $z.Name
    $port = $z.Port
    # Derive compose service name from container name. Compose service
    # keys are unprefixed (weaviate / ollama / code_embed); the actual
    # container ships under vco_ / vct_ / unprefixed / _claude variants
    # depending on install era. Strip every known prefix/suffix.
    $service = $name -replace "^vco_", "" -replace "^vct_", "" -replace "_claude$", ""
    Write-Output "   → recovering $name (port :$port) via $runtime"
    if ($runtime -eq "podman") {
        # Podman state-DB desync: force-rm + recreate. `podman restart`
        # is a no-op because Podman thinks the container is alive.
        & $runtime rm -f $name *>$null
        if ($LASTEXITCODE -eq 0) {
            if ($composeDir) {
                Push-Location $composeDir
                try {
                    & $composeArgs[0] $composeArgs[1..($composeArgs.Length - 1)] up -d $service *>$null
                    if ($LASTEXITCODE -ne 0) {
                        Write-Output "     ! $($composeArgs -join ' ') up -d $service failed; manual: cd $composeDir; $($composeArgs -join ' ') up -d $service"
                    }
                } finally {
                    Pop-Location
                }
            } else {
                Write-Output "     ! could not auto-detect compose dir; manual: $($composeArgs -join ' ') up -d $service"
            }
        } else {
            Write-Output "     ! $runtime rm -f $name failed"
        }
    } else {
        # Docker silent-crash: state DB is reliable, so the app inside
        # has wedged. Restart cycles PID 1 and is enough.
        & $runtime restart $name *>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Output "     ! $runtime restart $name failed; manual: $runtime logs $name"
        }
    }
}

Write-Output "   recovery complete; first KG/Ollama call may take 20-30s while services warm up"
