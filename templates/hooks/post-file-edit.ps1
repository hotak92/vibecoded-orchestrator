# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# post-file-edit.ps1
# Mirror of post-file-edit.sh. Auto-syncs knowledge/, docs/, and code files.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

param(
    [Parameter(Position=0)] [string]$EditedFile = ""
)

$ScriptDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$KnowledgeRoot = Join-Path $ProjectRoot "knowledge"
$DocsDir = Join-Path $ProjectRoot "docs"

if (-not $EditedFile) { exit 0 }

# 1. Knowledge graph auto-sync.
if ($EditedFile.StartsWith($KnowledgeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Output "Knowledge file edited: $EditedFile"
    Write-Output "   Syncing to Weaviate (inference happens during sync)..."

    $relPath = $EditedFile
    if ($EditedFile.StartsWith($ProjectRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $relPath = $EditedFile.Substring($ProjectRoot.Length).TrimStart('\','/')
    }

    $kgSyncPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-sync.ps1"
    $kgSyncSh = Join-Path $ProjectRoot ".claude/scripts/kg-sync"
    if (Test-Path $kgSyncPs1) {
        Start-Process -FilePath "pwsh" -ArgumentList @('-NoProfile','-File',$kgSyncPs1,$relPath) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
    } elseif ((Test-Path $kgSyncSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
        Start-Process -FilePath "bash" -ArgumentList @($kgSyncSh, $relPath) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
    }
    Write-Output "Background sync started for knowledge graph"

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
        Write-Output "Running duplicate detection (every 10 edits)..."
        $dupPs1 = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates.ps1"
        $dupSh = Join-Path $ProjectRoot ".claude/scripts/kg-duplicates"
        # Note (audit fix 2026-05-07): the .ps1 sibling already discards
        # output via `Start-Process -WindowStyle Hidden | Out-Null`, so the
        # bash-side bug (unbounded grep buffer) does not exist here.
        # Parity-touch only — no behavioural change.
        if (Test-Path $dupPs1) {
            Start-Process -FilePath "pwsh" -ArgumentList @('-NoProfile','-File',$dupPs1,'--threshold','0.95') -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        } elseif ((Test-Path $dupSh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath "bash" -ArgumentList @($dupSh, '--threshold', '0.95') -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
        }
    }
}

# 2. Docs auto-sync.
if ($EditedFile.StartsWith($DocsDir, [StringComparison]::OrdinalIgnoreCase) -and ($EditedFile -like "*.md")) {
    Write-Output "Documentation edited: $EditedFile"
    Write-Output "   Syncing to Weaviate development collection..."
    $venvPy = Join-Path (if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }) "claude_mcp_servers\.venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        $venvPy = Join-Path (if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }) "claude_mcp_servers/.venv/bin/python"
    }
    $uploadScript = Join-Path $ProjectRoot ".claude/scripts/upload_docs.py"
    if ((Test-Path $venvPy) -and (Test-Path $uploadScript)) {
        Start-Process -FilePath $venvPy -ArgumentList @($uploadScript, $EditedFile) -WorkingDirectory $ProjectRoot -WindowStyle Hidden | Out-Null
    }
    Write-Output "Background sync started for development docs"
}

# 3. Code file changes: code graph incremental update.
if ($EditedFile -match '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$') {
    $bn = Split-Path $EditedFile -Leaf
    Write-Output "Code file edited: $bn"
    $cgIncPs1 = Join-Path $ScriptDir "code-graph-incremental.ps1"
    if (Test-Path $cgIncPs1) {
        & pwsh -NoProfile -File $cgIncPs1 $EditedFile $ProjectRoot "ClaudeOrchestrator"
    }
    Write-Output "Reminder: when done coding, update CONTEXT_STATE.md + add KG node if new patterns emerged."
}

# 4. CONTEXT_STATE.md update reminder.
if ($EditedFile.EndsWith("CONTEXT_STATE.md", [StringComparison]::OrdinalIgnoreCase)) {
    $expertSkill = Join-Path $ProjectRoot ".claude/skills/project-experts/claude-orchestrator-expert.md"
    if (Test-Path $expertSkill) {
        try {
            $changes = (Select-String -Path $EditedFile -Pattern '(✅|##\s+(Status|Current Work|Next Steps|Knowledge Captured))' -ErrorAction SilentlyContinue | Measure-Object).Count
            if ($changes -gt 5) {
                Write-Output ""
                Write-Output "Significant changes to CONTEXT_STATE.md detected ($changes markers)"
                Write-Output "   Consider updating: .claude/skills/project-experts/claude-orchestrator-expert.md"
                Write-Output ""
            }
        } catch { }
    }
}

# 5. Workflow system change reminder.
$workflowChanged = $false
$skillsDir = Join-Path $ProjectRoot ".claude/skills"
$hooksDir = Join-Path $ProjectRoot ".claude/hooks"
if ($EditedFile.StartsWith($skillsDir, [StringComparison]::OrdinalIgnoreCase) -or
    $EditedFile.StartsWith($hooksDir,  [StringComparison]::OrdinalIgnoreCase)) {
    $workflowChanged = $true
}
if ($workflowChanged) {
    Write-Output ""
    Write-Output "Workflow system file edited: $(Split-Path $EditedFile -Leaf)"
    Write-Output "   Consider:"
    Write-Output "   - Test the change in actual usage"
    Write-Output "   - Update documentation if structure changed"
    Write-Output "   - Run /workflow-optimizer to check for optimizations"
    Write-Output "   - Update skills-setup-guide.md if setup process changed"
    Write-Output ""
}
exit 0
