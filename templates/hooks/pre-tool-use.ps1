# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed. 2026-07-13 (v0.2.80): block-reason messages routed to stderr via [Console]::Error.WriteLine to MATCH the .sh sibling (which already writes these to stderr) — one-directional align-to-sh, no Bash-side change to co-modify.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# VCO-CENTRALIZED-KG: NOT a KG consumer (v0.2.101 wave-3). The former
#   section-5 KG-suggestion path was retired (double emission with the
#   pre-edit/pre-write router wrappers -- review nit-6); every remaining
#   branch (SSRF guard, shell-injection scan, Build Anchor, file backup,
#   tool logging) is KG/codegraph-free. Marker kept (the centralization
#   audit classifies every hook); classification: no KG access.

# pre-tool-use.ps1
# Pre-tool-use hook: SSRF guard, shell injection scan, Build Anchor
# Protocol, file backup. (KG search suggestion RETIRED, v0.2.101 wave-3.)
#
# v0.2.77 9-bis: the per-tool-call TOUCAN dataset writer
# (.claude/logs/toucan_dataset.jsonl) was RETIRED here — a write-only
# collector with zero consumers (RL training telemetry lives in
# launcher.db rl_events + the citation drain, unaffected). MUST MATCH
# pre-tool-use.sh (same removal). The stdin parse below still decodes the
# full payload for the security + Build-Anchor branches.

. "$PSScriptRoot/_lib/stderr-cap.ps1"
# Source emit-context.ps1 ONLY if the file exists. If the helper is
# missing (partial install or just-after-clone before _lib/ is fully
# populated), the hook still runs its other branches (logging,
# security guards). The KG-suggestion branch below uses Get-Command
# to tolerate a missing Emit-AdditionalContext.
if (Test-Path "$PSScriptRoot/_lib/emit-context.ps1") {
    . "$PSScriptRoot/_lib/emit-context.ps1"
}

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
# Positional args ($args) and $env:CLAUDE_TOOL_NAME etc. are EMPTY because
# Claude Code does NOT populate those env vars — verified empirically
# 2026-05-08 via stdin-capture diagnostic. Without this, the SSRF guard,
# shell-injection scan, and Build-Anchor branches would all see empty
# tool_name / tool_input.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$ToolName = ""
# v0.2.76 P5 (hook-latency parity): the bash sibling consolidated its stdin
# parse from SIX `python -c` spawns into ONE NUL-delimited decode (each
# interpreter cold-start cost ~15ms and this hook fires on the `*` matcher —
# every tool call). PowerShell already single-decodes here via one
# ConvertFrom-Json — no per-field re-parse ever existed — so the perf issue
# was bash-only. This touch keeps the OS-parity gate satisfied AND aligns the
# tool_input default with the bash side: default to a valid "{}" (not "") so a
# missing tool_input yields the same empty-object shape on both OSes.
$ToolArgs = "{}"
$UserMessage = ""
$SessionIdFromStdin = ""
# V52-L.2 Fix 1: parse subagent identity from stdin payload. Per A5
# audit, PreToolUse hooks DO fire for subagent tool calls and the
# payload carries agent_id + agent_type. Parsed for parity with the
# post-tool-security / post-file-edit siblings that still consume them;
# this hook's own former consumer (the TOUCAN row) was retired in
# v0.2.77 9-bis. Empty string when absent (parent context).
$AgentId = ""
$AgentType = ""
# WP-E (v0.2.92): transcript_path + prompt_id ride the shared parse.
# v0.2.101 wave-3: this hook no longer CONSUMES them (section 5 retired --
# the router wrappers resolve both from the payload themselves); the fields
# stay in the parse so the sibling contract is unchanged. transcript_path
# remains a PATH ONLY everywhere (R31). MUST MATCH pre-tool-use.sh's
# NUL-delimited parse.
$TranscriptPath = ""
$PromptId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.tool_name)       { $ToolName = [string]$payload.tool_name }
        if ($payload.tool_input)      { $ToolArgs = ($payload.tool_input | ConvertTo-Json -Compress -Depth 8) }
        if ($payload.user_message)    { $UserMessage = [string]$payload.user_message }
        if ($payload.session_id)      { $SessionIdFromStdin = [string]$payload.session_id }
        if ($payload.agent_id)        { $AgentId = [string]$payload.agent_id }
        if ($payload.agent_type)      { $AgentType = [string]$payload.agent_type }
        if ($payload.transcript_path) { $TranscriptPath = [string]$payload.transcript_path }
        if ($payload.prompt_id)       { $PromptId = [string]$payload.prompt_id }
    }
} catch {
    # Empty/malformed stdin — keep variables at defaults
}

