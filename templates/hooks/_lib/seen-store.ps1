# _lib/seen-store.ps1
# Unified per-session read/inject dedup store, dot-sourced by the .ps1 context
# hooks (pre-edit-context-inject, pre-bash-context-inject, pre-tool-use). The
# PowerShell sibling of _lib/seen-store.sh.
#
# Why this exists (one concern, one home -- CLAUDE.md "search before add")
# -----------------------------------------------------------------------
# v0.2.70 Stream E. See seen-store.sh for the full rationale. In brief: it
# unifies the inject-dedup (was inline + title-coarse in pre-edit only, absent
# in pre-bash) and the explicit-Read ledger (was written but never consulted)
# behind one set of functions, keyed per-session.
#
# Granularity:
#   KG   -> PER-CHUNK. Key = "<title>#<sha1(body)[:12]>" (a NEW chunk of a seen
#           node has a different body -> different key -> STILL injects).
#   CODE -> PER-ENTITY. Key = "<full_name>".
#
# TOP RISK: cross-session bleed via a shared "default" bucket. Get-VcoHookSessionId
# returns "" for missing/malformed AND "default" for a hostile id. EITHER value
# means "no trustworthy per-chat key" -> Get-VcoSeenStorePath returns "" and
# Invoke-VcoFilterSeenBlocks then dedups NOTHING (inject blind). Never write a
# shared bucket.
#
# MUST MATCH: templates/hooks/_lib/seen-store.sh -- the KEY FORMAT
# ("<title>#<sha1(body)[:12]>" for KG, "<full_name>" for CODE), the
# inject-blind-on-empty/default policy, and the header regex must agree
# cross-OS. Any change to the key shape here MUST be mirrored there.
#
# Plain ASCII only (no em-dash, no BOM needed). Dot-sourced, never executed.

# --- Idempotent double-source guard ---------------------------------------
if ($script:VcoSeenStoreSourced) { return }
$script:VcoSeenStoreSourced = $true

# Get-VcoSeenStorePath: resolve the per-session store file for a given kind.
#   -Kind       "inject" | "reads"
#   -SessionId  already-sanitised id from Get-VcoHookSessionId
#   -ProjectRoot
# Returns the absolute path, OR "" when SessionId is untrustworthy ("" or
# "default") -- the caller MUST treat "" as "no store; inject blind".
#
# File-name convention (MUST MATCH seen-store.sh):
#   inject -> seen_inject_<sid>.txt
#   reads  -> seen_reads_<sid>.txt  (INJECTOR reads-ledger, repo-relative paths;
#             DISTINCT from pre-tool-use's Build-Anchor reads_<sid>.txt -- SF-1)
function Get-VcoSeenStorePath {
    param(
        [string]$Kind,
        [string]$SessionId,
        [string]$ProjectRoot
    )
    if ([string]::IsNullOrEmpty($SessionId) -or $SessionId -eq "default") { return "" }
    if ([string]::IsNullOrEmpty($ProjectRoot)) { return "" }
    $stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
    return (Join-Path $stateDir ("seen_{0}_{1}.txt" -f $Kind, $SessionId))
}

