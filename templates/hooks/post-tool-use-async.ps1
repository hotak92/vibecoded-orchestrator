# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# post-tool-use-async.ps1 - Windows sibling of post-tool-use-async.sh
# (v0.2.101): the ONE async PostToolUse dispatcher.
#
# WHY THIS HOOK EXISTS (transcript-bloat fix, measured metadata-only):
# Pre-v0.2.101 the settings templates carried EIGHT async PostToolUse
# registrations across six scripts (post-edit-outcome, post-bash-context-
# record, kg-summary-generator x3, post-git-commit-kg-sync, post-file-
# delete, kg-update-nudge). One Bash tool call spawned up to 3 async
# processes, and every async run that SPOKE (any stdout/stderr) or DIED
# (timeout) wrote an async_hook_response attachment (~660 B) into the
# session transcript. One maintainer transcript held 700,330 such records /
# 462.3 MB. This dispatcher replaces those eight registrations with ONE
# (matcher *, async, timeout 15): 3 spawns -> 1 per tool call, <= 1
# transcript record per tool call and only when the dispatcher itself is
# killed - because it guarantees silence (below).
#
# WHAT IT DOES (mirrors the .sh sibling step for step)
#   1. Reads the hook stdin ONCE into a temp file every child shares.
#   2. Parses the routing fields (tool_name; whether tool_input.command
#      starts with "git commit") with the same find-python + inline-Python
#      idiom every sub-hook uses - no second parsing mechanism.
#   3. Routes over the $RouteTable below: tool equality or *, with the
#      git-commit-prefix gate carrying the retired registration key
#      "if: Bash(git commit *)" (word-boundary: "git commit" exactly or
#      followed by a space - "git commit-tree" does NOT fire the review
#      agent). The retired "if: Edit|Write(knowledge/**.md)" keys are
#      enforced by kg-summary-generator's OWN knowledge-path validation
#      (unchanged body - one home preserved; this file is a router, not a
#      re-implementation. That delegation costs one short-lived no-op
#      python per non-knowledge Edit/Write - review N-4, kept deliberately:
#      any dispatcher-side pre-filter would be a SECOND home of the
#      knowledge-path rule, and the spawn is concurrent and off the
#      critical path). A stem listed in VCO_ASYNC_DISABLED_HOOKS
#      (<project>/.claude/env - the launcher's Hooks-tab sub-hook toggle)
#      is skipped (review SF-2).
#   4. Runs every matched sub-hook CONCURRENTLY through the ONE spawn home
#      (_lib/resolve-powershell.ps1 Start-VcoDetachedProcess, -PassThru +
#      WaitForExit) - the wall-time shape the separate async registrations
#      had; the single 15 s registration timeout bounds the fan-out (the
#      largest retired per-hook budget was 10 s).
#   5. GUARANTEES SILENCE: this process' Console stdout/stderr are
#      redirected away from the harness before any work; child stdout is
#      discarded; child stderr and non-zero exits condense to ONE line per
#      failure in "<VCO metrics dir>\post-tool-use-async.log"
#      (_lib/metrics-dir.ps1 resolves the home). Always exits 0 -
#      PostToolUse cannot block, and any output would be transcript bloat
#      again.
#
# BEHAVIOUR PRESERVED PER SUB-HOOK: each script still ships unchanged,
# receives the exact stdin bytes the harness gave (temp-file redirect),
# keeps its own internal gates and exits; env (VCT_KG_ACCESS_LIST,
# CLAUDE_PROJECT_DIR, ...) is inherited exactly as it was when the harness
# spawned each script directly.
#
# The retired registrations are declared in vco_lib/hook_retirements.py
# (event PostToolUse, retired_in v0.2.101) so EXISTING installs lose them
# at the next bundle update; the nudge's SYNC UserPromptSubmit +
# SessionStart(compact) registrations are event-scoped out of that and
# stay alive.
#
# VCO-CENTRALIZED-KG: router (PR #171 / 0.1.7 classification). Touches no
#   Weaviate collection itself; routes PostToolUse payloads to the
#   classified scripts (kg-summary-generator = write-side delegator,
#   kg-update-nudge = counter-only, post-git-commit-kg-sync = spawns
#   claude). The access matrix travels to the children via env
#   inheritance, exactly as before the merge.
#
# MUST MATCH: post-tool-use-async.sh - the $RouteTable below is mirrored
# byte-for-byte in the sibling (shared-config tier of the cross-language
# rule); tests/test_v02101_async_posttooluse_dispatcher.py parses BOTH and
# pins them equal, and pins the stem set against the v0.2.101 retirement
# rows so a sub-hook can never be routed on one OS only.
#
# Routing table format: <tool_name or *>|<script stem>|<gate>
#   gate -                 = always route on a tool match
#   gate git-commit-prefix = only when tool_input.command starts with
#                            "git commit" (the retired "if" key)