$ScriptDir = $PSScriptRoot
# v0.2.29: prefer canonical $CLAUDE_PROJECT_DIR (the active workspace
# the launcher hands us). Fall back to SCRIPT_DIR/../.. for ad-hoc
# invocations.
$ProjectRoot = if ($env:CLAUDE_PROJECT_DIR) {
    $env:CLAUDE_PROJECT_DIR
} else {
    (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
}

$LibDir = Join-Path $ScriptDir "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# v0.2.70 Streams C+E: shared helpers (canonical session-id, unified seen-store
# for the reads ledgers written in the Read branch below; v0.2.101 §C2/§C6
# retired this hook's code-graph INJECTION branches — read-context-inject /
# grep-context-inject are their homes now).
$SessionIdLib = Join-Path $LibDir "session-id.ps1"
if (Test-Path $SessionIdLib) { . $SessionIdLib }
$SeenStoreLib = Join-Path $LibDir "seen-store.ps1"
if (Test-Path $SeenStoreLib) { . $SeenStoreLib }
# (v0.2.101 wave-3: the query-cache.ps1 sourcing was retired with section 5 --
# its last caller. The lib itself was deleted; the router owns caching now.)
$script:ProjectRoot = $ProjectRoot

# v0.2.70 Stream E: unify session-id (parse+sanitise) with the other hooks.
# $SessionIdRaw preserves the trustworthy-vs-untrustworthy distinction for the
# unified reads store the injectors consult. $SessionId keeps the date fallback
# so the Build Anchor reads_*.txt still has a stable per-hour key.
if (Get-Command Get-VcoHookSessionId -ErrorAction SilentlyContinue) {
    $SessionIdRaw = Get-VcoHookSessionId -Stdin $HookStdin
} else {
    $SessionIdRaw = ($SessionIdFromStdin -replace '[^A-Za-z0-9_-]', '')
}
$SessionId = if ($SessionIdFromStdin) { $SessionIdFromStdin } elseif ($env:CLAUDE_SESSION_ID) { $env:CLAUDE_SESSION_ID } else { (Get-Date).ToString("yyyyMMdd_HH") }
# Per-session dedup state lives under the project's .claude/state/ rather
# than $env:TMPDIR / $env:TEMP so it survives reboots + launcher restarts
# (Claude Code persists session_id across restarts via the resume feature).
# Windows TEMP may be cleared on reboot too. .claude/state/ is gitignored
# and wiped only by PostCompact (correct semantic — context truly resets
# at compaction).
$SessionStateDir = Join-Path $ProjectRoot ".claude/state"
$SessionReadsFile = Join-Path $SessionStateDir "reads_$SessionId.txt"
$BackupDir = Join-Path $SessionStateDir "tool_backups"
$SecurityLog = Join-Path $ProjectRoot ".claude/logs/security_events.jsonl"

$LogsDir = Join-Path $ProjectRoot ".claude/logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null
}
New-Item -ItemType Directory -Force -Path $SessionStateDir -ErrorAction SilentlyContinue | Out-Null
New-Item -ItemType Directory -Force -Path $BackupDir -ErrorAction SilentlyContinue | Out-Null

# Best-effort 14-day GC of stale per-session reads files. Sessions that
# haven't been touched in two weeks are almost certainly abandoned;
# keeping them around just wastes inodes. Errors suppressed: housekeeping
# pass, not a correctness step.
# HK-4 (v0.2.75) accepted-scatter: one of 4 per-hook GC sweeps (uniform 14d);
# a shared sweeper is optional and deliberately SKIPPED to keep hooks
# single-file. MUST MATCH the .sh sibling. See pre-edit-context-inject.ps1.
try {
    Get-ChildItem -Path $SessionStateDir -Filter 'reads_*.txt' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # v0.2.70 Stream E (SF-1): same GC for the INJECTOR reads store.
    Get-ChildItem -Path $SessionStateDir -Filter 'seen_reads_*.txt' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
        Remove-Item -Force -ErrorAction SilentlyContinue
} catch { }

