# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# HK-3 (v0.2.73): named-script extraction of the former INLINE knowledge-sync
# hook that lived in settings.json.windows.template as a raw
# `powershell -Command "try { ... } catch { }"` block.
#
# MUST MATCH kg-sync-on-edit.sh — same guard + scrub + accurate-error +
# delete-redundant-inline-sync contract (D-5/D-6 acceptance):
#   (a) GUARD  — honours $env:VCT_DISABLE_HOOKS (the inline block's bash-ism
#                guard was inert under cmd.exe — see D-4).
#   (b) SCRUB  — scrubs the canonical secret-env list before any subprocess.
#   (c) ERRORS — routes through the venv-resolving kg-sync wrapper (one home)
#                and writes ONE diagnostic line on real failure instead of
#                the inline block's silent `catch { }`.
#   (d) DELETE REDUNDANT — post-file-edit.ps1 already debounce-syncs
#                knowledge/**/*.md; the inline registration was a redundant
#                un-debounced second write, now removed from the template.

$PSScriptRootLocal = $PSScriptRoot
$LibDir = Join-Path $PSScriptRootLocal "_lib"

# (b) Scrub sensitive env BEFORE spawning any subprocess.
$ScrubLib = Join-Path $LibDir "scrub-env.ps1"
if (Test-Path $ScrubLib) {
    . $ScrubLib
    Invoke-VctScrubSecretEnv
} else {
    foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
        if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    }
}

# (a) Guard.
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# Read the untrusted JSON payload from stdin.
$Input = ""
try {
    $Input = [Console]::In.ReadToEnd()
} catch {
    exit 0
}
if (-not $Input) { exit 0 }

# Extract the edited file path from the payload. Parse in PowerShell (robust
# to path metacharacters — never interpolated into a command string).
$FilePath = $null
try {
    $payload = $Input | ConvertFrom-Json
    $ti = $payload.tool_input
    if ($ti) {
        foreach ($k in 'file_path','path','filePath') {
            if ($ti.PSObject.Properties[$k] -and $ti.$k) { $FilePath = $ti.$k; break }
        }
    }
} catch {
    exit 0
}
if (-not $FilePath) { exit 0 }

# Only sync knowledge/**/*.md — dev-collection + code-graph paths are
# handled by post-file-edit.ps1.
$norm = $FilePath -replace '\\', '/'
if ($norm -notmatch '(^|/)knowledge/.*\.md$') { exit 0 }

# Route through the venv-resolving kg-sync wrapper (the ONE home for the
# weaviate/yaml import-resolution).
$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$KgSync = Join-Path $ProjectDir ".claude/scripts/kg-sync.ps1"
if (Test-Path $KgSync) {
    . (Join-Path $LibDir "resolve-powershell.ps1") 2>$null
    try {
        if ($PsExe) {
            & $PsExe -NoProfile -File $KgSync $FilePath *> $null
        } else {
            & powershell -NoProfile -File $KgSync $FilePath *> $null
        }
        if ($LASTEXITCODE -ne 0) {
            [Console]::Error.WriteLine("[kg-sync-on-edit] kg-sync failed for $FilePath (KG may be stale)")
        }
    } catch {
        [Console]::Error.WriteLine("[kg-sync-on-edit] kg-sync failed for $FilePath (KG may be stale)")
    }
}

exit 0
