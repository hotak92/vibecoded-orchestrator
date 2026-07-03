# stop-codegraph-drain.ps1 -- Stop hook (v0.2.73 FIX-B)
# OS-PARITY: ports templates/hooks/stop-codegraph-drain.sh. END-OF-TURN BATCHED
# code-graph sync. Replaces the per-EDIT code-graph-incremental scheduling that
# post-file-edit used to fire on every code-file Edit (the Weaviate disk
# write-amplification driver). post-file-edit now appends each edited path to a
# per-turn drain queue (.claude/state/codegraph_drain_<sid>.txt); this hook
# drains it at end-of-turn and runs ONE analyzer pass per canonical root,
# subject to a 2-minute per-project rate limit + per-canonical-root
# serialization. Vanished (edited-then-deleted) paths are pruned by the
# analyzer's --only-files-from. FIX-A' worktree gate is applied per path.
#
# Contract: always exit 0; soft-fail throughout. MUST MATCH the .sh sibling.
# Plain ASCII only (no em-dash, no BOM needed).

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

if (Test-Path "$PSScriptRoot/_lib/stderr-cap.ps1") { . "$PSScriptRoot/_lib/stderr-cap.ps1" }
if (Test-Path "$PSScriptRoot/_lib/resolve-powershell.ps1") { . "$PSScriptRoot/_lib/resolve-powershell.ps1" }
# Shared venv resolver — resolves the analyzer's python interpreter from the
# VCO clone's `.venv` (mirrors code-graph-incremental.ps1). MUST dot-source
# this helper for any `.venv` reference (venv-resolver-drift gate).
if (Test-Path "$PSScriptRoot/_lib/resolve-vco-venv.ps1") { . "$PSScriptRoot/_lib/resolve-vco-venv.ps1" }
if (Test-Path "$PSScriptRoot/_lib/canonical-repo-root.ps1") { . "$PSScriptRoot/_lib/canonical-repo-root.ps1" }
if (Test-Path "$PSScriptRoot/_lib/worktree-gate.ps1") { . "$PSScriptRoot/_lib/worktree-gate.ps1" }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

# Rate-limit window (seconds) between drains per project.
$MinInterval = 120
if ($env:VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS -match '^\d+$') {
    $MinInterval = [int]$env:VCO_CODEGRAPH_DRAIN_MIN_INTERVAL_SECONDS
}

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.session_id) { $SessionId = [string]$payload.session_id }
} catch { }
if ([string]::IsNullOrEmpty($SessionId)) { exit 0 }
if ($SessionId -match '[^A-Za-z0-9_-]') { exit 0 }

$stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
$queue = Join-Path $stateDir ("codegraph_drain_{0}.txt" -f $SessionId)
# Session-agnostic SHARED drain queue (v0.2.73 I/O-audit HIGH-2 — must match
# stop-codegraph-drain.sh + subagent-stop-reconcile.*): the SubagentStop
# reconciler enqueues gated-IN subagent code edits here (replacing the removed
# orphan code-graph-queue.jsonl) so the NEXT eligible Stop drain (any session)
# processes them, decoupled from the subagent's session id.
$sharedQueue = Join-Path $stateDir "codegraph_drain_shared.txt"
if (-not (Test-Path -LiteralPath $queue) -and -not (Test-Path -LiteralPath $sharedQueue)) { exit 0 }

# --- RATE LIMIT ---
$lastTsFile = Join-Path $stateDir "codegraph_drain_last_sync.ts"
$now = [int][double]::Parse((Get-Date -UFormat %s))
$lastTs = 0
if (Test-Path -LiteralPath $lastTsFile) {
    try {
        $raw = (Get-Content -LiteralPath $lastTsFile -Raw -ErrorAction Stop).Trim()
        if ($raw -match '^\d+$') { $lastTs = [int]$raw }
    } catch { }
}
if ($lastTs -gt 0 -and (($now - $lastTs) -lt $MinInterval)) {
    # Rate-limited: leave the queue for the next eligible drain.
    exit 0
}