# v0.2.100 WP-17: read a tool_input field NATIVELY from the payload the
# ConvertFrom-Json above already decoded. The old Get-Field piped $ToolArgs
# through a `python -c` child, so with no Python on PATH it returned "" and
# every branch reading a field -- the Bash shell-injection scan above all --
# was silently skipped on Windows (and under Windows PowerShell 5.1 the pipe's
# ASCII $OutputEncoding turned non-ASCII into `?`). No interpreter is needed
# to read a decoded object. MUST MATCH pre-tool-use.sh's `_get_field` (a
# missing / null field is "", a value is its string form, trimmed).
function Get-Field([string]$field) {
    if (-not $payload -or -not $payload.tool_input) { return "" }
    try {
        $value = $payload.tool_input.$field
        if ($null -eq $value) { return "" }
        return ([string]$value).Trim()
    } catch { }
    return ""
}

# v0.2.100 WP-17: the ONE place a branch that genuinely needs Python says so,
# loudly, instead of skipping itself. Every time it goes to stderr for the
# human; once per session (a sentinel under .claude/state) it is queued in
# $script:VcoModelNotice, which the hook emits as its ONE additionalContext
# envelope at the hook's exit (the retired section-5 gate's successor) -- PreToolUse
# stderr on exit 0 is not shown to the model, and a second envelope on stdout
# would break the hook's JSON contract.
# MUST MATCH pre-tool-use.sh's _vco_report_no_python.
$script:VcoModelNotice = ""
function Write-VcoNoPythonNotice([string]$what) {
    $msg = "[VCO broken install] $what did NOT run: no Python interpreter was found (python / py / python3 on PATH). Put Python 3 on PATH or re-run the orchestrator's install / update (``python install.py --update`` in the orchestrator root, or the launcher's Update), then retry."
    [Console]::Error.WriteLine($msg)
    $key = if ($SessionIdRaw) { $SessionIdRaw } else { "default" }
    $sentinel = Join-Path $SessionStateDir "no_python_notice_$key"
    if (Test-Path -LiteralPath $sentinel) { return }
    try { Set-Content -LiteralPath $sentinel -Value "" -ErrorAction Stop } catch { }
    $script:VcoModelNotice = $msg
}

function Write-SecurityLine([string]$json) {
    try { Add-Content -Path $SecurityLog -Value $json -ErrorAction Stop } catch { }
}

