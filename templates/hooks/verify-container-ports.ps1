# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
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
#
# Log: every run appends ONE JSON line to
# <project>/.claude/logs/container_port_check.jsonl (<project> =
# $env:CLAUDE_PROJECT_DIR, else the project this hook is installed in):
# timestamp, runtime, each service's result (healthy | slow | zombie |
# absent, with the container and port) and the action taken (none | skipped
# + reason | waiting_for_session_lock | lock_busy | recovered + what was
# done to each zombie). A run that finds a zombie re-runs itself detached
# under the session lock, so it writes the detection line and the re-run
# writes the recovery line.
# Soft-fail: a log that cannot be written never changes what the hook does.
# MUST MATCH the record verify-container-ports.sh writes.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
. "$PSScriptRoot/_lib/compose-invocation.ps1"

if ($env:VCT_SKIP_PORT_WATCHDOG -eq "1") { return }

# ── The run log (see the header) ─────────────────────────────────────────
$PortCheckProject = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { Join-Path $PSScriptRoot "..\.." }
$PortCheckLog = Join-Path $PortCheckProject ".claude/logs/container_port_check.jsonl"
$script:LogRuntime = ""
$script:LogContainers = [System.Collections.ArrayList]::new()
$script:LogRecovery = [System.Collections.ArrayList]::new()
function Get-VcoServiceOf {
    param([string]$Name)
    if ($Name -like "*weaviate*") { return "weaviate" }
    if ($Name -like "*ollama*") { return "ollama" }
    if ($Name -like "*code_embed*") { return "code_embed" }
    return ""
}
function Add-VcoLogContainer {
    param([string]$Name, [int]$Port, [string]$Result)
    [void]$script:LogContainers.Add(@{ service = (Get-VcoServiceOf $Name); container = $Name; port = $Port; result = $Result })
}
function Add-VcoLogRecovery {
    param([string]$Name, [string]$Action, [string]$Detail = "")
    [void]$script:LogRecovery.Add([ordered]@{ container = $Name; service = (Get-VcoServiceOf $Name); action = $Action; detail = $Detail })
}
function Write-VcoPortCheckLog {
    param([string]$Action, [string]$Reason = "")
    try {
        # The per-service summary: the most telling state of the service's
        # watched containers (zombie > slow > healthy); absent when none runs.
        $services = [ordered]@{}
        foreach ($svc in @("weaviate", "ollama", "code_embed")) {
            $best = $null
            $rank = 0
            foreach ($c in $script:LogContainers) {
                if ($c.service -ne $svc) { continue }
                $rk = switch ($c.result) { "zombie" { 3 } "slow" { 2 } "healthy" { 1 } default { 0 } }
                if ($rk -gt $rank) { $rank = $rk; $best = $c }
            }
            if ($best) {
                $services[$svc] = [ordered]@{ result = $best.result; container = $best.container; port = [int]$best.port }
            } else {
                $services[$svc] = [ordered]@{ result = "absent" }
            }
        }
        $record = [ordered]@{
            timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'")
            hook      = "verify-container-ports"
            runtime   = [string]$script:LogRuntime
            services  = $services
            action    = $Action
        }
        if ($Reason) { $record.reason = $Reason }
        if ($script:LogRecovery.Count -gt 0) { $record.recovery = @($script:LogRecovery.ToArray()) }
        $line = ConvertTo-Json -InputObject $record -Compress -Depth 6
        $dir = Split-Path $PortCheckLog -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force -ErrorAction Stop | Out-Null }
        [System.IO.File]::AppendAllText($PortCheckLog, $line + "`n")
    } catch { }
}