# --- DRAIN: consume queue + shared queue, gate, group by canonical root ---
# Consume the per-session queue (if present) by atomic rename so concurrent
# appends start a fresh queue; then FOLD IN the shared queue (subagent edits)
# the same way. If the per-session queue is absent, seed CONSUMED empty so the
# shared fold still has a target.
$consumed = Join-Path $stateDir ("codegraph_drain_{0}.draining.{1}" -f $SessionId, $PID)
if (Test-Path -LiteralPath $queue) {
    try { Move-Item -LiteralPath $queue -Destination $consumed -Force -ErrorAction Stop }
    catch { try { Set-Content -LiteralPath $consumed -Value $null -ErrorAction Stop } catch { exit 0 } }
} else {
    try { Set-Content -LiteralPath $consumed -Value $null -ErrorAction Stop } catch { exit 0 }
}
if (Test-Path -LiteralPath $sharedQueue) {
    $sharedConsumed = Join-Path $stateDir ("codegraph_drain_shared.draining.{0}" -f $PID)
    try {
        Move-Item -LiteralPath $sharedQueue -Destination $sharedConsumed -Force -ErrorAction Stop
        try { Get-Content -LiteralPath $sharedConsumed -ErrorAction Stop | Add-Content -LiteralPath $consumed } catch { }
        Remove-Item -LiteralPath $sharedConsumed -ErrorAction SilentlyContinue
    } catch { }
}

$defaultRepoRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$analyzer = if ($env:VCT_ANALYZER_SCRIPT) { $env:VCT_ANALYZER_SCRIPT } else { Join-Path $defaultRepoRoot ".claude/scripts/analyze_code_graph.py" }
if (-not (Test-Path -LiteralPath $analyzer)) {
    try { Get-Content -LiteralPath $consumed | Add-Content -LiteralPath $queue } catch { }
    Remove-Item -LiteralPath $consumed -ErrorAction SilentlyContinue
    exit 0
}

# Resolve python (venv preferred via the shared resolver, else system).
$python = $env:VCT_PYTHON
if (-not $python -and (Get-Command Resolve-VcoVenvPython -ErrorAction SilentlyContinue)) {
    $python = Resolve-VcoVenvPython -ScriptDir $ScriptDir
}
if (-not $python) {
    foreach ($c in @('python','py','python3')) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { $python = $cmd.Source; break }
    }
}
if (-not $python) {
    try { Get-Content -LiteralPath $consumed | Add-Content -LiteralPath $queue } catch { }
    Remove-Item -LiteralPath $consumed -ErrorAction SilentlyContinue
    exit 0
}

$codeRe = '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$'

function Resolve-DrainProject {
    param([string]$Root)
    $name = ""
    $resolver = Join-Path $Root ".claude/scripts/vct_project_config.ps1"
    if (Test-Path $resolver) {
        try { $name = (& $PsExe -NoProfile -File $resolver -Project $Root -Field "code_graph_collection_prefix" 2>$null) } catch { $name = "" }
    }
    if (-not $name) { $name = Split-Path $Root -Leaf }
    return ("" + $name).Trim()
}
function Resolve-DrainDotClaude {
    param([string]$Root)
    $val = ""
    $resolver = Join-Path $Root ".claude/scripts/vct_project_config.ps1"
    if (Test-Path $resolver) {
        try { $val = (& $PsExe -NoProfile -File $resolver -Project $Root -Field "code_graph_index_dot_claude" 2>$null) } catch { $val = "" }
    }
    switch -Regex ($val) {
        '^(true|True|TRUE|1)$'  { return '--index-dot-claude' }
        '^(false|False|FALSE|0)$' { return '--no-index-dot-claude' }
    }
    if ((Test-Path (Join-Path $Root "vco_lib")) -and (Test-Path (Join-Path $Root ".claude"))) {
        return '--index-dot-claude'
    }
    return '--no-index-dot-claude'
}