# === 1. SSRF GUARD ===
if ($ToolName -eq "WebFetch") {
    # The URL comes from the payload ConvertFrom-Json already decoded, NOT
    # from Get-Field: that helper pipes the JSON through a `python -c` child,
    # which returns nothing when no Python is found (the guard would then be
    # skipped — the R18F-08 hole) and, under Windows PowerShell 5.1, pipes
    # with the ASCII $OutputEncoding, turning a non-ASCII host into `?`.
    $url = ""
    if ($payload -and $payload.tool_input -and $null -ne $payload.tool_input.url) {
        $url = ([string]$payload.tool_input.url).Trim()
    }
    if ($url) {
        # Allowed local services (Weaviate, Ollama, code-embed, vct-hub, :8082,
        # Gradio). SearXNG (:8888) and the mcp__search__fetch_page tool both
        # removed in v0.2.11 (see PR-14a); the search MCP itself was deleted
        # in v0.2.101, so nothing else reaches the network outside WebFetch.
        # v0.2.100: the decision is `python -m vco_lib.ssrf_url` (one
        # implementation for every OS; its docstring is the contract), run
        # once through _lib/ssrf-allowlist.ps1. MUST MATCH the .sh sibling.
        # The allowed pairs are DERIVED from the projected env, so a moved
        # service_endpoints port is allowed and nothing asks the user to
        # hand-edit this hook. FAIL CLOSED: only the exact words `allow` /
        # `pass` let the call through. The lib missing (partial install), no
        # interpreter, vco_lib not importable, or any other output blocks,
        # and says why.
        $ssrfLib = Join-Path $LibDir "ssrf-allowlist.ps1"
        $ssrfVerdict = ""
        $ssrfPairs = ""
        $ssrfWhy = "hooks/_lib/ssrf-allowlist.ps1 is missing - run the bundle update to restore it"
        if (Test-Path -LiteralPath $ssrfLib) {
            . $ssrfLib
            $ssrfCheck = Invoke-VcoSsrfCheck -Url $url -HooksDir $ScriptDir
            $ssrfVerdict = [string]$ssrfCheck.Verdict
            $ssrfPairs = [string]$ssrfCheck.Pairs
            $ssrfErr = if ($ssrfCheck.Error) { [string]$ssrfCheck.Error } else { "unrecognised answer '$ssrfVerdict'" }
            $ssrfWhy = "the guard could not run ($ssrfErr) - a broken VCO install: re-run the orchestrator's update (``python install.py --update`` in the orchestrator root, or the launcher's Update), which reinstalls vco_lib into the VCO venv"
        }
        if ($ssrfVerdict -cne "allow" -and $ssrfVerdict -cne "pass") {
            # Route the block message to STDERR (matches pre-tool-use.sh).
            # Claude Code's PreToolUse runner discards plain stdout - an
            # exit-2 hook with only-stdout renders as "hook error: No
            # stderr output". [Console]::Error.WriteLine goes to the true
            # stderr stream (Write-Output / the PS error stream would not).
            if ($ssrfVerdict -ceq "block") {
                if (-not $ssrfPairs) { $ssrfPairs = "none" }
                [Console]::Error.WriteLine("SSRF guard: '$url' targets a private/internal network address (or one the guard cannot read).")
                [Console]::Error.WriteLine("   Allowed local services on this machine: $ssrfPairs")
                [Console]::Error.WriteLine("   They follow WEAVIATE_URL / OLLAMA_URL / CODE_EMBED_SERVICE_URL and the hub port - a moved service is")
                [Console]::Error.WriteLine("   changed with the launcher's Services page or ``python -m vco_lib.service_endpoints move``, never by editing this hook.")
            } else {
                [Console]::Error.WriteLine("SSRF guard: '$url' was blocked because $ssrfWhy.")
            }
            $urlEsc = $url -replace '\\', '\\\\' -replace '"', '\"'
            Write-SecurityLine "{""timestamp"":""$ts"",""event"":""ssrf_blocked"",""url"":""$urlEsc""}"
            exit 2
        }
    }
}

# === 2. SHELL INJECTION SCAN ===
if ($ToolName -eq "Bash") {
    $cmd = Get-Field "command"
    $injection = ""
    if ($cmd -match '(?i)(curl|wget)\s[^|]+\|\s*(ba)?sh\b') { $injection = "network fetch piped to shell" }
    elseif ($cmd -match '(?i)eval\s+["\$(]*(curl|wget)') { $injection = "eval + network fetch" }
    elseif ($cmd -match '(?i)base64\s+-d.*\|\s*(ba)?sh\b') { $injection = "base64-decoded pipe to shell" }

    if ($injection) {
        # Route the block message to STDERR (matches pre-tool-use.sh) —
        # see the SSRF-guard branch above for why plain stdout is dropped.
        [Console]::Error.WriteLine("Shell injection guard: detected '$injection' in Bash command.")
        $preview = if ($cmd.Length -gt 120) { $cmd.Substring(0, 120) } else { $cmd }
        [Console]::Error.WriteLine("   Blocked command preview: $preview")
        [Console]::Error.WriteLine("   If this is intentional, run the command manually in a terminal.")
        $previewEsc = ($preview -replace '\\', '\\\\' -replace '"', '\"')
        Write-SecurityLine "{""timestamp"":""$ts"",""event"":""shell_injection_blocked"",""pattern"":""$injection"",""cmd_preview"":""$previewEsc""}"
        exit 2
    }

    # Extended security scan via bash_security.py if available. It is Python,
    # so with no interpreter it cannot run -- and says so (v0.2.100 WP-17)
    # instead of being skipped in silence. The regex scan above already ran.
    $SecurityScript = Join-Path $ProjectRoot ".claude/scripts/bash_security.py"
    if ((Test-Path $SecurityScript) -and -not $PY) {
        Write-VcoNoPythonNotice "The Bash security scanner (.claude/scripts/bash_security.py)"
    }
    if ((Test-Path $SecurityScript) -and $PY) {
        try {
            $secOut = $cmd | & $PY $SecurityScript 2>&1
            $secExit = $LASTEXITCODE
            if ($secExit -eq 2) {
                # Route to STDERR (matches pre-tool-use.sh's bash-security
                # branch) — same stdout-discard rationale as the SSRF /
                # injection guards above.
                [Console]::Error.WriteLine("Bash security scanner blocked this command:")
                [Console]::Error.WriteLine("   $secOut")
                $detail = if ("$secOut".Length -gt 200) { "$secOut".Substring(0,200) } else { "$secOut" }
                $detailEsc = $detail -replace '\\', '\\\\' -replace '"', '\"'
                $cmdPreview = if ($cmd.Length -gt 80) { $cmd.Substring(0,80) } else { $cmd }
                $cmdPreviewEsc = $cmdPreview -replace '\\', '\\\\' -replace '"', '\"'
                Write-SecurityLine "{""timestamp"":""$ts"",""event"":""bash_security_blocked"",""detail"":""$detailEsc"",""cmd_preview"":""$cmdPreviewEsc""}"
                exit 2
            }
        } catch { }
    }
}

