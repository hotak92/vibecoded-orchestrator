# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# v0.2.18 (Commit 11): surface embedding-backend failure hints to Claude.
#
# Purpose
#   When EmbeddingService.for_project() fails because no backend is reachable,
#   vco_lib/embedding_service.py writes a Claude-readable hint to
#   .claude/context/EMBEDDING_FAILURES.md (and a JSONL diagnostic to
#   <metrics dir>\embedding_failures.jsonl). The MD file is auto-cleared the
#   next time construction succeeds. This SessionStart hook surfaces the hint
#   (when it exists) into the current Claude Code session so the LLM has
#   immediate context about the broken state.
#
# v0.2.92 (W4) second surface: the OUTAGE banner is blind to everything that
#   is not a construction-time NoEmbeddingBackendError. The shrink-on-refusal
#   path (a real fidelity loss on the ACTIVE slot, previously disclosed only
#   by a WARNING to stderr nobody reads) and the floor refusal (slot got NO
#   vector) are meant to reach the SAME jsonl as "kind": "shrink_summary"
#   rows — ONE row per run, never per chunk. BEFORE CREDITING THE LEG, CHECK
#   BOTH HALVES: reader = this wrapper + vco_lib.embedding_fidelity; writers
#   = kg-sync (outage rows) and embedding_service._embed_shrinking_on_overflow
#   via _note_fidelity (shrink/floor rows). Both were live as of v0.2.92, but
#   the shrink writer spent part of that cycle unwired while this comment
#   credited it — re-run the grep in that module's "Wiring status" docstring
#   rather than trusting this paragraph. Silence here means "no NEW summary
#   row", never "nothing shrank" — and, since v0.2.92 MAJOR-3, never "the
#   reader is broken" either: a failed spawn prints a named diagnostic on
#   stdout instead of being swallowed by `2>$null` + `catch`. The
#   fidelity leg below surfaces those rows as a SEPARATE, lower-severity
#   notice ("NOT an outage"), spawning the SAME python module the .sh sibling
#   does (the A-leg of the cross-language rule: one implementation, thin
#   wrappers). A per-project marker (.claude/state/embedding-fidelity.seen)
#   deduplicates; the size gate keeps the common nothing-new session at one
#   length check — no python spawn. Mirrors the .sh sibling.
#
# Second job (v0.2.92 W7): this is the SessionStart hook that owns the metrics
#   directory, so it is also where the once-per-machine COPY of the legacy
#   ~/.claude/metrics archive into ~/.vct/metrics is triggered
#   (`Invoke-VcoMetricsMigrateOnce`). It costs one directory test on a machine
#   with nothing to copy and one file test after the first success; the Python
#   is spawned only while a copy is genuinely owed. Deliberately BEFORE the
#   early exits below, because the copy is owed whether or not an embedding
#   failure happened to be recorded. Mirrors the .sh sibling.
#
# Idempotent — safe to run on every SessionStart even when there's nothing
# to surface. Soft-fails throughout; never blocks SessionStart.

# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# Metrics home + the once-per-machine archive COPY. `_lib/metrics-dir.ps1` is
# the ONE PowerShell-side resolver (lockstep sibling of `_lib/metrics-dir.sh`).
# A missing helper leaves the path unresolved and the hook still surfaces the
# hint — it just has no absolute path to name.
$MetricsLibLoaded = $false
$MetricsLib = Join-Path $PSScriptRoot "_lib/metrics-dir.ps1"
if (Test-Path -LiteralPath $MetricsLib -PathType Leaf) {
    . $MetricsLib
    $MetricsLibLoaded = $true
    try { Invoke-VcoMetricsMigrateOnce -ScriptDir $PSScriptRoot } catch { }
}

# Discover install root — same anchor convention as ensure-containers.ps1.
# $env:CLAUDE_PROJECT_DIR is set by Claude Code; git toplevel is the fallback.
$InstallRoot = $env:CLAUDE_PROJECT_DIR
if (-not $InstallRoot) {
    try {
        $InstallRoot = (& git rev-parse --show-toplevel 2>$null).Trim()
    } catch {
        $InstallRoot = $null
    }
}
if (-not $InstallRoot) {
    # No project context — running outside any VCO project.
    exit 0
}

$HintFile = Join-Path $InstallRoot ".claude\context\EMBEDDING_FAILURES.md"

