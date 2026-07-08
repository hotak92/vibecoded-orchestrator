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
# Test-VcoCodegraphBashGate + Test-VcoCodegraphPatternGate regexes.
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

# Invoke-VcoCodegraphQueryBlock <Query> <ProjectArg> <Limit> <ExcludePath> [Anchor]
# Return the raw "CODE:"-prefixed --hook-format block(s), or "". Soft-fail to ""
# on absent CLI / error / empty. Bounded by a 4s job timeout so a hung child
# can't hang the hook. -ExcludePath: forwarded to the CLI as `--exclude-file`
# so candidates from that file are culled BEFORE the result trim (v0.2.72 B2 --
# replaces the old post-hoc line-wise filter, which stripped only the CODE:
# header line and left orphaned body lines). -Anchor (optional): edited-file
# path or grep symbol, forwarded as `--anchor` so the CLI's shared retrieval
# pipeline biases the rerank toward call-linked / same-module / shared-type
# code (v0.2.72 P2). Empty -> pure semantic (MCP parity). MUST MATCH
# codegraph-query.sh codegraph_query_block ($4 exclude_path / $5 anchor).
function Invoke-VcoCodegraphQueryBlock {
    param(
        [string]$Query,
        [string]$ProjectArg = "",
        [int]$Limit = 2,
        [string]$ExcludePath = "",
        [string]$Anchor = ""
    )
    if ([string]::IsNullOrEmpty($Query)) { return "" }
    $cli = Get-VcoCodegraphCli
    if (-not $cli) { return "" }

    $argList = @("search", $Query)
    if ($ProjectArg) { $argList += ($ProjectArg -split '\s+') }
    $argList += @("--limit", "$Limit", "--hook-format")
    # B2: root-fix self-exclusion -- the CLI drops the file's candidates
    # pre-trim. MUST MATCH codegraph-query.sh codegraph_query_block.
    if ($ExcludePath) { $argList += @("--exclude-file", $ExcludePath) }
    if ($Anchor) { $argList += @("--anchor", $Anchor) }

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
    # Cap the volume. Self-reference exclusion happens INSIDE the CLI via
    # --exclude-file (B2) -- the old line-wise filter here stripped only the
    # CODE: header line and left orphaned body lines. Do not re-add it.
    $lines = $raw -split "`n"
    return (($lines | Select-Object -First 20) -join "`n")
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

# Test-VcoCodegraphBashGate <Command> -- pre-bash-SPECIFIC gate. Fires ONLY when
# the bash command navigates code (delegates the identifier-shape check to
# Test-VcoCodegraphPatternGate). MUST MATCH codegraph-query.sh codegraph_bash_gate
# (same A/B rules + the same negatives: ls, cd, git status, git log a.b.c,
# cat notes.txt, grep foo.bar, grep "TODO").
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

# Get-VcoCodegraphSymbol <Text> -- first REAL code-symbol/path token (query),
# capped to 200 chars. P1e (v0.2.75): reject env-assignments, non-code paths,
# regex/glob fragments, redirects, URLs; return EMPTY when no discrete symbol
# is isolable (the caller then skips injection — a garbage whole-command query
# is worse than none). MUST MATCH codegraph-query.sh codegraph_extract_symbol.
$script:CgqNonCodeExtRe = '\.(log|txt|json|jsonl|yaml|yml|toml|lock|tar|gz|zip|md|html|css)$'
$script:CgqSourceExtRe  = '\.(py|js|mjs|jsx|ts|tsx|go|rs|lua|cpp|cc|cxx|c|h|hpp|java|rb|cs|proto|sh|bash)$'
function Get-VcoCodegraphSymbol {
    param([string]$Text)
    $tok = ""
    foreach ($word in ($Text -split '\s+')) {
        $w = $word.Trim('"').Trim("'")
        if (-not $w) { continue }
        if ($w.StartsWith("-")) { continue }   # skip flags
        # NOTE: -cmatch (case-SENSITIVE) throughout to mirror bash's `[[ =~ ]]`
        # (which is case-sensitive). PowerShell's bare -match is
        # case-INSENSITIVE, which would make the CamelCase rule
        # `[A-Z][a-z]+[A-Z]` match ANY 3+ letter word (e.g. `curl`) — a
        # divergence P1e's fixture-parity tests caught.
        # P1e: skip env-assignments (FOO=bar / LEAN_CTX_OFF=1).
        if ($w -cmatch '^[A-Za-z_][A-Za-z0-9_]*=') { continue }
        # P1e: skip redirects (2>, >>, <) and URLs.
        if ($w -cmatch '^[0-9]*[<>]') { continue }
        if ($w -cmatch '^https?://') { continue }
        # P1e: skip words with regex/glob metacharacters. `(` deliberately
        # allowed so a `symbol(` call-shape still matches below.
        if ($w -cmatch '[\\|\^\$\[\*\?]') { continue }
        # P1e: a `/`-bearing word qualifies ONLY as a real SOURCE file
        # (source extension AND not a non-code extension); else skip.
        if ($w -like '*/*') {
            if (($w -cmatch $script:CgqSourceExtRe) -and -not ($w -cmatch $script:CgqNonCodeExtRe)) {
                $tok = $w; break
            }
            continue
        }
        if ($w -cmatch $script:CgqSourceExtRe `
            -or $w -cmatch '[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]' `
            -or $w -cmatch '[A-Z][a-z]+[A-Z]' `
            -or $w -cmatch '[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+' `
            -or $w -cmatch '[A-Za-z_][A-Za-z0-9_]*\(') {
            $tok = $w
            break
        }
    }
    # P1e: NO whole-text fallback — empty means "no isolable symbol".
    if ($tok.Length -gt 200) { $tok = $tok.Substring(0, 200) }
    return $tok
}