# Container runtime + compose: ONE home — `python -m vco_lib.containers resolve`
# (v0.2.92 PLAN-EXTENSION §3.5 / R13). This hook used to mirror the
# podman/docker + compose-form detection inline (as did two sibling hooks,
# install.py and the launcher), and the four copies had drifted in their
# compose preference order. Class A of the A>B>C rule: one Python
# implementation, called via a ~50 ms subprocess on this session-start path.
# Loud-fail: if the resolver cannot run at all (no interpreter, broken
# install), say so on stderr and skip — never fall back to an inline copy.
$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
$RunPy = $PY
$VenvLib = Join-Path $LibDir "resolve-vco-venv.ps1"
if (Test-Path $VenvLib) {
    . $VenvLib
    try {
        $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $PSScriptRoot
        if ($VcoVenvPython -and (Test-Path $VcoVenvPython)) { $RunPy = $VcoVenvPython }
    } catch { }
}
if (-not $RunPy) {
    Write-Output "verify-container-ports: no Python interpreter for vco_lib.containers (broken VCO install?); skipping"
    Write-VcoPortCheckLog -Action "skipped" -Reason "no Python interpreter for vco_lib (broken VCO install?)"
    return
}
$VcoRt = $null
$VcoRtRc = $null
# Capture the resolver's stderr instead of discarding it (v0.2.92 MAJOR-6):
# `2>$null` used to throw the reason away, so a crash on Windows printed
# "resolve failed (rc=N)" with nothing to act on while the .sh sibling
# printed the stderr tail.
$VcoRtErr = Join-Path ([System.IO.Path]::GetTempPath()) "vco-containers-resolve.$PID.err"
try {
    $VcoRtJson = & $RunPy -m vco_lib.containers resolve --json 2>$VcoRtErr
    $VcoRtRc = $LASTEXITCODE
    if ($VcoRtRc -in 0, 3, 4) { $VcoRt = ($VcoRtJson | Out-String) | ConvertFrom-Json }
} catch { $VcoRt = $null }
if (-not $VcoRt) {
    $VcoRtWhy = ""
    if (Test-Path $VcoRtErr) { $VcoRtWhy = ((Get-Content $VcoRtErr -Tail 3) -join " ").Trim() }
    Remove-Item $VcoRtErr -ErrorAction SilentlyContinue
    Write-Output "verify-container-ports: vco_lib.containers resolve failed (rc=$VcoRtRc): $VcoRtWhy; skipping"
    Write-VcoPortCheckLog -Action "skipped" -Reason "vco_lib.containers resolve failed (rc=$VcoRtRc)"
    return
}
Remove-Item $VcoRtErr -ErrorAction SilentlyContinue
# v0.2.92 BLOCKER-4 + MAJOR-6: the resolver REFUSES a pinned-but-unusable
# runtime (podman and docker have per-runtime named volumes, so driving the
# one the user did not pin brings the stack up EMPTY) and hands back the
# refusal as its reason. Report it on STDOUT, not stderr: a SessionStart
# hook's stderr is not surfaced to the user when the hook exits 0 - only
# stdout is injected as session context, and an unread report is not a report.
if ($VcoRt.state -ne "resolved") {
    # Probe-only watchdog: quiet on a plain "no runtime" host (ensure-containers
    # already said it), but a REFUSED PIN is a user action, so it is reported.
    if ($VcoRt.requested) { Write-Output "verify-container-ports: $($VcoRt.reason); skipping" }
    Write-VcoPortCheckLog -Action "skipped" -Reason $(if ($VcoRt.reason) { [string]$VcoRt.reason } else { "no usable container runtime" })
    return
}
$runtime = $VcoRt.runtime
$script:LogRuntime = $runtime
# Compose driver as an argv array.
$composeArgs = if ($VcoRt.compose) { @($VcoRt.compose) } else { @($runtime, "compose") }