# ── v0.2.92 W4: fidelity leg (shrink summaries / floor refusals) ────────────
# Runs for every project with a metrics jsonl, INDEPENDENT of the outage MD:
# the two are different severities with different writers. Cheap size gate —
# only when the jsonl has grown past this project's marker do we spawn the
# python that renders (and re-writes) the notice. Mirrors the .sh sibling.
$FidelityJsonl = ""
if ($MetricsLibLoaded) {
    $FidelityJsonl = Get-VcoMetricsReadFile -Name "embedding_failures.jsonl"
}
if ($FidelityJsonl -and (Test-Path -LiteralPath $FidelityJsonl -PathType Leaf)) {
    $Marker = Join-Path $InstallRoot ".claude\state\embedding-fidelity.seen"
    $Seen = 0
    if (Test-Path -LiteralPath $Marker -PathType Leaf) {
        try {
            $Seen = [int]((Get-Content -LiteralPath $Marker -Raw) |
                Select-String -Pattern '"offset":\s*(\d+)' |
                Select-Object -First 1 |
                ForEach-Object { $_.Matches[0].Groups[1].Value })
        } catch { $Seen = 0 }
    }
    $Size = (Get-Item -LiteralPath $FidelityJsonl).Length
    if ($Size -gt $Seen) {
        # Prefer the VCO venv-Python so `import vco_lib.embedding_fidelity`
        # works; NEVER the user's project venv. Mirrors the other hooks.
        $VcoVenvPython = $null
        $VenvLib = Join-Path $PSScriptRoot "_lib/resolve-vco-venv.ps1"
        if (Test-Path -LiteralPath $VenvLib -PathType Leaf) {
            . $VenvLib
            try {
                $VcoVenvPython = Resolve-VcoVenvPython -ScriptDir $PSScriptRoot
            } catch { $VcoVenvPython = $null }
        }
        if ($VcoVenvPython -and (Test-Path -LiteralPath $VcoVenvPython)) {
            try {
                # v0.2.92 MAJOR-3 — FAILURE MUST BE DISCOVERABLE. `2>$null`
                # plus the swallowing `catch` made a broken vco_lib import
                # (the documented shadow-copy state) an invisible, permanent
                # silence on the ONE surface that reports embedding-fidelity
                # loss. Stderr goes to a temp file; a non-zero exit is
                # reported on STDOUT, which Claude Code injects as a
                # system-reminder (a SessionStart hook's stderr is
                # discarded on exit 0, so it would be no more visible than
                # nothing). Still soft-fail: exit code untouched. Mirrors
                # the .sh sibling.
                $FidelityErrFile = [System.IO.Path]::GetTempFileName()
                try {
                    & $VcoVenvPython -m vco_lib.embedding_fidelity notice `
                        --project-root "$InstallRoot" 2>$FidelityErrFile
                    $FidelityRc = $LASTEXITCODE
                    if ($FidelityRc -ne 0) {
                        $FidelityLast = ""
                        try {
                            $FidelityLast = (Get-Content -LiteralPath $FidelityErrFile -ErrorAction SilentlyContinue |
                                Where-Object { $_ -and $_.Trim() } |
                                Select-Object -Last 1)
                        } catch { }
                        if (-not $FidelityLast) { $FidelityLast = "(no stderr)" }
                        Write-Output "[embedding-failures-surface] the embedding-fidelity notice could not run (exit $FidelityRc)."
                        Write-Output "  interpreter: $VcoVenvPython"
                        Write-Output "  error:       $FidelityLast"
                        Write-Output "  Until this is fixed, shrink/refusal fidelity loss is recorded but never reported."
                        Write-Output "  Usually a broken or stale vco_lib install: run ``python install.py --update``"
                        Write-Output "  from the orchestrator root, then check that ``import vco_lib`` resolves"
                        Write-Output "  to a path inside that checkout."
                    }
                } finally {
                    Remove-Item -LiteralPath $FidelityErrFile -Force -ErrorAction SilentlyContinue
                }
            } catch {
                Write-Output "[embedding-failures-surface] the embedding-fidelity notice could not run (spawn failed)."
                Write-Output "  interpreter: $VcoVenvPython"
                Write-Output "  error:       $($_.Exception.Message)"
                Write-Output "  Until this is fixed, shrink/refusal fidelity loss is recorded but never reported."
            }
        } else {
            # The OTHER way this leg goes permanently quiet: no interpreter to
            # spawn at all. There used to be no such branch at all, which made
            # it indistinguishable from "nothing to report". Mirrors the .sh
            # sibling.
            Write-Output "[embedding-failures-surface] the embedding-fidelity notice could not run (no VCO venv resolved)."
            Write-Output "  Probed `$env:VCT_VENV, `$env:VCT_INSTALL_ROOT and the clone-relative fallback; none held a usable python."
            Write-Output "  Until this is fixed, shrink/refusal fidelity loss is recorded but never reported."
            Write-Output "  Remedy: start the session from the launcher (it exports VCT_INSTALL_ROOT), or point"
            Write-Output "  `$env:VCT_VENV at the orchestrator's venv."
        }
    }
}

if (-not (Test-Path $HintFile)) {
    # No outage recorded — the fidelity leg above already ran; silent exit
    # (idempotent zero-output path when nothing new was surfaced either).
    exit 0
}

# Name the log Claude should read. Prefer an EXISTING file, new home before
# the frozen archive, so a user whose modified hooks still append to ~/.claude
# — or one whose copy has not run yet — is pointed at the file that actually
# has their rows, not at a path that does not exist. Falls back to the write
# target when neither exists yet (the path is informational; existence is not
# required here).
$JsonlPath = ""
if ($MetricsLibLoaded) {
    $JsonlPath = Get-VcoMetricsReadFile -Name "embedding_failures.jsonl"
    if (-not $JsonlPath -and $script:VcoMetricsDir) {
        $JsonlPath = Join-Path $script:VcoMetricsDir "embedding_failures.jsonl"
    }
}
if (-not $JsonlPath) { $JsonlPath = "(metrics directory not resolvable)" }

# Surface to Claude via stdout — SessionStart hook stdout is injected as
# a system-reminder. Print path + full hint body so the LLM sees both the
# pointer and the diagnostic in-context.
Write-Output ""
Write-Output "==================================================================="
Write-Output "Embedding-backend failure recorded since last successful run."
Write-Output "Claude: read this hint and (if asked) investigate the JSONL log."
Write-Output ""
Write-Output "Hint file:    $HintFile"
Write-Output "Detail log:   $JsonlPath"
Write-Output "==================================================================="
Write-Output ""
try {
    Get-Content -LiteralPath $HintFile -Raw -ErrorAction Stop | Write-Output
} catch {
    Write-Output "(hint file became unreadable between check and read)"
}
Write-Output ""
Write-Output "==================================================================="
Write-Output ""

exit 0
