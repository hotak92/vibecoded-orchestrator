# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: write-side delegator (PR #171 / 0.1.7).
#   Calls .claude/scripts/kg-sync (writes to the project's own
#   KG_COLLECTION / DEVELOPMENT_COLLECTION) and code-graph-incremental.ps1
#   (writes to the project's own code-graph collections via
#   analyze_code_graph.py). Writes do NOT consult VCT_KG_ACCESS_LIST or
#   VCT_CODE_GRAPH_ACCESS_LIST — those env vars are read-side only
#   (fan-out search across peer KGs). This hook is correct as-is; no
#   centralization needed. See knowledge/concepts/multi-source-kg-runtime.md.

# post-file-edit.ps1 — PostToolUse hook
#
# Side-effects (background): KG / docs sync, code-graph incremental.
# LLM-visible reminders (additionalContext envelope):
#   - Code-file edits → CONTEXT_STATE / KG capture reminder
#   - CONTEXT_STATE.md significant-changes → expert-skill update prompt
#   - .claude/skills or .claude/hooks edits → workflow-test prompt
#
# Plain stdout from PostToolUse hooks is silently dropped per the
# v2.1.x contract — reminders intended for the model MUST go through
# `Emit-AdditionalContext` from `_lib/emit-context.ps1`.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
$EmitContextLib = Join-Path $PSScriptRoot "_lib/emit-context.ps1"
if (Test-Path $EmitContextLib) { . $EmitContextLib }

# Accumulate LLM-visible reminders, emit one envelope at the end.
$LlmNudge = ""
function Add-Nudge([string]$msg) {
    if ($script:LlmNudge) {
        $script:LlmNudge = "$script:LlmNudge`n`n$msg"
    } else {
        $script:LlmNudge = $msg
    }
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
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
$KnowledgeRoot = Join-Path $ProjectRoot "knowledge"
$DocsDir = Join-Path $ProjectRoot "docs"

if (-not $EditedFile) { exit 0 }

# v0.2.49 Phase 8 (item #22): access-matrix gate for KG writes.
#
# Before kicking off any kg-sync subprocess (or upload_docs.py), check
# if this project has write access to the target Weaviate collection.
# The check is fail-open: if the hub is unreachable / the project
# isn't registered / the response is malformed, the resolver returns
# "write" + emits a WARNING + logs a dropped-write-metric row, then
# the sync proceeds. This is DELIBERATE (closed-circuit would brick
# all KG writes during launcher restart).
#
# When the gate returns "read" or "none", we SKIP the sync silently +
# the user gets the WARNING from the resolver client about the deny.
#
# Mirrors templates/hooks/post-file-edit.sh's _kg_write_allowed shell
# function. Resolver discovery: templates/scripts/vct_access_check.ps1
# (orchestrator-root) → .claude/scripts/vct_access_check.ps1
# (user-project install). Resolver script lives at
# templates/scripts/vct_access_check.ps1 and is byte-equivalent to the
# bash sibling (shipped together by bundle install).
function Test-KgWriteAllowed {
    param(
        [string]$Project,
        [string]$Collection
    )
    if (-not $Project) { return $true }    # no project context → allow (legacy path)
    if (-not $Collection) { return $true } # no collection context → allow

    $resolver = $null
    $candidates = @(
        (Join-Path $ProjectRoot "templates/scripts/vct_access_check.ps1"),
        (Join-Path $ProjectRoot ".claude/scripts/vct_access_check.ps1")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $resolver = $c; break }
    }
    if (-not $resolver) {
        # Resolver not on disk → allow (pre-v0.2.49 install, or post-
        # update where the script hasn't been bundled yet). Matches the
        # bash sibling's same fallthrough.
        return $true
    }

    try {
        $level = & pwsh -NoProfile -File $resolver $Project $Collection 2>$null
        if ($null -eq $level) { return $true }  # fail-open on null
        $level = ([string]$level).Trim()
    } catch {
        return $true  # fail-open on any invocation error
    }
    return ($level -eq 'write')
}