# v0.2.97 (R7a F10, parity with the .sh sibling — the rationale is there):
# detection runs unlocked; a zombie is recovered only under the per-user
# session lock shared with ensure-containers, after the session reconcile,
# and detection is REPEATED under the lock (ensure-containers may have
# recovered it meanwhile). Ports are the rows' (the plan), not literals.
function Read-VcoLifecyclePlan {
    try { return ((& $RunPy -m vco_lib.service_lifecycle plan --json 2>$null | Out-String) | ConvertFrom-Json) } catch { return $null }
}
$lcPlan = Read-VcoLifecyclePlan
function Get-VcoProbePort {
    param([string]$Service, [int]$Default)
    if ($lcPlan -and $lcPlan.services -and $lcPlan.services.$Service -and $lcPlan.services.$Service.port) {
        return [int]$lcPlan.services.$Service.port
    }
    return $Default
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
function Get-VcoWatchList {
    $wv = Get-VcoProbePort -Service "weaviate" -Default 8081
    $ol = Get-VcoProbePort -Service "ollama" -Default 11435
    $ce = Get-VcoProbePort -Service "code_embed" -Default 11440
    return @(
    # Weaviate — canonical first
    @{ Name = "vco_weaviate";        Port = $wv; Kind = "http"; Endpoint = "/v1/meta" }
    @{ Name = "weaviate";            Port = $wv; Kind = "http"; Endpoint = "/v1/meta" }
    @{ Name = "weaviate_claude";     Port = $wv; Kind = "http"; Endpoint = "/v1/meta" }
    # Ollama
    @{ Name = "vco_ollama";          Port = $ol; Kind = "http"; Endpoint = "/api/tags" }
    @{ Name = "ollama";              Port = $ol; Kind = "http"; Endpoint = "/api/tags" }
    @{ Name = "ollama_claude";       Port = $ol; Kind = "http"; Endpoint = "/api/tags" }
    # Code-embedding service
    @{ Name = "vco_code_embed";      Port = $ce; Kind = "tcp";  Endpoint = "" }
    @{ Name = "vct_code_embed";      Port = $ce; Kind = "tcp";  Endpoint = "" }
    @{ Name = "code_embed";          Port = $ce; Kind = "tcp";  Endpoint = "" }
    @{ Name = "code_embed_claude";   Port = $ce; Kind = "tcp";  Endpoint = "" }
    # NOTE: v0.2.50 audit F3 (2026-06-08) — the `model_router_claude;11436`
    # row that previously lived here was a maintainer-machine leak (same
    # shape as the `_claude` suffix family v0.2.15 already cleaned up
    # for weaviate/ollama/code_embed). There is no canonical
    # `vco_model_router` service in compose or install.py; the model-
    # router runs only on the maintainer's host. Drop the row to stop
    # this hook from polling port 11436 on every install.
    )
}

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

function Find-VcoZombies {
$zombies = @()
$script:healthy = 0
$script:absent = 0
$script:LogContainers.Clear()

foreach ($entry in (Get-VcoWatchList)) {
    $name = $entry.Name
    $port = $entry.Port
    $kind = $entry.Kind
    $endpoint = $entry.Endpoint

    if (-not (Test-ContainerRunning $name)) {
        $script:absent++
        if ($verbose) { Write-Output "verify-container-ports: $name not running (skip)" }
        continue
    }

    # A dead main PID is a zombie whatever its port says; the port is probed
    # only for a live (or uncheckable) PID (parity with the .sh sibling).
    if (Test-ContainerPidAlive $name) {
        $ok = if ($kind -eq "http") { Test-PortHttp $port $endpoint } else { Test-PortTcp $port }
        if ($ok) {
            $script:healthy++
            Add-VcoLogContainer -Name $name -Port $port -Result "healthy"
            if ($verbose) { Write-Output "verify-container-ports: $name :$port OK" }
        } else {
            Add-VcoLogContainer -Name $name -Port $port -Result "slow"
            if ($verbose) { Write-Output "verify-container-ports: $name :$port slow (PID alive, starting up?)" }
        }
        continue
    }

    Add-VcoLogContainer -Name $name -Port $port -Result "zombie"
    $zombies += @{ Name = $name; Port = $port }
}
return ,$zombies
}

$zombies = Find-VcoZombies
if ($zombies.Count -eq 0) {
    if ($verbose) { Write-Output "verify-container-ports: $($script:healthy) healthy, $($script:absent) absent, 0 zombies" }
    Write-VcoPortCheckLog -Action "none"
    return
}
# Recover only under the session lock, after the reconcile, on a repeated
# detection (see the header of this section).
. (Join-Path $LibDir "session-lock.ps1")
# R8 G3 (parity with the .sh sibling): recovery runs DETACHED, so the 30 s
# timeout's kill never reaches the lock holder mid-recovery; the detached
# run repeats detection, then takes the lock.
if (-not $env:VCO_SESSION_DETACHED) {
    Write-VcoPortCheckLog -Action "waiting_for_session_lock"
    Invoke-VcoSessionHookDetached -RunPy $RunPy -Hook "verify-container-ports" -ScriptPath $PSCommandPath
    return
}
$VcoSessionLock = Enter-VcoSessionLock -RunPy $RunPy -WaitSeconds 15
if (-not $VcoSessionLock.Held) {
    Write-Output "verify-container-ports: $($zombies.Count) zombie container(s) seen, but ensure-containers still holds the session lock; not recovered here (the next session re-checks)"
    Write-VcoPortCheckLog -Action "lock_busy" -Reason "ensure-containers held the session lock"
    return
}
# Exit 5 = the rows are not known to be current: nothing is removed on them.
& $RunPy -m vco_lib.service_lifecycle session-reconcile --if-stale 60 --require-fresh 2>$null | ForEach-Object { Write-Output $_ }
$rowsChecked = ($LASTEXITCODE -eq 0)
$lcPlan = Read-VcoLifecyclePlan
$zombies = Find-VcoZombies
if ($zombies.Count -eq 0) {
    if ($verbose) { Write-Output "verify-container-ports: recovered meanwhile (ensure-containers); nothing to do" }
    Write-VcoPortCheckLog -Action "none"
    return
}

Write-Output "🩺 Container port-binding watchdog: $($zombies.Count) zombie state(s) detected"
Write-Output "   (container says 'running' but host port is unbound AND container PID is dead)"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
# v0.2.97: the installer's compose (infrastructure/) FIRST — a VCO-managed
# service is re-created under the project that owns it, never under the
# legacy claude_mcp_servers/ home. Same tiers as ensure-containers (parity
# with the .sh sibling).
$composeDir = $null
$composeCandidates = @(
    $env:VCT_COMPOSE_DIR,
    $env:VCT_INFRASTRUCTURE_DIR,
    $(if ($env:VCT_ORCHESTRATOR_ROOT) { Join-Path $env:VCT_ORCHESTRATOR_ROOT "infrastructure" } else { $null }),
    (Join-Path $projectRoot "infrastructure"),
    (Join-Path $projectRoot "claude_mcp_servers"),
    [string]$projectRoot
)
foreach ($path in $composeCandidates) {
    if (-not $path) { continue }
    if ((Test-Path (Join-Path $path "compose.yaml")) -or `
        (Test-Path (Join-Path $path "compose.yml")) -or `
        (Test-Path (Join-Path $path "docker-compose.yml"))) {
        $composeDir = $path
        break
    }
}

# v0.2.97 (plan invariant I1 + the zombie gate, parity with the .sh
# sibling): only a VCO-managed service (launcher.db service_endpoints plan,
# re-read above after the reconcile) is `rm -f`'d and re-created, by compose
# naming that ONE service with `--no-deps`. An adopted container, or one
# whose service has no row yet, is never removed. No readable plan ->
# nothing is re-created.

foreach ($z in $zombies) {
    $name = $z.Name
    $port = $z.Port
    Write-Output "   → recovering $name (port :$port) via $runtime"
    if ($runtime -eq "podman") {
        if (-not $lcPlan) {
            Write-Output "     ! the service_endpoints plan could not be read - $name left as is (manual: $runtime start $name)"
            Add-VcoLogRecovery -Name $name -Action "left_as_is" -Detail "the service_endpoints plan could not be read"
            continue
        }
        $policy = @($lcPlan.containers | Where-Object { $_.container -eq $name }) | Select-Object -First 1
        if (-not $policy -or $policy.on_zombie -ne 'recreate') {
            # Not VCO-managed (adopted / unlisted / no row yet - ownership
            # unknown): ensure-containers cleans its orphan runtime state and
            # starts it BY NAME; never removed.
            Write-Output "     ! $name is not VCO-managed - never removed or re-created here (manual: $runtime start $name)"
            Add-VcoLogRecovery -Name $name -Action "left_as_is" -Detail "not VCO-managed"
            continue
        }
        if (-not $rowsChecked) {
            # The session reconcile did not complete: the row may be stale.
            Write-Output "     ! the service_endpoints rows could not be re-checked this session - $name left as is (manual: $runtime start $name)"
            Add-VcoLogRecovery -Name $name -Action "left_as_is" -Detail "the service_endpoints rows could not be re-checked"
            continue
        }
        $service = [string]$policy.service
        $upArgs = $null
        try {
            $upArgs = @(((& $RunPy -m vco_lib.service_lifecycle compose-args --json --services $service 2>$null | Out-String) | ConvertFrom-Json).args)
        } catch { $upArgs = $null }
        if (-not $upArgs -or $upArgs.Count -eq 0) {
            Write-Output "     ! no compose argv for $service - $name left as is"
            Add-VcoLogRecovery -Name $name -Action "left_as_is" -Detail "no compose argv"
            continue
        }
        # Podman state-DB desync: force-rm + recreate. `podman restart`
        # is a no-op because Podman thinks the container is alive.
        & $runtime rm -f $name *>$null
        if ($LASTEXITCODE -eq 0) {
            if ($composeDir) {
                Push-Location $composeDir
                try {
                    # v0.2.92: `$composeArgs[1..($composeArgs.Length - 1)]` is
                    # the range `1..0` for a ONE-token compose command
                    # (standalone `podman-compose`), which PowerShell evaluates
                    # DESCENDING as @(1, 0) — invoking `podman-compose
                    # podman-compose up -d <svc>`. Routed through the shared
                    # splitter so all four hook sites share one correct rule.
                    $composeInvocation = Split-VcoComposeCommand -ComposeCmd ($composeArgs -join ' ')
                    & $composeInvocation.Head @($composeInvocation.Rest) @upArgs *>$null
                    if ($LASTEXITCODE -ne 0) {
                        Write-Output "     ! $($composeArgs -join ' ') $($upArgs -join ' ') failed; manual: cd $composeDir; $($composeArgs -join ' ') $($upArgs -join ' ')"
                        Add-VcoLogRecovery -Name $name -Action "failed" -Detail "removed; compose up failed"
                    } else {
                        Add-VcoLogRecovery -Name $name -Action "recreated" -Detail ($upArgs -join ' ')
                    }
                } finally {
                    Pop-Location
                }
            } else {
                Write-Output "     ! could not auto-detect compose dir; manual: $($composeArgs -join ' ') $($upArgs -join ' ')"
                Add-VcoLogRecovery -Name $name -Action "failed" -Detail "removed; no compose directory found"
            }
        } else {
            Write-Output "     ! $runtime rm -f $name failed"
            Add-VcoLogRecovery -Name $name -Action "failed" -Detail "$runtime rm -f failed"
        }
    } else {
        # Docker silent-crash: state DB is reliable, so the app inside
        # has wedged. Restart cycles PID 1 and is enough.
        & $runtime restart $name *>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Output "     ! $runtime restart $name failed; manual: $runtime logs $name"
            Add-VcoLogRecovery -Name $name -Action "failed" -Detail "$runtime restart failed"
        } else {
            Add-VcoLogRecovery -Name $name -Action "restarted"
        }
    }
}
Write-VcoPortCheckLog -Action "recovered"

Write-Output "   recovery complete; first KG/Ollama call may take 20-30s while services warm up"
