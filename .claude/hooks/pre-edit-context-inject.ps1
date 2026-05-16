# OS-EXEMPT-PARITY: Windows-only fix 2026-05-08 — added hookSpecificOutput/additionalContext JSON envelope. The .sh sibling already emitted that envelope from earlier work; no .sh change needed in this commit.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars (this hook doesn't need credentials)
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: read-side delegator (PR #171 / 0.1.7).
#   Delegates KG search to claude_mcp_servers/scripts/rl_kg_search.py and
#   code-graph search to .claude/scripts/code-graph-query — both call the
#   access-aware helpers (_kg_collections_to_search /
#   code_graph_collections_to_query) in claude_mcp_servers/weaviate_mcp/
#   server.py, which read VCT_KG_ACCESS_LIST + VCT_CODE_GRAPH_ACCESS_LIST.
#   This hook does NOT query Weaviate directly. Env propagation is by
#   subprocess inheritance (Start-Process / & inherit env by default).
#   See knowledge/concepts/multi-source-kg-runtime.md and
#   tests/test_kg_access_list.py for the consumer contract.

# pre-edit-context-inject.ps1
# Pre-edit context injection — KG + code graph context for the file being edited.
# Always exit 0 (never block the edit).

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# Source emit-context.ps1 ONLY if the file exists. If the helper is
# missing (partial install or just-after-clone before _lib/ is fully
# populated), the hook still runs its dedup/state work. The
# Emit-ContextJson wrapper tolerates a missing Emit-AdditionalContext
# function via Get-Command. We deliberately do NOT swallow source
# errors — a syntax error in an existing helper is a real bug.
if (Test-Path "$PSScriptRoot/_lib/emit-context.ps1") {
    . "$PSScriptRoot/_lib/emit-context.ps1"
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($args) and $env:CLAUDE_TOOL_NAME etc. are EMPTY —
# verified empirically 2026-05-08 via stdin-capture diagnostic.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
$ToolArgs = ""
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)   { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_input)  { $ToolArgs = ($payload.tool_input | ConvertTo-Json -Compress -Depth 8) }
        if ($payload.session_id)  { $SessionId = [string]$payload.session_id }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}

if ($ToolName -ne "Edit") { exit 0 }

$ScriptDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# session_id from stdin JSON is the canonical per-conversation key. Falls
# back to "default" only if the payload is malformed (which would mean the
# hook contract itself is broken — see ConvertFrom-Json above).
if (-not $SessionId) { $SessionId = "default" }
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$CacheBase = Join-Path $Tmp "claude_edit_cache_$SessionId"
$CacheTtl = 600

# State lives in the project directory (not /tmp/) so it survives reboots and
# is co-located with the session's other ephemeral state. Wiped by the
# PostCompact hook when the LLM's context is trimmed (so the dedup window
# matches the actual context window the LLM sees).
$SeenDir = Join-Path $ProjectRoot ".claude/state"
if (-not (Test-Path $SeenDir)) {
    New-Item -ItemType Directory -Path $SeenDir -Force -ErrorAction SilentlyContinue | Out-Null
}
$SeenNodesFile = Join-Path $SeenDir "seen_kg_titles_$SessionId.txt"

function Get-JsonField([string]$field) {
    if (-not $PY -or -not $ToolArgs) { return "" }
    try {
        $code = "import sys, json`ntry:`n    d = json.loads(sys.stdin.read())`n    print(d.get('$field', ''))`nexcept Exception:`n    print('')"
        $r = $ToolArgs | & $PY -c $code 2>$null
        if ($r) { return $r.Trim() }
    } catch { }
    return ""
}

$FilePath = Get-JsonField "file_path"
$NewString = Get-JsonField "new_string"
if (-not $FilePath) { exit 0 }