# === v0.2.70 Stream C — RETIRED (v0.2.101 §C2/§C6) ============================
# The shared code-graph injection helper (Invoke-CgInject) and its two call
# surfaces — Read(code) and Grep(symbol) — were REMOVED. Their home is the
# router-backed hooks read-context-inject.ps1 (PostToolUse Read) and
# grep-context-inject.ps1 (PreToolUse Grep): exact-symbol lookups, gates,
# seen-store dedupe and the per-turn budget all live in
# claude_mcp_servers/scripts/hook_context_router.py + vco_lib/inject_intent.
# This hook keeps ONLY the PreToolUse concerns that must stay here: the
# security guards, the Build-Anchor reads ledger and the unified seen_reads
# ledger (both written below), file backup, and the §5 Edit/Write KG
# suggestion. MUST MATCH pre-tool-use.sh.

# === 3. BUILD ANCHOR PROTOCOL: track reads (the Read(code) code-graph inject
# moved to read-context-inject.ps1, v0.2.101 §C2) ===
if ($ToolName -eq "Read") {
    $filePath = Get-Field "file_path"
    if ($filePath) {
        # Build Anchor ledger (unchanged path/shape — harness exact-match gate).
        try { Add-Content -Path $SessionReadsFile -Value $filePath -ErrorAction Stop } catch { }
        # v0.2.70 Stream E (SF-1 fix): record a REPO-RELATIVE path into the
        # INJECTOR reads store (seen_reads_<sid>.txt, DISTINCT from the
        # Build-Anchor reads_<sid>.txt) so it matches the producers' repo-relative
        # "| src=<path>" trailer. An absolute ledger entry would NEVER match the
        # exact suppression. The abs->relative conversion is the ONE shared
        # ConvertTo-VcoRepoRelative helper (no inline copy). Skipped when the
        # session id is untrustworthy.
        $relFp = $filePath
        if (Get-Command ConvertTo-VcoRepoRelative -ErrorAction SilentlyContinue) {
            $relFp = ConvertTo-VcoRepoRelative -Path $filePath -ProjectRoot $ProjectRoot
        }
        if (Get-Command Get-VcoSeenStorePath -ErrorAction SilentlyContinue) {
            $unifiedReads = Get-VcoSeenStorePath -Kind "reads" -SessionId $SessionIdRaw -ProjectRoot $ProjectRoot
            if ($unifiedReads) {
                try { Add-Content -Path $unifiedReads -Value $relFp -ErrorAction Stop } catch { }
            }
        }
        # v0.2.101 injection redesign (§C2): the Read(code) code-graph
        # injection branch that used to live here was REMOVED — its home is
        # now the PostToolUse(Read) hook read-context-inject.ps1 (one
        # concern, one home; the kickoff probe measured the old branch as
        # always-killed by its 3 s settings timeout, so it never injected).
        # The ledger writes ABOVE stay: they must happen PreToolUse so
        # same-turn Write-anchor checks and seen-store suppression see them.
        # MUST MATCH pre-tool-use.sh.
    }
    exit 0
}

# === v0.2.70 Stream C Surface 4 (Grep) — RETIRED (v0.2.101 §C6) ============
# The Grep(symbol) code-graph injection branch was REMOVED: grep-context-
# inject.ps1 (PreToolUse(Grep), router surface `grep`) is its one home now.
# A Grep call falls through to the Write/Edit gate below (no-op for Grep)
# and exits at the §5 tool-name gate. MUST MATCH pre-tool-use.sh.

