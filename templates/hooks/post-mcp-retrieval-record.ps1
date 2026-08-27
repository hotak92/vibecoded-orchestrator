# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# post-mcp-retrieval-record.ps1 - P4 (v0.2.91)
# OS-PARITY: ports the .sh sibling. PostToolUse on the weaviate-kg RETRIEVAL
# tools. Records what an EXPLICIT retrieval already put in the model's context,
# into the SAME per-session stores the injecting hooks consult, so they stop
# re-injecting it.
#
# ONE HOME (CLAUDE.md "share, don't mirror, cross-language logic", option A):
# ALL parsing + key derivation lives in the SHARED
# templates/scripts/mcp_retrieval_record.py, which this hook and the .sh sibling
# both invoke. A PowerShell re-implementation would be a mirror of hash-and-parse
# logic - exactly the class that silently drifts and makes the two OSes suppress
# different things. This file is a thin argv/stdin shim, nothing more.
#
# The gap it closes + the "suppress ONLY what is provably already in context"
# safety rule are documented in that shared script's module docstring.
#
# Never blocks, never prints to stdout. Soft-fail: a parse failure records
# nothing.

foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }
$SessionIdLib = Join-Path $LibDir "session-id.ps1"
if (Test-Path $SessionIdLib) { . $SessionIdLib }
$SeenStoreLib = Join-Path $LibDir "seen-store.ps1"
if (Test-Path $SeenStoreLib) { . $SeenStoreLib }
$script:ProjectRoot = $ProjectRoot

$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# Untrustworthy session id ("" / "default") -> EMPTY store paths -> record
# nothing, exactly like the injectors' inject-blind policy. Never compose a
# shared bucket that could bleed one chat's suppression into another.
$SessionIdRaw = ""
if (Get-Command Get-VcoHookSessionId -ErrorAction SilentlyContinue) {
    $SessionIdRaw = Get-VcoHookSessionId -Stdin $HookStdin
}
if (-not $SessionIdRaw -or $SessionIdRaw -eq "default") { exit 0 }
if (-not (Get-Command Get-VcoSeenStorePath -ErrorAction SilentlyContinue)) { exit 0 }

$InjectFile = Get-VcoSeenStorePath -Kind "inject" -SessionId $SessionIdRaw -ProjectRoot $ProjectRoot
$ReadsFile  = Get-VcoSeenStorePath -Kind "reads"  -SessionId $SessionIdRaw -ProjectRoot $ProjectRoot
if (-not $InjectFile) { exit 0 }
$StateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
if (-not (Test-Path -LiteralPath $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
}

# Resolve the SHARED recorder. Installed layout first, orchestrator-clone
# layout second; absent (partial install) -> silent no-op.
$Recorder = Join-Path (Join-Path (Join-Path $ProjectRoot ".claude") "scripts") "mcp_retrieval_record.py"
if (-not (Test-Path -LiteralPath $Recorder)) {
    $Alt = Join-Path (Join-Path (Split-Path -Parent $ScriptDir) "scripts") "mcp_retrieval_record.py"
    if (Test-Path -LiteralPath $Alt) { $Recorder = $Alt } else { exit 0 }
}

try {
    $HookStdin | & $PY $Recorder $InjectFile $ReadsFile 2>$null | Out-Null
} catch { }

exit 0