# ConvertTo-VcoRepoRelative <Path> <ProjectRoot>
# The ONE PowerShell-side home (SF-1) for normalising a path to the REPO-RELATIVE
# shape the producers' "| src=" trailers use. Used by the reads-ledger writer +
# codegraph self-exclude in pre-tool-use.ps1. Already-relative paths and absolute
# paths outside the project root are returned unchanged (slashes normalised to
# forward-slash to match the producers' POSIX src). MUST MATCH seen-store.sh's
# vco_to_repo_relative.
function ConvertTo-VcoRepoRelative {
    param([string]$Path, [string]$ProjectRoot)
    if ([string]::IsNullOrEmpty($Path)) { return "" }
    if ($ProjectRoot -and $Path.StartsWith($ProjectRoot)) {
        return ($Path.Substring($ProjectRoot.Length).TrimStart('/', '\') -replace '\\', '/')
    }
    return $Path
}

# Test-VcoSeenHas <File> <Key> -- $true if Key is present (exact line match).
function Test-VcoSeenHas {
    param([string]$File, [string]$Key)
    if ([string]::IsNullOrEmpty($File)) { return $false }
    if (-not (Test-Path -LiteralPath $File)) { return $false }
    try {
        foreach ($line in [System.IO.File]::ReadLines($File)) {
            if ($line -eq $Key) { return $true }
        }
    } catch { return $false }
    return $false
}

# Add-VcoSeen <File> <Key> -- append Key to the store (soft-fail).
function Add-VcoSeen {
    param([string]$File, [string]$Key)
    if ([string]::IsNullOrEmpty($File)) { return }
    try { Add-Content -LiteralPath $File -Value $Key -ErrorAction Stop } catch { }
}

# Test-VcoSeenSrcMatches <ReadsFile> <Src> [ProjectRoot]
# P4 (v0.2.91): rule-(b) comparison WITH PATH-FORM NORMALIZATION.
# The ledger holds REPO-RELATIVE paths, but a producer's "| src=" trailer can be
# absolute (a peer / extra-path code graph rooted elsewhere), so a single
# exact-match check silently never fired for those. Compare on BOTH shapes --
# normalizing only to absolute would REGRESS the common relative-vs-relative
# match. Each check stays EXACT (no prefix fuzz), so a never-Read path can never
# be mistaken for a Read one. MUST MATCH seen-store.sh's vco_seen_src_matches.
#
# SEPARATOR normalization (v0.2.91 wave-4 NIT-3, WINDOWS-ONLY surface): the
# composed absolute form used '/' unconditionally, so a ledger entry written on
# Windows in the as-Read BACKSLASH form ("C:\repo\knowledge\x.md") never matched
# the '/'-composed candidate -- and ConvertTo-VcoRepoRelative's prefix test
# likewise fails when Src and ProjectRoot disagree on the separator. Every
# candidate is therefore tried in BOTH separator forms. This has no .sh sibling
# by design: only Windows produces backslash paths, and Git Bash normalises them
# before any hook sees them, so vco_seen_src_matches stays separator-free.
function Test-VcoSeenSrcMatches {
    param([string]$ReadsFile, [string]$Src, [string]$ProjectRoot = "")
    if ([string]::IsNullOrEmpty($ReadsFile) -or [string]::IsNullOrEmpty($Src)) { return $false }
    if (Test-VcoSeenHas -File $ReadsFile -Key $Src) { return $true }
    if (-not $ProjectRoot) {
        $ProjectRoot = if ($script:ProjectRoot) { $script:ProjectRoot } elseif ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { "" }
    }
    if (-not $ProjectRoot) { return $false }
    $candidates = @()
    if ([System.IO.Path]::IsPathRooted($Src)) {
        $rel = ConvertTo-VcoRepoRelative -Path $Src -ProjectRoot $ProjectRoot
        if ($rel) { $candidates += $rel }
        # Retry the strip with both sides separator-normalised: the helper is a
        # prefix match, so a backslash Src under a forward-slash root (or the
        # converse) does not strip and would compare as an unchanged absolute.
        $relNorm = ConvertTo-VcoRepoRelative -Path ($Src -replace '\\', '/') `
            -ProjectRoot ($ProjectRoot -replace '\\', '/')
        if ($relNorm -and ($relNorm -ne $rel)) { $candidates += $relNorm }
    } else {
        $candidates += (($ProjectRoot -replace '\\', '/').TrimEnd('/') + '/' + ($Src -replace '\\', '/'))
    }
    foreach ($cand in $candidates) {
        foreach ($shape in @($cand, ($cand -replace '/', '\'))) {
            if (Test-VcoSeenHas -File $ReadsFile -Key $shape) { return $true }
        }
    }
    return $false
}

# --- Per-session codegraph inject VOLUME cap (v0.2.72 P6) ------------------
# The dedup store bounds RE-injection (same identity) but NOT total injection: a
# long session navigating many DISTINCT entities injects a fresh block for each.
# This cap bounds the TOTAL codegraph injections per session_id. Mechanism: a
# tiny per-session counter file under the SAME .claude/state/ dir + session
# keying as the dedup store. MUST MATCH seen-store.sh -- file-name convention
# (seen_cginject_count_<sid>.txt), default cap (40), fail-open-on-error policy,
# and the emit-the-note-once contract.

# Get-VcoCgInjectCountPath <SessionId> <ProjectRoot>
# Return the per-session codegraph-inject counter path, or "" for an
# untrustworthy session id ("" / "default") -- caller then runs UNCAPPED.
function Get-VcoCgInjectCountPath {
    param([string]$SessionId, [string]$ProjectRoot)
    if ([string]::IsNullOrEmpty($SessionId) -or $SessionId -eq "default") { return "" }
    if ([string]::IsNullOrEmpty($ProjectRoot)) { return "" }
    $stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
    return (Join-Path $stateDir ("seen_cginject_count_{0}.txt" -f $SessionId))
}

# Get-VcoCgInjectCap -- effective cap (env override VCO_CG_INJECT_CAP, else 40).
# A non-numeric / non-positive override falls back to 40 (a fat-fingered value
# must not silently disable ALL injection).
function Get-VcoCgInjectCap {
    $cap = 40
    $raw = $env:VCO_CG_INJECT_CAP
    if (-not [string]::IsNullOrEmpty($raw)) {
        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed) -and $parsed -gt 0) { $cap = $parsed }
    }
    return $cap
}

# Test-VcoCgInjectCapped <CountFile>
# READ-ONLY predicate: $true (capped -- suppress) when the per-session count has
# reached the cap, else $false (still room). Does NOT mutate the counter, so the
# caller can short-circuit BEFORE the heavy codegraph query. Soft-fail: ""
# CountFile (untrustworthy session) OR read error -> $false (not capped).
# MUST MATCH seen-store.sh vco_cg_inject_capped.
function Test-VcoCgInjectCapped {
    param([string]$CountFile)
    if ([string]::IsNullOrEmpty($CountFile)) { return $false }
    if (-not (Test-Path -LiteralPath $CountFile)) { return $false }
    $cap = Get-VcoCgInjectCap
    $n = 0
    try {
        $raw = (Get-Content -LiteralPath $CountFile -Raw -ErrorAction Stop).Trim()
        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed)) { $n = $parsed }
    } catch { $n = 0 }
    if ($n -ge $cap) { return $true }
    return $false
}

# Add-VcoCgInjectRecord <CountFile>
# Increment the per-session inject counter by one. Called ONLY when a block is
# actually emitted (cap counts real injections, not query attempts). Soft-fail:
# "" CountFile or write error -> no-op. MUST MATCH seen-store.sh
# vco_cg_inject_record.
function Add-VcoCgInjectRecord {
    param([string]$CountFile)
    if ([string]::IsNullOrEmpty($CountFile)) { return }
    $n = 0
    if (Test-Path -LiteralPath $CountFile) {
        try {
            $raw = (Get-Content -LiteralPath $CountFile -Raw -ErrorAction Stop).Trim()
            $parsed = 0
            if ([int]::TryParse($raw, [ref]$parsed)) { $n = $parsed }
        } catch { $n = 0 }
    }
    $n = $n + 1
    try { Set-Content -LiteralPath $CountFile -Value $n -ErrorAction Stop } catch { }
}

# Test-VcoCgInjectNoteOnce <SessionId> <ProjectRoot>
# Return $true (emit the cap note now) EXACTLY ONCE per session, $false
# thereafter -- via a sentinel file next to the counter. Soft-fail: unkeyable
# session or write error -> $false (do NOT spam the note when we can't dedup it).
function Test-VcoCgInjectNoteOnce {
    param([string]$SessionId, [string]$ProjectRoot)
    if ([string]::IsNullOrEmpty($SessionId) -or $SessionId -eq "default") { return $false }
    if ([string]::IsNullOrEmpty($ProjectRoot)) { return $false }
    $stateDir = Join-Path (Join-Path $ProjectRoot ".claude") "state"
    $sentinel = Join-Path $stateDir ("seen_cginject_capnote_{0}.txt" -f $SessionId)
    if (Test-Path -LiteralPath $sentinel) { return $false }
    try { Set-Content -LiteralPath $sentinel -Value "1" -ErrorAction Stop } catch { return $false }
    return $true
}

# Get-VcoCapKeyField <Text> -- truncate a key's identity field (KG title / CODE
# full_name) to at most 200 UTF-8 BYTES, cut on a character boundary.
#
# MUST MATCH seen-store.sh's vco_cap_key_field and
# templates/scripts/mcp_retrieval_record.py's cap_key_field. All three write
# keys into the SAME per-session store, so the truncation axis has to be one
# thing everywhere.
#
# Why BYTES (v0.2.91 wave-4 NIT-4): Substring(0, 200) counts UTF-16 CODE UNITS
# (2 per astral char), bash's ${v:0:200} counts characters under a UTF-8 locale
# and bytes under LC_ALL=C, Python's [:200] counts code points. For a >200-byte
# non-ASCII title that is three different keys -> the recorder's key never
# matches the injector's and the suppression silently misses (fails open).
# Bytes is the one definition all three can pin. The back-off over trailing
# CONTINUATION bytes (10xxxxxx) drops a truncated multi-byte sequence rather
# than emitting a half-written one, matching the other two exactly.
function Get-VcoCapKeyField {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return "" }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    if ($bytes.Length -le 200) { return $Text }
    $n = 200
    while (($n -gt 0) -and ((($bytes[$n]) -band 0xC0) -eq 0x80)) { $n-- }
    return [System.Text.Encoding]::UTF8.GetString($bytes, 0, $n)
}

# Get-VcoSeenFirstField <Rest> -- first " | "-delimited field of a header,
# capped to 200 UTF-8 bytes. Strips an accidentally-doubled prefix.
function Get-VcoSeenFirstField {
    param([string]$Rest)
    $r = $Rest
    if ($r.StartsWith("KG: ")) { $r = $r.Substring(4) }
    if ($r.StartsWith("CODE: ")) { $r = $r.Substring(6) }
    $idx = $r.IndexOf(" | ")
    $field = if ($idx -ge 0) { $r.Substring(0, $idx) } else { $r }
    return (Get-VcoCapKeyField -Text $field)
}

# Get-VcoSeenNormalizedBody <Text> -- canonicalize a block body before hashing:
# the trailing run of newlines collapses to EXACTLY ONE (an empty body stays
# empty).
#
# WHY (v0.2.91 wave-4 MINOR-5): the number of trailing newlines a block carries
# depends on WHERE IN THE BLOB it sits, which is not a property of the content.
# The producer prints header + `print(body)`, so a body that itself ends in a
# newline emits an extra EMPTY line; the caller's whole-blob capture then strips
# trailing newlines, so that empty line survives for every block EXCEPT THE
# LAST. Two different keys for the same chunk, and neither predictable by the
# MCP-retrieval recorder (it cannot know a result's eventual position).
# Collapsing here makes the key a function of the CONTENT only.
#
# MUST MATCH seen-store.sh's vco_seen_normalize_body and
# mcp_retrieval_record.py's normalize_block_body.
function Get-VcoSeenNormalizedBody {
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return "" }
    $trimmed = $Text.TrimEnd("`n")
    if ([string]::IsNullOrEmpty($trimmed)) { return "" }
    return ($trimmed + "`n")
}

# Get-VcoSeenHash <Text> -- sha1 of the body, first 12 hex chars.
# MUST MATCH seen-store.sh vco_seen_hash (sha1, first 12 hex).
function Get-VcoSeenHash {
    param([string]$Text)
    try {
        $sha1 = [System.Security.Cryptography.SHA1]::Create()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        $hash = $sha1.ComputeHash($bytes)
        $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
        return $hex.Substring(0, 12)
    } catch {
        # Fallback sketch (length + head), distinct enough in practice.
        $len = $Text.Length
        $head = ($Text.Substring(0, [Math]::Min(24, $Text.Length))) -replace '[^a-zA-Z0-9]', '_'
        return ("{0}_{1}" -f $len, $head)
    }
}

# Invoke-VcoFilterSeenBlocks <Input> <InjectFile> <ReadsFile>
# Migrated + extended _filter_seen. Returns only blocks not already-seen this
# session. Suppress if (a) dedup KEY in InjectFile, or (b) source path (from a
# "| src=<path>" header suffix) in ReadsFile. When InjectFile is "" (untrusted
# session id), dedup is DISABLED (inject blind). Newly-emitted blocks get their
# KEY appended to InjectFile.
function Invoke-VcoFilterSeenBlocks {
    param(
        [string]$InputText,
        [string]$InjectFile,
        [string]$ReadsFile = ""
    )

    $dedupOn = $true
    if ([string]::IsNullOrEmpty($InjectFile)) {
        $dedupOn = $false
    } else {
        try {
            if (-not (Test-Path -LiteralPath $InjectFile)) {
                New-Item -ItemType File -Path $InjectFile -Force -ErrorAction Stop | Out-Null
            }
        } catch { $dedupOn = $false }
    }

    $sb = New-Object System.Text.StringBuilder
    $curPrefix = ""
    $curFirst = ""
    $curSrc = ""
    $curBlock = ""
    $curBody = ""

    # v0.2.70 FIX-1: the parse accumulators ($curPrefix/$curFirst/$curSrc/
    # $curBlock/$curBody) are FUNCTION-LOCAL. The flush logic MUST read and
    # write the SAME scope as the parse loop. The earlier version mutated
    # `$script:`-scoped copies inside a `& $flush` closure while the loop read
    # the bare function-local copies, so the flush never saw the loop's writes
    # (and vice-versa) -> injected KG/CODE blocks were shredded to orphaned
    # body fragments and dedup never recorded a key. Using `$script:` for
    # per-call parse state is ALSO a re-entrancy bug (a second call would see
    # the first call's leftovers). The fix: invoke the flush block with the
    # dot-source operator (`. $flush`) so it runs in THIS function's scope and
    # the bare-name reads/writes target the function-local accumulators
    # directly -- matching the bash sibling's dynamic-scope `_vco_flush`, which
    # mutates the enclosing function's locals.
    $flush = {
        if ([string]::IsNullOrEmpty($curPrefix)) {
            $curPrefix = ""; $curFirst = ""; $curSrc = ""
            $curBlock = ""; $curBody = ""
            return
        }
        if ($curPrefix -eq "KG") {
            # sha1 of the NORMALIZED body -- the normalization is what makes the
            # key depend on the CONTENT rather than on the block's position in
            # the blob (see Get-VcoSeenNormalizedBody).
            $bh = Get-VcoSeenHash -Text (Get-VcoSeenNormalizedBody -Text $curBody)
            $key = "{0}#{1}" -f $curFirst, $bh
        } else {
            $key = $curFirst
        }
        $suppress = $false
        if ($dedupOn) {
            if (Test-VcoSeenHas -File $InjectFile -Key $key) { $suppress = $true }
            # P4 (v0.2.91): compare through Test-VcoSeenSrcMatches, which tries
            # BOTH path shapes; the old single-shape check silently never matched
            # when the ledger and the producer disagreed on relative-vs-absolute.
            if ((-not $suppress) -and $curSrc -and $ReadsFile -and (Test-VcoSeenSrcMatches -ReadsFile $ReadsFile -Src $curSrc)) {
                $suppress = $true
            }
        }
        if (-not $suppress) {
            [void]$sb.Append($curBlock)
            if ($dedupOn) { Add-VcoSeen -File $InjectFile -Key $key }
        }
        $curPrefix = ""; $curFirst = ""; $curSrc = ""
        $curBlock = ""; $curBody = ""
    }

    foreach ($line in ($InputText -split "`n")) {
        $line = $line.TrimEnd("`r")
        $m = [regex]::Match($line, '^(KG|CODE): (.+)$')
        if ($m.Success) {
            . $flush
            $curPrefix = $m.Groups[1].Value
            $rest = $m.Groups[2].Value
            $curFirst = Get-VcoSeenFirstField -Rest $rest
            $curSrc = ""
            $sidx = $rest.IndexOf("| src=")
            if ($sidx -ge 0) {
                $curSrc = ($rest.Substring($sidx + 6)).TrimEnd()
            }
            $curBlock = $line + "`n"
            $curBody = ""
        } elseif (-not [string]::IsNullOrEmpty($curPrefix)) {
            $curBlock = $curBlock + $line + "`n"
            $curBody = $curBody + $line + "`n"
        } else {
            if ($line -match '\S') {
                [void]$sb.Append($line + "`n")
            }
        }
    }
    . $flush

    return $sb.ToString()
}