# Group surviving paths by canonical root.
$byRoot = @{}          # hash -> list of paths
$rootForHash = @{}     # hash -> canonical root
$seen = @{}
$lines = @()
try { $lines = @(Get-Content -LiteralPath $consumed -ErrorAction Stop) } catch { }
foreach ($p in $lines) {
    if ([string]::IsNullOrWhiteSpace($p)) { continue }
    if ($seen.ContainsKey($p)) { continue }
    $seen[$p] = $true
    if ($p -notmatch $codeRe) { continue }
    if ($p -match '[\\/]\.claude[\\/]state[\\/]') { continue }

    $canon = Get-CanonicalRepoRoot -File $p
    if (-not $canon) {
        $canon = Get-CanonicalRepoRoot -File (Join-Path $ProjectRoot ".")
        if (-not $canon) { $canon = $ProjectRoot }
    }
    if (Test-EphemeralWorktreeEdit -EditedFile $p -RepoPath $ProjectRoot -CanonRoot $canon) { continue }

    $h = [System.BitConverter]::ToString(
        [System.Security.Cryptography.MD5]::Create().ComputeHash(
            [System.Text.Encoding]::UTF8.GetBytes($canon))).Replace('-','').ToLower()
    $rootForHash[$h] = $canon
    if (-not $byRoot.ContainsKey($h)) { $byRoot[$h] = New-Object System.Collections.Generic.List[string] }
    [void]$byRoot[$h].Add($p)
}

# Nothing survived -> advance the rate-limit clock + clean up.
if ($byRoot.Count -eq 0) {
    Set-Content -LiteralPath $lastTsFile -Value "$now" -NoNewline -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $consumed -ErrorAction SilentlyContinue
    exit 0
}

# Stale-lock breaker (v0.2.73 I/O-audit HIGH-1 — must match stop-codegraph-drain.sh):
# the per-root lock dir below is released only inside the detached analyzer's
# cleanup. If that process dies before release (kill, OOM, out-of-disk — the exact
# condition this effort targets, or a reboot) the lock leaks and that root's code
# graph freezes silently forever. Break locks older than the max plausible analyzer
# runtime (30 min) so a dead drain self-heals on the next turn.
try {
    Get-ChildItem -LiteralPath $stateDir -Directory -Filter 'codegraph_drain_root_*.lock' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddMinutes(-30) } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
} catch {}

foreach ($h in @($byRoot.Keys)) {
    $canon = $rootForHash[$h]
    $project = Resolve-DrainProject -Root $canon
    $dotFlag = Resolve-DrainDotClaude -Root $canon

    # Per-canonical-root serialization: atomic dir-create lock.
    $lock = Join-Path $stateDir ("codegraph_drain_root_{0}.lock" -f $h)
    $gotLock = $false
    try { New-Item -ItemType Directory -Path $lock -ErrorAction Stop | Out-Null; $gotLock = $true } catch { $gotLock = $false }
    if (-not $gotLock) {
        # A prior drain for this root is running -> requeue its paths.
        try { $byRoot[$h] | Add-Content -LiteralPath $queue } catch { }
        continue
    }

    $listFile = Join-Path $stateDir ("codegraph_drain_list_{0}.txt" -f $h)
    try { Set-Content -LiteralPath $listFile -Value $byRoot[$h] -ErrorAction Stop } catch { }

    # Detached background run holding the per-root lock for the whole analyzer
    # run, then releasing it + the list file.
    $argList = @($analyzer, $canon, '--project', $project, '--only-files-from', $listFile, '--canonical-source', $canon, $dotFlag)
    $cleanup = "try { Remove-Item -LiteralPath '$listFile' -ErrorAction SilentlyContinue } catch {}; try { Remove-Item -LiteralPath '$lock' -Recurse -Force -ErrorAction SilentlyContinue } catch {}"
    $inner = "& '$python' " + (($argList | ForEach-Object { "'" + ($_ -replace "'","''") + "'" }) -join ' ') + " *> `$null; $cleanup"
    Start-Process -FilePath $PsExe -ArgumentList @('-NoProfile','-Command',$inner) -WindowStyle Hidden | Out-Null
}

Set-Content -LiteralPath $lastTsFile -Value "$now" -NoNewline -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $consumed -ErrorAction SilentlyContinue
exit 0
