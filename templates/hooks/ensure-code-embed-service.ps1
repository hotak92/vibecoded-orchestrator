# Scrub sensitive env vars before any subprocess
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# ensure-code-embed-service.ps1
# Ensure the code embedding service container is running.
# Mirror of ensure-code-embed-service.sh. No flock on Windows; we use a
# best-effort lockfile (sentinel) instead.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
. "$PSScriptRoot/_lib/compose-invocation.ps1"

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

# Container runtime + compose: ONE home - `python -m vco_lib.containers resolve`
# (v0.2.92 PLAN-EXTENSION section 3.5 / R13). This hook used to mirror the
# podman/docker + compose-form detection inline (as did two sibling hooks,
# install.py and the launcher), and the four copies had drifted in their
# compose preference order. Class A of the A>B>C rule: one Python
# implementation, called via a ~50 ms subprocess on this session-start path.
# Loud-fail: if the resolver cannot run at all (no interpreter, broken
# install), say so on stderr and skip - never fall back to an inline copy.
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
    Write-Output "ensure-code-embed-service: no Python interpreter for vco_lib.containers (broken VCO install?); skipping"
    exit 0
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
    Write-Output "ensure-code-embed-service: vco_lib.containers resolve failed (rc=$VcoRtRc): $VcoRtWhy; skipping"
    exit 0
}
Remove-Item $VcoRtErr -ErrorAction SilentlyContinue
# v0.2.92 BLOCKER-4 + MAJOR-6: the resolver REFUSES a pinned-but-unusable
# runtime (podman and docker have per-runtime named volumes, so driving the
# one the user did not pin brings the stack up EMPTY) and hands back the
# refusal as its reason. Report it on STDOUT, not stderr: a SessionStart
# hook's stderr is not surfaced to the user when the hook exits 0 - only
# stdout is injected as session context, and an unread report is not a report.
if ($VcoRt.state -ne "resolved") {
    Write-Output "ensure-code-embed-service: $($VcoRt.reason); skipping"
    exit 0
}
$Runtime = $VcoRt.runtime
$ComposeCmd = if ($env:VCT_COMPOSE_CMD) { $env:VCT_COMPOSE_CMD } elseif ($VcoRt.compose) { ($VcoRt.compose -join " ") } else { "" }

# Best-effort lock: if another session set the file in last 30s, bail.
if (Test-Path $LockFile) {
    $age = ((Get-Date) - (Get-Item $LockFile).LastWriteTime).TotalSeconds
    if ($age -lt 30) {
        Write-Output "[code_embed] Another session is starting the service, skipping"
        exit 0
    }
}
New-Item -ItemType File -Path $LockFile -Force | Out-Null

# v0.2.92 BLOCKER-1 (parity with report_code_embed_staleness in the .sh sibling):
# this service is the ONLY one whose image is BUILT from the checkout, and
# `compose up` builds only when the image is MISSING - so it can serve
# months-old code through every update, silently truncating over-window input
# at HTTP 200 instead of refusing it. One line, only when the running service's
# self-reported source digest does not match this checkout's;
# `--quiet-unless-stale` prints nothing otherwise. Write-Output, not
# Write-Error: a SessionStart hook's stderr is discarded when it exits 0.
function Report-VcoCodeEmbedStaleness {
    if (-not $RunPy) { return }
    try {
        & $RunPy -m vco_lib.code_embed_image --quiet-unless-stale 2>$null |
            ForEach-Object { Write-Output $_ }
    } catch { }
}

try {
    # Already running?
    $status = $null
    try {
        $status = (& $Runtime container inspect $ContainerName --format '{{.State.Status}}' 2>$null | Out-String).Trim()
    } catch { }
    if ($status -eq "running") {
        if (Test-PortOpen -port ([int]$Port) -timeoutSec 3) {
            Write-Output "[code_embed] Already running on port $Port"
            Report-VcoCodeEmbedStaleness
            exit 0
        }
        Write-Output "[code_embed] Container running but not responding, restarting..."
        try { & $Runtime restart $ContainerName 2>$null | Out-Null } catch { }
        exit 0
    }

    # Port in use by something else?
    if (Test-PortOpen -port ([int]$Port) -timeoutSec 2) {
        Write-Output "[code_embed] Port $Port already in use (external process)"
        Report-VcoCodeEmbedStaleness
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
            # v0.2.92 BLOCKER-1: `--build` here, for the same reason as the .sh
            # sibling - we are CREATING this container, so a build is already on
            # the critical path when no image exists; the flag only adds cost in
            # the one case that must not be skipped (an image built from older
            # source). Scoped to `code_embed`, so no other service is touched.
            $composeInvocation = Split-VcoComposeCommand -ComposeCmd $ComposeCmd
            $cmdHead = $composeInvocation.Head
            $cmdRest = @($composeInvocation.Rest)
            $output = & $cmdHead @cmdRest up -d --build code_embed 2>&1
            $output | Select-Object -Last 3 | ForEach-Object { Write-Output $_ }
        } finally { Pop-Location }
        # The runtime, not an exit code, decides whether the retry is needed.
        $created = $false
        try {
            & $Runtime container inspect $ContainerName 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { $created = $true }
        } catch { }
        if (-not $created) {
            # Older compose implementations reject `--build` on `up`. Retry
            # without it rather than leaving the service down; the image then
            # stays stale, which Report-VcoCodeEmbedStaleness and `vco doctor`
            # both surface.
            Write-Output "[code_embed] compose up --build did not create the container - retrying without --build"
            Push-Location $ComposeDir
            try {
                $output = & $cmdHead @cmdRest up -d code_embed 2>&1
                $output | Select-Object -Last 3 | ForEach-Object { Write-Output $_ }
            } finally { Pop-Location }
            Write-Output "[code_embed] NOTE: the image was NOT rebuilt from source; run 'python install.py --update' from the orchestrator root to refresh it."
        }
        Write-Output "[code_embed] Started container $ContainerName on port $Port"
    }
} finally {
    # Touch the lock file mtime so it expires naturally.
    try { (Get-Item $LockFile).LastWriteTime = Get-Date } catch { }
}
exit 0
