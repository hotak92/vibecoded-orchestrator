# Parity-confirmation: full body parity with post-file-delete.sh.
#   - Sensitive env-var scrub (foreach below) mirrors .sh `unset ...`.
#   - VCT_DISABLE_HOOKS short-circuit mirrors .sh line 23.
#   - Stdin JSON parse + tool_input.command extraction mirrors .sh
#     Python one-liner.
#   - Quick-reject on path + verb mirrors .sh case statements.
#   - Command parse → path enumeration delegates to the
#     `vco_lib.diagram_delete_parser` module (same module the .sh hook
#     uses; single source of truth for the chain-walk + verb-peeling
#     rules). See module docstring for the B4 regression note.
#   - `vco_lib.diagram_indexer drop` cascade mirrors .sh tail loop.
#
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# post-file-delete.ps1
# PostToolUse(Bash) hook — detects deletes of .mmd / .excalidraw files
# under .claude/diagrams/ and cascades the delete across SQLite +
# sidecar + Weaviate via `vco_lib.diagram_indexer drop <file>`.
#
# Retroactive cleanup of orphans (false negatives): use
# `vco rebuild-diagram-index --prune`. This replaced the
# previously-planned `cleanup-orphan-diagrams.sh` SessionStart hook.
#
# Always exits 0. Silent when no diagram delete is detected.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }

$Command = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload -and $payload.tool_input -and $payload.tool_input.command) {
        $Command = [string]$payload.tool_input.command
    }
} catch { }

if (-not $Command) { exit 0 }

# Quick reject: command must mention .claude/diagrams or .mmd/.excalidraw
# AND a delete-flavoured verb. Saves us from spawning the parser on
# every Bash invocation.
if (-not (
    $Command -match '\.claude[/\\]diagrams' -or
    $Command -match '\.mmd' -or
    $Command -match '\.excalidraw'
)) { exit 0 }
if (-not (
    $Command -match '(^|\s)rm\s' -or
    $Command -match '(^|\s)unlink\s' -or
    $Command -match '(^|\s)mv\s' -or
    $Command -match '(^|\s)Remove-Item\s' -or
    $Command -match '(^|\s)Move-Item\s'
)) { exit 0 }

# Resolve a Python interpreter (mirrors .sh _lib/find-python.sh).
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $Py) { $Py = (Get-Command py -ErrorAction SilentlyContinue).Source }
if (-not $Py) { exit 0 }

$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

# Delegate to the vco_lib.diagram_delete_parser module — single source
# of truth for the chain-walk + verb-peeling rules (same module the .sh
# hook uses). Unit-tested via tests/test_post_file_delete_parser.py.
$env:PYTHONPATH = "$ProjectRoot$(if ($env:PYTHONPATH) { [System.IO.Path]::PathSeparator + $env:PYTHONPATH })"
$Paths = $Command | & $Py -m vco_lib.diagram_delete_parser 2>$null
if (-not $Paths) { exit 0 }

foreach ($path in ($Paths -split "`n")) {
    $p = $path.Trim()
    if (-not $p) { continue }
    if (-not [System.IO.Path]::IsPathRooted($p)) {
        $p = Join-Path $ProjectRoot $p
    }
    $env:PYTHONPATH = "$ProjectRoot$(if ($env:PYTHONPATH) { [System.IO.Path]::PathSeparator + $env:PYTHONPATH })"
    & $Py -m vco_lib.diagram_indexer drop $p *> $null
}

exit 0
