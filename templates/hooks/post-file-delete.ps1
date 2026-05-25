# Parity-confirmation: full body parity with post-file-delete.sh.
#   - Sensitive env-var scrub (foreach below) mirrors .sh `unset ...`.
#   - VCT_DISABLE_HOOKS short-circuit mirrors .sh line 23.
#   - Stdin JSON parse + tool_input.command extraction mirrors .sh
#     Python one-liner.
#   - Quick-reject on path + verb mirrors .sh case statements.
#   - Shlex-style command parse → path enumeration mirrors .sh Python
#     parser block (we use the same Python parser for consistency).
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

# Reuse the .sh hook's Python parser by piping the command into it.
# Keeps a single source of truth for the parsing rules; only the
# subprocess invocation differs.
$ParserScript = @'
import json, shlex, sys, glob, os
cmd = sys.stdin.read().strip()
try:
    tokens = shlex.split(cmd, posix=True)
except ValueError:
    sys.exit(0)
if not tokens:
    sys.exit(0)
i = 0
while i < len(tokens) and '=' in tokens[i] and not tokens[i].startswith('-'):
    eq = tokens[i].index('=')
    head = tokens[i][:eq]
    if head and head.replace('_','').isalnum() and head[0].isalpha():
        i += 1
        continue
    break
if i >= len(tokens):
    sys.exit(0)
verb = os.path.basename(tokens[i]).lower()
if verb not in ('rm', 'unlink', 'mv', 'remove-item', 'move-item'):
    sys.exit(0)
args = []
for tok in tokens[i+1:]:
    if tok.startswith('-'):
        continue
    if tok in (';', '&&', '||', '|'):
        break
    args.append(tok)
if verb in ('mv', 'move-item') and len(args) >= 2:
    args = args[:1]
expanded = []
for a in args:
    if any(c in a for c in '*?['):
        expanded.extend(glob.glob(a))
    else:
        expanded.append(a)
for p in expanded:
    if not p:
        continue
    if not p.endswith('.mmd') and not p.endswith('.excalidraw'):
        continue
    norm = os.path.normpath(p)
    if '.claude' + os.sep + 'diagrams' + os.sep not in norm and '.claude/diagrams/' not in norm:
        continue
    print(norm)
'@

$Paths = $Command | & $Py -c $ParserScript 2>$null
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