# Resolve project_id once for the access checks below. Same env-then-
# grep-.claude/env fallback as the bash sibling.
$VctProjectId = $Env:VCT_PROJECT_ID
if (-not $VctProjectId) {
    $envFile = Join-Path $ProjectRoot ".claude/env"
    if (Test-Path $envFile) {
        try {
            $envLines = Get-Content -LiteralPath $envFile -ErrorAction Stop
            foreach ($line in $envLines) {
                if ($line -match '^\s*VCT_PROJECT_ID\s*=\s*"?([^"]+)"?\s*$') {
                    $VctProjectId = $Matches[1].Trim()
                    break
                }
            }
        } catch { }
    }
}

# 1. Knowledge graph auto-sync (background side-effect).
if ($EditedFile.StartsWith($KnowledgeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    $relPath = $EditedFile
    if ($EditedFile.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $relPath = $EditedFile.Substring($ProjectRoot.Length).TrimStart('\','/')
    }

    # v0.2.49 Phase 8: gate the sync on access-matrix write permission.
    # KG_COLLECTION is the target Weaviate class for primary-KG writes.
    if (Test-KgWriteAllowed -Project $VctProjectId -Collection $Env:KG_COLLECTION) {
        $kgSyncPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-sync.ps1"
        $kgSyncSh = Join-Path $ProjectRoot ".claude/scripts/kg-sync"
        if (Test-Path $kgSyncPs1) {
            Start-Process -FilePath "pwsh" -ArgumentList @('-NoProfile','-File',$kgSyncPs1,$relPath) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        } elseif ((Test-Path $kgSyncSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath "bash" -ArgumentList @($kgSyncSh, $relPath) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        }
    }

    # Duplicate detection every 10 edits.
    $editCountFile = Join-Path $ProjectRoot ".claude/logs/.kg_edit_count"
    $logsDir = Split-Path $editCountFile -Parent
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }
    $count = 0
    if (Test-Path $editCountFile) {
        try { $count = [int](Get-Content $editCountFile -Raw -ErrorAction Stop).Trim() } catch { $count = 0 }
    }
    $count++
    Set-Content -Path $editCountFile -Value $count -Encoding ascii

    if (($count % 10) -eq 0) {
        $dupPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates.ps1"
        $dupSh = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates"
        if (Test-Path $dupPs1) {
            Start-Process -FilePath "pwsh" -ArgumentList @('-NoProfile','-File',$dupPs1,'--threshold','0.95') -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        } elseif ((Test-Path $dupSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath "bash" -ArgumentList @($dupSh, '--threshold', '0.95') -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        }
    }
}

# 2. Docs auto-sync (background side-effect).
# v0.2.46 post-adversarial: dot-source the shared resolver instead of
# the inline VCT_INSTALL_ROOT-or-ProjectRoot fallback (the latter pointed
# at the USER's venv which doesn't have vco_lib + weaviate-client).
. (Join-Path $ScriptDir "_lib/resolve-vco-venv.ps1")
if ($EditedFile.StartsWith($DocsDir, [StringComparison]::OrdinalIgnoreCase) -and ($EditedFile -like "*.md")) {
    # v0.2.49 Phase 8: gate docs sync on access-matrix write permission
    # against DEVELOPMENT_COLLECTION (the docs/ target).
    if (Test-KgWriteAllowed -Project $VctProjectId -Collection $Env:DEVELOPMENT_COLLECTION) {
        $venvPy = Resolve-VcoVenvPython -ScriptDir $ScriptDir
        $uploadScript = Join-Path $ProjectRoot ".claude/scripts/upload_docs.py"
        if ($venvPy -and (Test-Path $uploadScript)) {
            Start-Process -FilePath $venvPy -ArgumentList @($uploadScript, $EditedFile) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        }
    }
}

# 2b. Auto-index diagrams (Phase 1.5 — Mermaid + Excalidraw). Mirror of
# the .sh sibling's same-numbered branch. 60s per-file throttle, skips
# sidecar .meta.json writes (would infinite-loop), notifies vct-hub for
# UI refresh (404 silently swallowed until Phase 1.2 broadcast route).
$DiagramsDir = Join-Path $ProjectRoot ".claude/diagrams"
if ($EditedFile.StartsWith($DiagramsDir, [StringComparison]::OrdinalIgnoreCase) `
    -and ($EditedFile -notlike "*.meta.json") `
    -and (($EditedFile -like "*.mmd") -or ($EditedFile -like "*.excalidraw"))) {

    $throttleDir = Join-Path $ProjectRoot ".claude/state"
    if (-not (Test-Path $throttleDir)) {
        New-Item -ItemType Directory -Path $throttleDir -Force | Out-Null
    }

    # MD5 hash of file path → throttle key (no slashes).
    $md5 = [System.Security.Cryptography.MD5]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($EditedFile)
        $digest = $md5.ComputeHash($bytes)
        $diagramHash = -join ($digest | ForEach-Object { $_.ToString('x2') })
    } finally {
        $md5.Dispose()
    }
    $throttleFile = Join-Path $throttleDir "diagram_idx_${diagramHash}.ts"

    $nowTs = [int][double]::Parse(((Get-Date) - (Get-Date "1970-01-01Z")).TotalSeconds)
    $lastTs = 0
    if (Test-Path $throttleFile) {
        try {
            $raw = (Get-Content $throttleFile -Raw -ErrorAction Stop).Trim()
            if ($raw -match '^[0-9]+$') { $lastTs = [int]$raw }
        } catch { $lastTs = 0 }
    }

    if (($nowTs - $lastTs) -ge 60) {
        Set-Content -Path $throttleFile -Value $nowTs -Encoding ascii -ErrorAction SilentlyContinue

        # v0.2.46 post-adversarial: shared resolver (already dot-sourced
        # above at the docs branch). Falls back to system python ONLY when
        # no VCO venv is resolvable — never to the USER's project venv.
        $diagVenv = Resolve-VcoVenvPython -ScriptDir $ScriptDir
        if (-not $diagVenv) {
            $diagVenv = (Get-Command python -ErrorAction SilentlyContinue).Source
        }

        if ($diagVenv) {
            # Build the indexer arguments. Pass --diagrams-collection
            # when DIAGRAMS_COLLECTION is set in the env (fix/a1-indexing-
            # pipeline 2026-05-25). Without this kwarg the indexer's
            # Weaviate upsert silently skips (Bug-1 of the wiring audit).
            # Older projects without DIAGRAMS_COLLECTION in env keep the
            # legacy sidecar-only behaviour automatically.
            $diagArgs = @('-m', 'vco_lib.diagram_indexer', 'index', $EditedFile)
            if ($env:DIAGRAMS_COLLECTION) {
                $diagArgs += @('--diagrams-collection', $env:DIAGRAMS_COLLECTION)
            }

            # Serialized index + snapshot in a single background job.
            # R2 (code review 2026-05-25): previously two separate
            # Start-Process calls ran in parallel; the snapshot CLI's
            # `project_diagrams WHERE file_path=?` query returned no
            # row on first-edit-per-file because the indexer UPSERT
            # hadn't committed yet → first-version snapshot lost
            # forever. Use Start-Job to run the two CLIs sequentially
            # (index → snapshot) without blocking the hook itself.
            $snapArgs = @(
                '-m', 'vco_lib.diagram_indexer',
                'snapshot', 'create', $EditedFile, '--quiet'
            )
            Start-Job -ScriptBlock {
                param($vp, $iargs, $sargs, $cwd)
                Set-Location $cwd
                & $vp @iargs *> $null
                & $vp @sargs *> $null
            } -ArgumentList $diagVenv, $diagArgs, $snapArgs, $ProjectRoot | Out-Null
        }

        # Live UI refresh in DiagramsTab is driven by the launcher's
        # frontend file-watcher (chokidar) — NOT a hub broadcast. The
        # original Phase 1.5.A design called for /api/v1/notify/diagram-changed
        # but pub/sub from hub → frontend would need SSE/WebSocket
        # plumbing the launcher does not have today. Re-evaluate if
        # multi-machine notification ever becomes a real requirement.
    }
}

# 3. Code file changes: code graph incremental update + LLM nudge.
# v0.2.21 Step 18 (caller migration): resolve the code-graph collection
# prefix via the launcher's vct-hub first (`vct_project_config.ps1 -Field
# code_graph_collection_prefix`); fall back to the legacy env chain when
# the hub is unreachable. Mirrors the .sh sibling at the same line.
#
# v0.2.23 field switch: previously this read `code_graph_project`, which
# the hub returns as a legacy alias for `project_slug` — NOT the canonical
# Weaviate prefix. The analyzer's `_sanitize_collection_prefix` then
# re-canonicalised the slug, producing a prefix that diverged from the
# launcher's `project_codegraph_bindings.collection_prefix`. Incremental
# writes landed in zombie collections (e.g. `Orchestrator_root_Code*`)
# while consumers queried the canonical prefix and saw 0 results.
# `code_graph_collection_prefix` is the binding-row truth and the only
# correct source for the write target.
#
# Pre-v0.2.11 behaviour hardcoded "ClaudeOrchestrator" here, which
# polluted the legacy collection from every project install. Do NOT
# re-introduce a hardcoded literal in this position.
if ($EditedFile -match '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$') {
    $bn = Split-Path $EditedFile -Leaf
    $cgIncPs1 = Join-Path $ScriptDir "code-graph-incremental.ps1"
    if (Test-Path $cgIncPs1) {
        $codeGraphPrefix = ""
        $resolverPs1 = Join-Path $ProjectRoot ".claude/scripts/vct_project_config.ps1"
        if (Test-Path $resolverPs1) {
            try {
                $codeGraphPrefix = (& pwsh -NoProfile -File $resolverPs1 `
                    -Project $ProjectRoot -Field code_graph_collection_prefix 2>$null) -as [string]
                if ($null -eq $codeGraphPrefix) { $codeGraphPrefix = "" }
                $codeGraphPrefix = $codeGraphPrefix.Trim()
            } catch { $codeGraphPrefix = "" }
        }
        if (-not $codeGraphPrefix) {
            $codeGraphPrefix = if ($env:CODE_GRAPH_PROJECT) { $env:CODE_GRAPH_PROJECT } `
                elseif ($env:PROJECT_NAME) { $env:PROJECT_NAME } `
                else { Split-Path $ProjectRoot -Leaf }
        }
        & pwsh -NoProfile -File $cgIncPs1 $EditedFile $ProjectRoot $codeGraphPrefix
    }
    Add-Nudge "[Code edit reminder] $bn was just edited.`nWhen you're done with this work item:`n- Update CONTEXT_STATE.md with what changed and what's next.`n- Capture any non-obvious learnings as a KG node under knowledge/concepts/."
}

# 4. CONTEXT_STATE.md significant-changes → expert-skill nudge.
if ($EditedFile.EndsWith("CONTEXT_STATE.md", [StringComparison]::OrdinalIgnoreCase)) {
    $expertSkill = Join-Path $ProjectRoot ".claude/skills/project-experts/claude-orchestrator-expert.md"
    if (Test-Path $expertSkill) {
        try {
            $changes = (Select-String -Path $EditedFile -Pattern '(✅|##\s+(Status|Current Work|Next Steps|Knowledge Captured))' -ErrorAction SilentlyContinue | Measure-Object).Count
            if ($changes -gt 5) {
                Add-Nudge "[CONTEXT_STATE.md updated — expert-skill review] $changes significant markers detected.`nConsider updating .claude/skills/project-experts/claude-orchestrator-expert.md if any of:`n  - Major milestone completed (Skills system, knowledge graph, etc.)`n  - Architecture changed (MCP, agents, workflow)`n  - New scripts/commands added (kg-*, wrappers)`n  - Recent work section needs refresh"
            }
        } catch { }
    }
}

# 5. Workflow-system edits → workflow-test nudge.
$workflowChanged = $false
$skillsDir = Join-Path $ProjectRoot ".claude/skills"
$hooksDir = Join-Path $ProjectRoot ".claude/hooks"
if ($EditedFile.StartsWith($skillsDir, [StringComparison]::OrdinalIgnoreCase) -or
    $EditedFile.StartsWith($hooksDir,  [StringComparison]::OrdinalIgnoreCase)) {
    $workflowChanged = $true
}
if ($workflowChanged) {
    $bn = Split-Path $EditedFile -Leaf
    Add-Nudge "[Workflow file edited] $bn was changed.`nConsider:`n  - Test the change in actual usage before assuming it works.`n  - Update documentation if the structure changed.`n  - Run /workflow-optimizer to check for optimizations.`n  - Update skills-setup-guide.md if the setup process changed."
}

# Emit accumulated nudges as a single PostToolUse envelope.
if ($LlmNudge -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
    Emit-AdditionalContext $LlmNudge PostToolUse
}
exit 0