# Scrub sensitive env vars before any subprocess spawning.
$secretEnvNames = @(
    "SUPABASE_KEY", "SUPABASE_URL", "GITHUB_TOKEN", "GH_TOKEN",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID", "TELEGRAM_BOT_TOKEN", "POSTGRES_PASSWORD",
    "VERCEL_TOKEN", "CLAUDE_API_KEY"
)
foreach ($name in $secretEnvNames) {
    if (Test-Path "Env:$name") { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# ROUTING TABLE - the ONE declaration (parsed by the loop below; the row
# lines are mirrored byte-for-byte in the .sh sibling's ROUTE_TABLE and both
# are read by the tests). Do not reformat the rows.
# NOTE: a plain multi-line single-quoted string, deliberately NOT a @'...'@
# here-string: tests/test_v0291_dogfood_deferral_selfclear.py treats EVERY
# single-quoted here-string in a shipped .ps1 as an embedded-Python payload
# and its AST gate reports a parse failure as a hit (loud, by design) - a
# non-Python here-string would be flagged as a partition offender.
$RouteTable = '
Edit|post-edit-outcome|-
Edit|kg-summary-generator|-
Write|post-edit-outcome|-
Write|kg-summary-generator|-
Bash|post-bash-context-record|-
Bash|post-git-commit-kg-sync|git-commit-prefix
Bash|post-file-delete|-
mcp__weaviate-kg__store_knowledge_node|kg-summary-generator|-
*|kg-update-nudge|-
'

$ScriptDir = $PSScriptRoot

# Resolve the failure log BEFORE going silent (Get-VcoMetricsDir creates
# its target; an empty result logs nowhere and never fails the hook -
# conservative default on a best-effort path).
$AsyncLog = $null
$MetricsLib = Join-Path $ScriptDir "_lib/metrics-dir.ps1"
if (Test-Path -LiteralPath $MetricsLib -PathType Leaf) {
    try {
        . $MetricsLib
        $mdir = Get-VcoMetricsDir
        if ($mdir) { $AsyncLog = Join-Path $mdir "post-tool-use-async.log" }
    } catch { $AsyncLog = $null }
}

# SILENCE GUARANTEE + ONE LOG WRITER (v0.2.101 review SF-1): this process'
# Console streams are redirected away from the harness exactly ONCE, here,
# and EVERY failure-log line goes through that one redirected writer (see
# Write-VcoAsyncLogLine below). The shared spawn helper's soft-fail line
# ([Console]::Error) lands in the same log through the same writer.
#
# DO NOT add a second writer ([System.IO.File]::AppendAllText and friends):
# the StreamWriter below holds the log with FileShare.Read, so on Windows a
# separate append-open throws IOException and a naive catch swallows it -
# EVERY structured line would be silently dropped (that was the SF-1
# defect). The drop is invisible on Unix, where .NET does not enforce
# FileShare - which is exactly why the structural pin lives in
# tests/test_v02101_async_posttooluse_dispatcher.py. The writers are
# deliberately never disposed: this is a one-shot process, and disposing
# while a child's redirect is still flushing could truncate a line;
# process exit closes the handles.
$script:VcoAsyncLogReady = $false
try {
    if ($AsyncLog) {
        $errWriter = New-Object System.IO.StreamWriter($AsyncLog, $true)
    } else {
        $errWriter = New-Object System.IO.StreamWriter([System.IO.Stream]::Null)
    }
    $errWriter.AutoFlush = $true
    [Console]::SetError($errWriter)
    $outWriter = New-Object System.IO.StreamWriter([System.IO.Stream]::Null)
    $outWriter.AutoFlush = $true
    [Console]::SetOut($outWriter)
    $script:VcoAsyncLogReady = $true
} catch { }

function Write-VcoAsyncLogLine {
    param([Parameter(Mandatory = $true)][string]$Line)
    # Through the ONE redirected writer: the null stream when no log could
    # be resolved, and NOTHING when the redirect itself failed (ready flag
    # false) - Console.Error would still be the harness' pipe then, and the
    # silence guarantee outranks the log.
    if ($script:VcoAsyncLogReady) {
        try { [Console]::Error.WriteLine($Line) } catch { }
    }
}

# PER-SUB-HOOK DISABLE (v0.2.101 review SF-2): a stem listed in
# VCO_ASYNC_DISABLED_HOOKS (<project>/.claude/env - the SAME per-project
# hook-knob channel lean-ctx's VCO_LEAN_CTX_DEFAULT uses; comma-separated
# stems) is skipped, so the merge kept the per-registration toggle the
# eight retired async entries had. The launcher's Hooks tab writes the key
# (set_claude_env_value, the lean-ctx toggle's own command) and a bundle
# update carries a pre-merge parked disable into it
# (vco_lib.hook_retirements.carry_parked_async_disables). Read the way
# lean-ctx-rewrite.ps1 reads its knob: a targeted line scan of the anchored
# file, last match wins, file value outranks a pre-set process env.
$ProjectRoot = $env:CLAUDE_PROJECT_DIR
if (-not $ProjectRoot) {
    try { $ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "../..")) } catch { $ProjectRoot = $null }
}
$DisabledRaw = $env:VCO_ASYNC_DISABLED_HOOKS
if ($ProjectRoot) {
    $EnvFile = Join-Path $ProjectRoot ".claude/env"
    if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
        try {
            foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
                if ($line -match '^\s*(?:export\s+)?VCO_ASYNC_DISABLED_HOOKS\s*=\s*(.+?)\s*$') {
                    $DisabledRaw = $Matches[1]
                }
            }
        } catch { }
    }
}
# N-B (re-review): strip ONE surrounding quote pair via the SHARED home
# _lib/strip-one-quote-pair.ps1 (lean-ctx-rewrite.ps1 dots the same helper).
# The .sh sibling SOURCES the file, where a hand-edited
# VCO_ASYNC_DISABLED_HOOKS="a,b" arrives unquoted; this scan captures the
# raw text, so the quoted spelling would be silently inert on Windows
# without this step. Dotted HERE -- AFTER the silence redirect above -- so a
# helper that ever printed could not reach the harness; guarded so an absent
# helper leaves the value unstripped (partial install) and never errors.
$QuotePairLib = Join-Path $ScriptDir "_lib/strip-one-quote-pair.ps1"
if (Test-Path -LiteralPath $QuotePairLib -PathType Leaf) { . $QuotePairLib }
if ($DisabledRaw -and (Get-Command Strip-OneQuotePair -ErrorAction SilentlyContinue)) {
    $DisabledRaw = Strip-OneQuotePair $DisabledRaw
} elseif ($DisabledRaw -and ($DisabledRaw.StartsWith('"') -or $DisabledRaw.StartsWith("'"))) {
    # Partial install (helper missing): a quoted knob would parse into
    # garbage stems and silently not apply -- say so in the failure log.
    Write-VcoAsyncLogLine "VCO_ASYNC_DISABLED_HOOKS is quoted but _lib/strip-one-quote-pair.ps1 is missing; the value is used unstripped (re-run the bundle update)"
}
$DisabledStems = @()
if ($DisabledRaw) {
    $DisabledStems = @($DisabledRaw -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$TmpInput = $null
$WorkDir = $null
try {
    # Read stdin ONCE to a temp file every sub-hook shares.
    $payload = [Console]::In.ReadToEnd()
    if (-not $payload) { exit 0 }
    $TmpInput = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($TmpInput, $payload)
    $WorkDir = Join-Path ([System.IO.Path]::GetTempPath()) ("vco-post-tool-async-" + [System.Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $WorkDir -ErrorAction Stop | Out-Null

    # Routing fields, one Python call (same idiom as the sub-hooks):
    # line 1 = tool_name, line 2 = "1" when tool_input.command starts with
    # "git commit" (the retired registration gate), else "0".
    $FindPy = Join-Path $ScriptDir "_lib/find-python.ps1"
    if (Test-Path -LiteralPath $FindPy -PathType Leaf) { . $FindPy }
    if (-not $PY) { exit 0 }  # No Python - cannot route; sub-hooks need it too
    $parseSrc = @'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8", errors="replace") as f:
        d = json.load(f)
except Exception:
    d = {}
if not isinstance(d, dict):
    d = {}
ti = d.get("tool_input")
cmd = (ti.get("command") or "") if isinstance(ti, dict) else ""
c = cmd.lstrip()
print(d.get("tool_name") or "")
print("1" if (c == "git commit" or c.startswith("git commit ")) else "0")
'@
    $fields = @(& $PY -c $parseSrc $TmpInput 2>$null)
    $ToolName = if ($fields.Count -ge 1) { [string]$fields[0] } else { "" }
    $IsGitCommit = if ($fields.Count -ge 2) { [string]$fields[1] } else { "0" }

    # Concurrent fan-out + wait, through the ONE spawn home (its
    # -WindowStyle guard is what keeps Start-Process spawnable on every
    # edition; -PassThru gives the waitable process object).
    # KNOWN PLATFORM ARTIFACT (accepted, documented): PowerShell's
    # Start-Process -RedirectStandardInput hands the child the file's bytes
    # PLUS a trailing newline. Every sub-hook parses stdin as JSON, where
    # trailing whitespace is inert, so byte-fidelity of the payload CONTENT
    # is preserved (pinned modulo that one newline by
    # tests/test_v02101_async_posttooluse_dispatcher.py). The .sh sibling
    # passes the temp file as the child's stdin directly and is byte-exact.
    . (Join-Path $ScriptDir "_lib/resolve-powershell.ps1")
    $childWorkDir = $null
    try { $childWorkDir = (Get-Location).Path } catch { $childWorkDir = $null }
    $procs = @()
    $procNames = @()
    $errFiles = @()
    foreach ($line in ($RouteTable -split "\r?\n")) {
        $line = $line.Trim()
        if (-not $line) { continue }
        $parts = $line -split '\|'
        if ($parts.Count -lt 3) { continue }
        $tool = $parts[0]
        $scriptStem = $parts[1]
        $gate = $parts[2]
        if ($tool -ne '*' -and $tool -ne $ToolName) { continue }
        if ($gate -eq 'git-commit-prefix' -and $IsGitCommit -ne '1') { continue }
        if ($DisabledStems -contains $scriptStem) { continue }  # per-sub-hook disable (SF-2)
        $childPath = Join-Path $ScriptDir ($scriptStem + ".ps1")
        if (-not (Test-Path -LiteralPath $childPath -PathType Leaf)) {
            $ts = [DateTime]::UtcNow.ToString("s") + "Z"
            Write-VcoAsyncLogLine "$ts post-tool-use-async $scriptStem missing"
            continue
        }
        $errFile = Join-Path $WorkDir ($scriptStem + ".err")
        $outFile = Join-Path $WorkDir ($scriptStem + ".out")
        $spawnArgs = @{
            FilePath               = $PsExe
            ArgumentList           = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $childPath)
            RedirectStandardInput  = $TmpInput
            RedirectStandardOutput = $outFile
            RedirectStandardError  = $errFile
            PassThru               = $true
        }
        if ($childWorkDir) { $spawnArgs['WorkingDirectory'] = $childWorkDir }
        $p = Start-VcoDetachedProcess @spawnArgs
        if ($p) {
            $procs += $p
            $procNames += $scriptStem
            $errFiles += $errFile
        }
    }
    for ($i = 0; $i -lt $procs.Count; $i++) {
        try { $procs[$i].WaitForExit() } catch { }
        $rc = 0
        try { $rc = $procs[$i].ExitCode } catch { $rc = -1 }
        $errText = ""
        try {
            if (Test-Path -LiteralPath $errFiles[$i] -PathType Leaf) {
                $errText = [System.IO.File]::ReadAllText($errFiles[$i])
            }
        } catch { $errText = "" }
        if ($rc -ne 0 -or ($errText -and $errText.Trim())) {
            $flat = ($errText -replace "[\r\n]+", " ")
            if ($flat.Length -gt 500) { $flat = $flat.Substring(0, 500) }
            $ts = [DateTime]::UtcNow.ToString("s") + "Z"
            Write-VcoAsyncLogLine "$ts post-tool-use-async $($procNames[$i]) exit=$rc stderr=$flat"
        }
        try {
            if (Test-Path -LiteralPath $errFiles[$i] -PathType Leaf) { Remove-Item -LiteralPath $errFiles[$i] -Force }
        } catch { }
    }
} catch {
    $ts = [DateTime]::UtcNow.ToString("s") + "Z"
    Write-VcoAsyncLogLine "$ts post-tool-use-async dispatcher error: $($_.Exception.Message)"
} finally {
    # Runs on every in-process exit path (PowerShell `exit` inside try DOES
    # run finally - pinned by the driven tests' clean-TMPDIR assertions).
    # A HARD process kill (the harness timeout's last resort) cannot run it
    # - the 0600-equivalent (user-temp-ACL) payload file then leaks into
    # the user's temp dir; documented limit, same class as the .sh sibling's
    # untrappable SIGKILL (review N-3).
    if ($TmpInput) {
        try { if (Test-Path -LiteralPath $TmpInput -PathType Leaf) { Remove-Item -LiteralPath $TmpInput -Force } } catch { }
    }
    if ($WorkDir) {
        try { if (Test-Path -LiteralPath $WorkDir -PathType Container) { Remove-Item -LiteralPath $WorkDir -Recurse -Force } } catch { }
    }
}

exit 0
