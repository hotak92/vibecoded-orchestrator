# _lib/codegraph-query.ps1
# Shared code-graph retrieval helper dot-sourced by the .ps1 context hooks. The
# PowerShell sibling of _lib/codegraph-query.sh.
#
# Why this exists (one concern, one home -- CLAUDE.md "search before add")
# -----------------------------------------------------------------------
# v0.2.70 Stream C. Code-graph injection fires on four surfaces (code-file
# Edit/Read, selective Bash, code-edit resync, symbol Grep). This is the one home
# for the query+format logic so the surfaces don't each carry a copy. Callers own
# dedup (via _lib/seen-store.ps1) -- this helper returns RAW blocks.
#
# MUST MATCH: templates/hooks/_lib/codegraph-query.sh -- the query/format
# contract (CODE:-prefixed --hook-format output, the inner timeout bound) AND the
# Test-VcoCodegraphSymbolGate regex (same 4 shapes + code-file extension list).
#
# Plain ASCII only. Dot-sourced, never executed. Library, not a hook.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoCodegraphQuerySourced) { return }
$script:VcoCodegraphQuerySourced = $true

# Get-VcoCodegraphCli: locate the code-graph-query CLI; "" if absent. Prefers
# the PowerShell wrapper (.ps1) then the bash wrapper.
function Get-VcoCodegraphCli {
    $root = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } elseif ($script:ProjectRoot) { $script:ProjectRoot } else { "" }
    if (-not $root) { return "" }
    $ps1 = Join-Path (Join-Path (Join-Path $root ".claude") "scripts") "code-graph-query.ps1"
    if (Test-Path -LiteralPath $ps1) { return $ps1 }
    $sh = Join-Path (Join-Path (Join-Path $root ".claude") "scripts") "code-graph-query"
    if (Test-Path -LiteralPath $sh) { return $sh }
    return ""
}

# Invoke-VcoCodegraphQueryBlock <Query> <ProjectArg> <Limit> <ExcludePath>
# Return the raw "CODE:"-prefixed --hook-format block(s), or "". Soft-fail to ""
# on absent CLI / error / empty. Bounded by a 4s job timeout so a hung child
# can't hang the hook.
function Invoke-VcoCodegraphQueryBlock {
    param(
        [string]$Query,
        [string]$ProjectArg = "",
        [int]$Limit = 2,
        [string]$ExcludePath = ""
    )
    if ([string]::IsNullOrEmpty($Query)) { return "" }
    $cli = Get-VcoCodegraphCli
    if (-not $cli) { return "" }

    $argList = @("search", $Query)
    if ($ProjectArg) { $argList += ($ProjectArg -split '\s+') }
    $argList += @("--limit", "$Limit", "--hook-format")

    $raw = ""
    try {
        $job = Start-Job -ScriptBlock {
            param($cli, $argList)
            if ($cli.EndsWith(".ps1")) {
                & $cli @argList 2>$null
            } else {
                & bash $cli @argList 2>$null
            }
        } -ArgumentList $cli, $argList
        if (Wait-Job $job -Timeout 4) {
            $raw = (Receive-Job $job) -join "`n"
        } else {
            Stop-Job $job -ErrorAction SilentlyContinue
        }
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    } catch { return "" }

    if ([string]::IsNullOrEmpty($raw)) { return "" }
    $lines = $raw -split "`n"
    if ($ExcludePath) {
        $lines = $lines | Where-Object { $_ -notmatch [regex]::Escape($ExcludePath) }
    }
    return (($lines | Select-Object -First 20) -join "`n")
}

# Test-VcoCodegraphSymbolGate <Text> -- $true if Text references a code symbol or
# code-file path (the same 4 shapes as the .sh gate). MUST MATCH
# codegraph-query.sh codegraph_symbol_gate.
function Test-VcoCodegraphSymbolGate {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $false }
    # (4) code-file path.
    if ($Text -match '[^\s]+\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)([^A-Za-z0-9]|$)') { return $true }
    # (1) dotted symbol token.
    if ($Text -match '[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]') { return $true }
    # (2) CamelCase identifier.
    if ($Text -match '[A-Z][a-z]+[A-Z]') { return $true }
    # (3) call shape: name(
    if ($Text -match '[A-Za-z_][A-Za-z0-9_]*\(') { return $true }
    return $false
}

# Test-VcoCodegraphPatternGate <Pattern> -- $true if Pattern is a code IDENTIFIER
# (snake/CamelCase/name(/keyword id). NOT on bare all-caps/lowercase word or bare
# dotted token. Used by the Grep surface + Test-VcoCodegraphBashGate (one home).
# MUST MATCH codegraph-query.sh codegraph_pattern_gate.
function Test-VcoCodegraphPatternGate {
    param([string]$Pattern)
    if ([string]::IsNullOrEmpty($Pattern)) { return $false }
    if ($Pattern -match '[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+') { return $true }   # snake_case
    if ($Pattern -match '[A-Z][a-z]+[A-Z]') { return $true }                      # CamelCase
    if ($Pattern -match '[A-Za-z_][A-Za-z0-9_]*\(') { return $true }              # name(
    if ($Pattern -match '(^|[^A-Za-z0-9_])(def|class|func|function|fn)\s+[A-Za-z_]') { return $true }  # keyword id
    return $false
}

# Test-VcoCodegraphBashGate <Command> -- pre-bash-SPECIFIC gate (tighter than
# Test-VcoCodegraphSymbolGate). Fires ONLY when the bash command navigates code.
# MUST MATCH codegraph-query.sh codegraph_bash_gate (same A/B rules + the same
# negatives: ls, cd, git status, git log a.b.c, cat notes.txt, grep foo.bar,
# grep "TODO").
function Test-VcoCodegraphBashGate {
    param([string]$Command)
    if ([string]::IsNullOrEmpty($Command)) { return $false }

    # (B) code-file path (clean stem before ext, path boundary) -- rejects a.b.c.
    if ($Command -match '(^|[\s/])[A-Za-z0-9_-]+\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto)([^A-Za-z0-9]|$)') { return $true }

    # (A) code-search tool present AND an identifier-shaped pattern.
    if ($Command -match '(^|[\s|])(grep|rg|ag|ack)(\s|$)') {
        if (Test-VcoCodegraphPatternGate -Pattern $Command) { return $true }
    }
    return $false
}

# Get-VcoCodegraphSymbol <Text> -- first symbol/path token the gate matched
# (query), capped to 200 chars; whole text when no discrete token isolable.
function Get-VcoCodegraphSymbol {
    param([string]$Text)
    $tok = ""
    foreach ($word in ($Text -split '\s+')) {
        $w = $word.Trim('"').Trim("'")
        if ($w.StartsWith("-")) { continue }   # skip flags
        if ($w -match '^[^\s]+\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$' `
            -or $w -match '[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]' `
            -or $w -match '[A-Z][a-z]+[A-Z]' `
            -or $w -match '[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+' `
            -or $w -match '[A-Za-z_][A-Za-z0-9_]*\(') {
            $tok = $w
            break
        }
    }
    if (-not $tok) { $tok = $Text }
    if ($tok.Length -gt 200) { $tok = $tok.Substring(0, 200) }
    return $tok
}