# Cache key from file path (md5 via .NET).
$md5 = [System.Security.Cryptography.MD5]::Create()
$bytes = [System.Text.Encoding]::UTF8.GetBytes($FilePath)
$hash = ($md5.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
$CacheDir = $CacheBase
$CacheFile = Join-Path $CacheDir $hash
if (-not (Test-Path $CacheDir)) {
    New-Item -ItemType Directory -Path $CacheDir -Force | Out-Null
}

# === Emit context as PreToolUse JSON envelope ===
# Wraps Emit-AdditionalContext from _lib/emit-context.ps1 — the helper
# gates on whitespace-only content so we don't surface empty
# system-reminder blocks when dedup suppresses every result. Pre-2026-05-08
# this hook printed plain stdout that never reached the LLM context on
# Windows; the .sh sibling was fixed in PR #168 and the .ps1 fix landed
# alongside the fork-readiness sweep for 0.1.7.
function Emit-ContextJson([string]$ctx) {
    # If the helper sourced (normal case), delegate. If it didn't (the
    # `_lib/emit-context.ps1` file was missing at hook startup), fall
    # back to a silent no-op rather than crashing on an undefined
    # function. The hook's other work (dedup state, cache write)
    # remains valid.
    if (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue) {
        Emit-AdditionalContext $ctx 'PreToolUse'
    }
}

# Check cache (10-min TTL) — uses .NET file mtime, no cross-OS stat issues.
# Cache stores RAW per-result blocks (with KG:/CODE: headers) so dedup can
# still apply on replay against the latest seen-list. Replay runs further
# down, after Filter-Seen is in scope.
$CacheHit = $false
$CacheBlob = ""
if (Test-Path $CacheFile) {
    $mtime = (Get-Item $CacheFile).LastWriteTime
    $age = ((Get-Date) - $mtime).TotalSeconds
    if ($age -lt $CacheTtl) {
        $CacheHit = $true
        try { $CacheBlob = (Get-Content $CacheFile -Raw -ErrorAction Stop) } catch { }
    }
}

# Auto-detect project for multi-codebase support — best-effort, optional.
$DetectScriptPs1 = Join-Path $ProjectRoot ".claude/scripts/detect-project.ps1"
$DetectedProject = ""
if (Test-Path $DetectScriptPs1) {
    try {
        $DetectedProject = (& pwsh -NoProfile -File $DetectScriptPs1 $FilePath $ProjectRoot 2>$null).Trim()
    } catch { }
}
$CodeGraphProjectArg = if ($DetectedProject) { @('--project', $DetectedProject) } else { @() }

$Basename = Split-Path $FilePath -Leaf
$ModuleName = [System.IO.Path]::GetFileNameWithoutExtension($Basename)
$NewSnippet = if ($NewString.Length -gt 200) { $NewString.Substring(0,200) } else { $NewString }
$Query = "$ModuleName $NewSnippet".Trim()

# Run searches sequentially (PowerShell parallel jobs add overhead worse than 5s budget).
$KgTmp = New-TemporaryFile
$CodeTmp = New-TemporaryFile

# Dual-layout venv resolution (PR-25 / v0.2.12). Modern installs put the
# venv at <repo_root>\.venv (top-level); pre-v0.2.x installs had it at
# <repo_root>\claude_mcp_servers\.venv. Hardcoding only the latter caused
# this hook to silently fall through to system python on modern installs,
# where weaviate-client isn't available, so KG search silently broke.
$VenvBase = if ($env:VCT_INSTALL_ROOT) { $env:VCT_INSTALL_ROOT } else { $ProjectRoot }
$VenvPy = $null
if ($env:VCT_VENV) {
    $cand = Join-Path $env:VCT_VENV "Scripts\python.exe"
    if (Test-Path $cand) { $VenvPy = $cand }
    if (-not $VenvPy) {
        $cand = Join-Path $env:VCT_VENV "bin/python"
        if (Test-Path $cand) { $VenvPy = $cand }
    }
    if (-not $VenvPy -and (Test-Path $env:VCT_VENV -PathType Leaf)) {
        $VenvPy = $env:VCT_VENV
    }
}
if (-not $VenvPy) {
    foreach ($cand in @(
        (Join-Path $VenvBase ".venv\Scripts\python.exe"),
        (Join-Path $VenvBase ".venv/bin/python"),
        (Join-Path $VenvBase "claude_mcp_servers\.venv\Scripts\python.exe"),
        (Join-Path $VenvBase "claude_mcp_servers/.venv/bin/python")
    )) {
        if (Test-Path $cand) { $VenvPy = $cand; break }
    }
}
# Final fallback: if no venv resolved, leave $VenvPy as $null — the
# (Test-Path $VenvPy) gate below skips the KG search subprocess and the
# hook still exits 0 without blocking the edit.
$RlScript = Join-Path $ProjectRoot "claude_mcp_servers/scripts/rl_kg_search.py"
if ($VenvPy -and (Test-Path $VenvPy) -and (Test-Path $RlScript)) {
    try {
        # --hook-format prepends "KG: " to each result header so dedup can match by title.
        & $VenvPy $RlScript $Query --limit 1 --hook-format 2>$null | Select-Object -First 40 | Set-Content -Path $KgTmp.FullName
    } catch { }
}

$IsCode = $false
if ($FilePath -match '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$') {
    $IsCode = $true
    $cgQueryPs1 = Join-Path $ProjectRoot ".claude/scripts/code-graph-query.ps1"
    $cgQuerySh = Join-Path $ProjectRoot ".claude/scripts/code-graph-query"
    try {
        # --hook-format emits "CODE: <full_name> | <collection> | distance=..." headers.
        if (Test-Path $cgQueryPs1) {
            $out = & pwsh -NoProfile -File $cgQueryPs1 search $Query @CodeGraphProjectArg --limit 2 --hook-format 2>$null |
                Where-Object { $_ -notlike "*$FilePath*" } |
                Select-Object -First 20
            $out | Set-Content -Path $CodeTmp.FullName
        } elseif ((Test-Path $cgQuerySh) -and (Get-Command bash -ErrorAction SilentlyContinue)) {
            $out = & bash $cgQuerySh search $Query @CodeGraphProjectArg --limit 2 --hook-format 2>$null |
                Where-Object { $_ -notlike "*$FilePath*" } |
                Select-Object -First 20
            $out | Set-Content -Path $CodeTmp.FullName
        }
    } catch { }
}

$KgResult = ""
$CodeResult = ""
try { $KgResult = (Get-Content -Path $KgTmp.FullName -Raw -ErrorAction Stop) } catch { }
if ($IsCode) {
    try { $CodeResult = (Get-Content -Path $CodeTmp.FullName -Raw -ErrorAction Stop) } catch { }
}
Remove-Item $KgTmp.FullName, $CodeTmp.FullName -Force -ErrorAction SilentlyContinue

# Dedup against this session's seen nodes.
#
# The KG/codegraph result blocks have the shape:
#
#   KG: <title> | <type> | score=<n.nn> | <body...>
#   <body line 1>
#   <body line 2>
#   ...
#
# We dedup by title (the part between "KG: " and the first " | "), and we
# suppress the ENTIRE block — title line plus its body — if the title has
# already been shown this session. The previous implementation extracted a
# "title" from every line including body content, which filled the seen-list
# with body fragments and let titles re-appear.
function Filter-Seen([string]$input) {
    if (-not $input) { return "" }
    $filtered = New-Object System.Text.StringBuilder
    if (-not (Test-Path $SeenNodesFile)) { New-Item -ItemType File -Path $SeenNodesFile -Force | Out-Null }
    $seen = @{}
    foreach ($l in Get-Content $SeenNodesFile -ErrorAction SilentlyContinue) { $seen[$l] = $true }

    $currentTitle = ""
    $currentBlock = New-Object System.Text.StringBuilder
    $currentSkip = $false

    $flushBlock = {
        param($title, $block, $skip, $filteredRef, $seenRef, $seenFile)
        if ($title -and -not $skip -and -not $seenRef.Value.ContainsKey($title)) {
            [void]$filteredRef.Value.Append($block)
            $seenRef.Value[$title] = $true
            Add-Content -Path $seenFile -Value $title -ErrorAction SilentlyContinue
        }
    }

    foreach ($line in $input -split "`n") {
        # Header line starts a new block. Format: "KG: <title> | ..." or
        # "CODE: <full_name> | ..." — the first field after the prefix and
        # before the next " | " is the dedup key.
        if ($line -match '^(KG|CODE):\s+(.+)$') {
            # Flush previous block.
            & $flushBlock $currentTitle $currentBlock.ToString() $currentSkip ([ref]$filtered) ([ref]$seen) $SeenNodesFile
            $rest = $Matches[2]
            # Defensive: strip any accidentally-doubled "KG: " / "CODE: " that
            # could slip in from a formatting transition (e.g. an old cache
            # written before the producers added their own prefix).
            if ($rest.StartsWith("KG: "))   { $rest = $rest.Substring(4) }
            if ($rest.StartsWith("CODE: ")) { $rest = $rest.Substring(6) }
            $currentTitle = ($rest -split ' \| ')[0]
            # Cap to 200 chars defensively (some code-graph entity names can be long)
            if ($currentTitle.Length -gt 200) { $currentTitle = $currentTitle.Substring(0, 200) }
            $currentBlock = New-Object System.Text.StringBuilder
            [void]$currentBlock.AppendLine($line)
            # If already seen, mark the block so we drop it AND its body lines.
            $currentSkip = $seen.ContainsKey($currentTitle)
        } elseif ($currentTitle) {
            # Body line for the current block — accumulate.
            [void]$currentBlock.AppendLine($line)
        } else {
            # Pre-amble or stray line not part of any block — pass through
            # only if it has non-whitespace content. Blank separators
            # between fully-deduped blocks would otherwise leak through
            # and surface an empty system-reminder block to the LLM.
            if ($line -match '\S') {
                [void]$filtered.AppendLine($line)
            }
        }
    }
    # Flush the final block.
    & $flushBlock $currentTitle $currentBlock.ToString() $currentSkip ([ref]$filtered) ([ref]$seen) $SeenNodesFile

    return $filtered.ToString()
}

# Cache replay: if we have a cache hit, dedup it against the current
# seen-list and emit. If everything in the cache is already seen, exit
# silently. The cache stores RAW per-result blocks (KG:/CODE: headers).
if ($CacheHit) {
    $filteredCache = Filter-Seen $CacheBlob
    $trimmed = ($filteredCache -replace '\s+', '')
    if (-not $trimmed) { exit 0 }
    $replayOut = [System.Text.StringBuilder]::new()
    [void]$replayOut.AppendLine("[Pre-edit context for ${Basename}]:")
    [void]$replayOut.AppendLine("")
    [void]$replayOut.Append($filteredCache)
    Emit-ContextJson $replayOut.ToString()
    exit 0
}

# Capture raw producer output (pre-dedup) for the cache. Caching post-dedup
# would perma-suppress titles eligible to re-appear after a /compact wipe.
$KgRaw   = $KgResult
$CodeRaw = $CodeResult

if ($KgResult)   { $KgResult   = Filter-Seen $KgResult }
if ($CodeResult) { $CodeResult = Filter-Seen $CodeResult }

$HasKg   = [bool]$KgResult
$HasCode = [bool]$CodeResult

# Build raw cache blob (used in both empty-output and emit branches).
$rawCache = ""
if ($KgRaw)   { $rawCache += $KgRaw + "`n" }
if ($CodeRaw) { $rawCache += $CodeRaw + "`n" }

if (-not $HasKg -and -not $HasCode) {
    if ($rawCache) {
        try { Set-Content -Path $CacheFile -Value $rawCache -Encoding UTF8 -NoNewline } catch { }
    }
    exit 0
}

# Per-result headers already carry "KG: " / "CODE: " prefixes from the
# producers (--hook-format). Don't add an extra block-level label.
$out = [System.Text.StringBuilder]::new()
[void]$out.AppendLine("[Pre-edit context for ${Basename}]:")
[void]$out.AppendLine("")
if ($HasKg) {
    [void]$out.Append($KgResult)
    [void]$out.AppendLine("")
}
if ($HasCode) {
    [void]$out.Append($CodeResult)
    [void]$out.AppendLine("")
}

# Cache RAW per-result blocks (pre-dedup) so replays apply current dedup
# state. Caching post-dedup would perma-suppress titles legitimately
# re-eligible after a /compact wipe.
if ($rawCache) {
    try { Set-Content -Path $CacheFile -Value $rawCache -Encoding UTF8 -NoNewline } catch { }
}
Emit-ContextJson $out.ToString()
exit 0