# === 4. BUILD ANCHOR + FILE BACKUP: Write/Edit checks ===
if ($ToolName -eq "Write" -or $ToolName -eq "Edit") {
    $filePath = Get-Field "file_path"
    if ($filePath) {
        if (Test-Path -LiteralPath $filePath -PathType Leaf) {
            # Existing file: check Build Anchor — WRITE ONLY.
            #
            # MUST MATCH templates/hooks/pre-tool-use.sh (section 4): the anchor
            # gate is enforced for `Write` (blind whole-file overwrite of an
            # unseen file) but NOT for `Edit` (Claude Code's built-in
            # read-before-edit rule already covers it; re-enforcing here was
            # redundant and a false-positive source vs the harness's own
            # file-state tracking). Defer Edit's read-before-edit to the harness.
            if ($ToolName -eq "Write") {
                $alreadyRead = $false
                if (Test-Path $SessionReadsFile) {
                    try {
                        foreach ($l in Get-Content $SessionReadsFile -ErrorAction Stop) {
                            if ($l -eq $filePath) { $alreadyRead = $true; break }
                        }
                    } catch { }
                }
                if (-not $alreadyRead) {
                    $bn = Split-Path $filePath -Leaf
                    # Route to STDERR (matches pre-tool-use.sh's Build Anchor
                    # branch) — same stdout-discard rationale as the guards
                    # above; an exit-2 hook with only-stdout renders as
                    # "hook error: No stderr output".
                    [Console]::Error.WriteLine("Build Anchor Protocol: '$bn' has not been Read this session.")
                    [Console]::Error.WriteLine("    Use the Read tool on this file before overwriting it with Write.")
                    $fpEsc = $filePath -replace '\\', '\\\\' -replace '"', '\"'
                    Write-SecurityLine "{""timestamp"":""$ts"",""event"":""anchor_blocked"",""file"":""$fpEsc"",""tool"":""Write""}"
                    exit 2
                }
            }
            # Backup existing file before modification.
            if (-not (Test-Path $BackupDir)) {
                New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
            }
            $stamp = (Get-Date).ToString("yyyyMMdd_HHmmss")
            $encoded = ($filePath -replace '[/\\]', '__') -replace ' ', '_'
            try {
                Copy-Item -LiteralPath $filePath -Destination (Join-Path $BackupDir "${stamp}__${encoded}") -Force -ErrorAction Stop
            } catch { }
            # Cleanup backups older than 24h.
            try {
                $cutoff = (Get-Date).AddMinutes(-1440)
                Get-ChildItem -Path $BackupDir -File -ErrorAction SilentlyContinue |
                    Where-Object { $_.LastWriteTime -lt $cutoff } |
                    Remove-Item -Force -ErrorAction SilentlyContinue
            } catch { }
        }
        try { Add-Content -Path $SessionReadsFile -Value $filePath -ErrorAction Stop } catch { }
    }
}

# === 5. KG SEARCH SUGGESTION -- RETIRED (v0.2.101 wave-3, review nit-6) ====
# This branch emitted a "Found N related patterns" KG suggestion on every
# Edit/Write -- the SAME tool calls pre-edit-context-inject.ps1 /
# pre-write-context-inject.ps1 inject gated, deduped, budgeted KG + code-graph
# context for: two context emissions per edit, no score floor on the
# suggestion path. The injection redesign made those wrappers (via
# hook_context_router.py) the ONE home for edit-time KG context (PLAN-V02101
# section 2.1 Edit/Write rows). Retiring this branch also retired the last
# shell caller of _lib/query-cache.ps1, deleted with it (the router keeps its
# own Python cache under the disjoint kgi/cgi namespaces). The
# pre_tool_use_kg_search task type stays registered in rl_kg_search's
# KNOWN_TASK_TYPES for the historical RL corpus.
# MUST MATCH pre-tool-use.sh's retirement tombstone.

# v0.2.100 WP-17 (kept from the retired section-5 tool gate): the Bash
# branch's queued broken-install notice leaves in this tool call's one
# envelope.
if ($script:VcoModelNotice -and (Get-Command Emit-AdditionalContext -ErrorAction SilentlyContinue)) {
    Emit-AdditionalContext $script:VcoModelNotice 'PreToolUse'
}
exit 0
