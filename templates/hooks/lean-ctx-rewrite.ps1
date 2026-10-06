# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Per-project lean-ctx PreToolUse hook for Bash tool calls (Windows).
# Windows sibling of templates/hooks/lean-ctx-rewrite.sh — see that file
# for the full rationale (fork-bomb avoidance, v0.2.101 allow-list
# inversion, lossless tee/pointer design, bypass hierarchy).
#
# CONTRACT (v0.2.101 — ALLOW-LIST INVERSION + LOSSLESS TEE)
# ----------------------------------------------------------
# Claude Code pipes a JSON PreToolUse payload to stdin. The hook
# compresses ONLY commands on the one committed allow-list at
# <hook-dir>/_lib/lean-ctx-allowlist.txt (the SAME file the .sh sibling
# parses — shared config, A>B>C tier B). Everything else runs RAW: loops,
# pipes, chains, redirects, command substitution, git (never allow-listed
# — owner rule), unknown commands, SEC-RAW credential hits.
#
# For an allow-listed command the hook writes the command text VERBATIM to
# <project>/.claude/state/lean-ctx-tee/<ts>-<pid>-<sha8>.cmd and emits
# `hookSpecificOutput.updatedInput.command` =
#   powershell -NoProfile -ExecutionPolicy Bypass -File '<hook-dir>/_lib/lean-ctx-tee.ps1' '<lean-ctx-bin>' '<raw-dir>' '<cmd-file>' '<ttl>'
# The wrapper tees the FULL raw output to a .log (TTL-swept; default
# 168 h, knob VCO_LEAN_CTX_TEE_TTL_HOURS in .claude/env, 0 = keep
# forever), prints the compressed output, ends with ONE pointer line
# naming the raw file, and exits with the command's own exit code.
#
# The response is CONSTRUCTED BY THIS HOOK — every field is ours, so
# `permissionDecision` can never appear (the D-3 auto-approval footgun is
# structurally impossible, not filtered). All other tool_input fields are
# preserved in updatedInput. Empty stdout = no rewrite = raw output.
#
# RETIRED (v0.2.101) — the allow-list subsumes them: TRIM-b git
# commit/push step-aside, TRIM-r read-only git verbs, delegation of the
# wrap decision to lean-ctx's own rewrite handler. KEPT: SEC-RAW
# (allow-listed installers/downloaders can carry credentials).
#
# GUARD ORDER (intentional, mirrors .sh)
# --------------------------------------
# 1. VCT_DISABLE_HOOKS — global sledgehammer, first.
# 2. .claude/env → VCO_LEAN_CTX_DEFAULT / VCO_LEAN_CTX_TEE_TTL_HOURS.
# 3. lean-ctx availability — graceful no-op when missing.
# 4. Allow-list decision + cmd-file write + response construction; every
#    failure arm emits NOTHING (raw command under the normal permission
#    flow). MUST MATCH lean-ctx-rewrite.sh (behavioural parity locked by
#    tests/test_v02101_lean_ctx_allowlist_tee.py).

# Scrub sensitive env vars before any subprocess spawning (defense-in-depth
# parity with every other VCO hook + .sh sibling). The hook itself doesn't
# read secrets, but spawned children inherit our env; scrubbing first means
# they can't leak a credential via their own logs / debug output.
foreach ($k in @('SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY')) {
    if (Test-Path "Env:$k") { Remove-Item -LiteralPath "Env:$k" -ErrorAction SilentlyContinue }
}

# 1. Global kill-switch (one switch for "turn off all VCT hook side-effects").
if ($env:VCT_DISABLE_HOOKS) { exit 0 }

# 2. Per-project knobs. Read .claude/env if it exists; look for the
#    VCO_LEAN_CTX_DEFAULT / VCO_LEAN_CTX_TEE_TTL_HOURS lines (KEY=VALUE
#    syntax). Anything else in the file is ignored — this script doesn't
#    `Invoke-Expression` user env files (avoids arbitrary code-exec from a
#    malformed .claude/env).
# One-quote-pair rule for the raw .claude/env scan -- SHARED home
# templates/hooks/_lib/strip-one-quote-pair.ps1 (post-tool-use-async.ps1 dots
# the SAME helper; the rule's rationale, its consumers and its PS1_ONLY_LIB
# parity declaration live there). The .sh sibling needs no helper: it SOURCES
# the file and the shell already removes one quote pair. Guarded dot-source so
# a partial install cannot break the hook; the calls below tolerate an absent
# function, and a syntax error in an EXISTING helper is deliberately NOT
# swallowed (a real bug -- same stance as pre-edit-context-inject.ps1).
$QuotePairLib = Join-Path $PSScriptRoot "_lib/strip-one-quote-pair.ps1"
if (Test-Path -LiteralPath $QuotePairLib -PathType Leaf) { . $QuotePairLib }

$envFile = Join-Path (Get-Location) ".claude/env"
$leanCtxDefault = "on"
$leanCtxTtl = "168"
if (Test-Path -LiteralPath $envFile) {
    try {
        foreach ($line in Get-Content -LiteralPath $envFile -ErrorAction Stop) {
            if ($line -match '^\s*VCO_LEAN_CTX_DEFAULT\s*=\s*(.+?)\s*$') {
                $leanCtxDefault = $Matches[1]
                if (Get-Command Strip-OneQuotePair -ErrorAction SilentlyContinue) {
                    $leanCtxDefault = Strip-OneQuotePair $leanCtxDefault
                }
                $leanCtxDefault = $leanCtxDefault.ToLowerInvariant()
            }
            if ($line -match '^\s*VCO_LEAN_CTX_TEE_TTL_HOURS\s*=\s*(.+?)\s*$') {
                $leanCtxTtl = $Matches[1]
                if (Get-Command Strip-OneQuotePair -ErrorAction SilentlyContinue) {
                    $leanCtxTtl = Strip-OneQuotePair $leanCtxTtl
                }
            }
        }
    } catch {
        # Read failure — fall through with the defaults. Same shape as the
        # .sh sibling's `[ -f .claude/env ] && . .claude/env` no-op when the
        # file is unreadable.
    }
}
if ($leanCtxDefault -eq "off") { exit 0 }

# Capture the PreToolUse stdin payload ONCE (reading [Console]::In drains
# it). MUST MATCH lean-ctx-rewrite.sh ($_lc_cmd).
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
if (-not $HookStdin) { exit 0 }

# 3. lean-ctx availability — optional dep, never break Bash for users
#    without it. D-11 (v0.2.75): probe the same candidate list install.py
#    uses so a `cargo install`ed binary at ~/.cargo/bin (off the hook
#    shell's PATH) still activates compression. MUST MATCH the CANONICAL
#    candidate order in install.py::_find_lean_ctx_binary (Windows arm)
#    AND lean-ctx-rewrite.sh (parity-pinned by
#    tests/test_v0295_wp7_bootstrap_cascade_parity.py).
$LeanCtxBin = $null
if (Get-Command lean-ctx -ErrorAction SilentlyContinue) {
    $LeanCtxBin = "lean-ctx"
} else {
    $home = if ($env:USERPROFILE) { $env:USERPROFILE } elseif ($env:HOME) { $env:HOME } else { "" }
    $cands = @()
    if ($home) {
        $cands += (Join-Path $home ".cargo/bin/lean-ctx.exe")
        $cands += (Join-Path $home "scoop/shims/lean-ctx.exe")
        $cands += (Join-Path $home "scoop/apps/lean-ctx/current/lean-ctx.exe")
    }
    if ($env:ProgramData) { $cands += (Join-Path $env:ProgramData "chocolatey/bin/lean-ctx.exe") }
    if ($env:ProgramFiles) { $cands += (Join-Path $env:ProgramFiles "lean-ctx/lean-ctx.exe") }
    # Cross-OS: a POSIX host running the .ps1 under pwsh resolves the same
    # extensionless candidates the .sh probes.
    if ($home) {
        $cands += (Join-Path $home ".cargo/bin/lean-ctx")
        $cands += (Join-Path $home ".local/bin/lean-ctx")
    }
    $cands += "/usr/local/bin/lean-ctx"
    $cands += "/usr/bin/lean-ctx"
    $cands += "/opt/homebrew/bin/lean-ctx"
    $cands += "/home/linuxbrew/.linuxbrew/bin/lean-ctx"
    foreach ($c in $cands) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) { $LeanCtxBin = $c; break }
    }
}
if (-not $LeanCtxBin) { exit 0 }

# Sibling data + wrapper next to THIS hook (works installed and in the
# template checkout alike).
$alPath = Join-Path $PSScriptRoot "_lib/lean-ctx-allowlist.txt"
$teePath = Join-Path $PSScriptRoot "_lib/lean-ctx-tee.ps1"
if (-not (Test-Path -LiteralPath $alPath)) { exit 0 }
if (-not (Test-Path -LiteralPath $teePath)) { exit 0 }

# 4. Allow-list decision. Every failure arm exits 0 with NO stdout → the
#    original command runs raw under the normal permission flow.
try { $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop } catch { exit 0 }
$cmd = ""
if ($payload -and $payload.tool_input -and $payload.tool_input.command) {
    $cmd = [string]$payload.tool_input.command
}
if (-not $cmd.Trim()) { exit 0 }

# SEC-RAW (2026-07-21; kept through the v0.2.101 allow-list inversion):
# allow-listed commands CAN carry credentials — pip install --index-url
# https://user:pass@host/simple, curl -u / auth headers, wget --password,
# npm registry _authToken args, secret-shaped env prefixes. Any hit
# ANYWHERE in the command → raw. Pattern list between
# SEC-RAW-PATTERNS-BEGIN/END MUST MATCH lean-ctx-rewrite.sh
# (parity-pinned by
# tests/test_d11_trimb_lean_ctx_discovery_and_git_bypass.py and
# tests/test_v02101_lean_ctx_allowlist_tee.py).
# SEC-RAW-PATTERNS-BEGIN
$secretPatterns = @(
    '(?i)\bauthorization\s*:',
    '(?i)\b(x-api-key|private-token|x-auth-token|api-key)\s*:',
    '(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}',
    '(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}',
    '(?:^|\s)(?:-u|--user|--proxy-user)\s+[^\s:]+:\S',
    '--(?:password|http-password|api-key|token|access-token)[= ]',
    '\$\{?[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL)',
    '\b[A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL)[A-Za-z_]*=\S',
    '\bvct\s+(?:exec|get)\b',
    'vct_secrets_resolve',
    'agent_secrets',
    '\b(?:ATATT[A-Za-z0-9_=-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|ghs_[A-Za-z0-9]{8,}|glpat-[A-Za-z0-9_-]{8,}|xox[bpoas]-[A-Za-z0-9-]{8,}|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{20,})',
    '://[^\s/:@]+:[^\s/@]+@',
    '(?i)\b_?auth(?:token|_token)?\s*='
)
# SEC-RAW-PATTERNS-END
foreach ($p in $secretPatterns) {
    if ([regex]::IsMatch($cmd, $p)) { exit 0 }
}

# Shape gate: single simple commands only (loops, pipes, chains,
# redirects, subshells, command substitution → raw). MUST MATCH the .sh
# python gate ([char]96 is the backtick).
if ($cmd -match "[|&;<>()`n`r]" -or $cmd.Contains('$(') -or $cmd.IndexOf([char]96) -ge 0) { exit 0 }

$toks = @($cmd -split '\s+' | Where-Object { $_ -ne '' })
$i = 0
while ($i -lt $toks.Count -and $toks[$i] -match '^[A-Za-z_][A-Za-z0-9_]*=') { $i++ }
if ($i -ge $toks.Count) { exit 0 }
$rest = @($toks[$i..($toks.Count - 1)])
# basename the program token (MUST MATCH the .sh rsplit on / and \).
$rest[0] = @(($rest[0] -replace '\\', '/') -split '/')[-1]
if ($rest[0] -eq 'lean-ctx') { exit 0 }

$entries = @()
try {
    foreach ($line in Get-Content -LiteralPath $alPath -ErrorAction Stop) {
        $l = ($line -split '#')[0].Trim()
        if ($l) { $entries += , @($l -split '\s+') }
    }
} catch { exit 0 }
$matched = $false
foreach ($e in $entries) {
    if ($e.Count -le $rest.Count) {
        $ok = $true
        for ($j = 0; $j -lt $e.Count; $j++) {
            if ($rest[$j] -ne $e[$j]) { $ok = $false; break }
        }
        if ($ok) { $matched = $true; break }
    }
}
if (-not $matched) { exit 0 }

# Allow-listed → cmd file (verbatim) + wrapper rewrite. Any failure →
# exit 0 with no stdout → raw.
if ($leanCtxTtl -notmatch '^[0-9]+$') { $leanCtxTtl = "168" }
$proj = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$rawDir = Join-Path $proj ".claude/state/lean-ctx-tee"
try { New-Item -ItemType Directory -Path $rawDir -Force -ErrorAction Stop | Out-Null } catch { exit 0 }
# SF-1 (v0.2.101 review) + NF-4 (re-review): the tee dir holds raw command
# OUTPUT that can carry credentials. On POSIX hosts the dir must be
# PROVABLY 0700 or there is NO wrap at all — fail closed, MUST MATCH the
# .sh, where a failing os.chmod lands in the nothing() arm. On native
# Windows privacy is enforced at tee time by _lib/lean-ctx-tee.ps1 (S5): it
# applies an owner-only ACL to the tee dir and every tee file and runs the
# command UNCOMPRESSED if it cannot make them private — inherited ACLs are
# never trusted (a project outside the user profile inherits its parent's,
# commonly Users:Modify). The command TEXT written here is
# non-credential-bearing by construction (allow-list + SEC-RAW gate).
$isPosix = [System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT
if ($isPosix) {
    if (-not (Get-Command chmod -ErrorAction SilentlyContinue)) { exit 0 }
    $global:LASTEXITCODE = 0
    & chmod 700 $rawDir 2>$null
    if ($LASTEXITCODE -ne 0) { exit 0 }
}
$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$ck = "00000000"
try {
    $sha = [System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($cmd))
    $ck = ([System.BitConverter]::ToString($sha) -replace '-', '').Substring(0, 8).ToLowerInvariant()
} catch { }
$cmdFile = Join-Path $rawDir ("{0}-{1}-{2}.cmd" -f $ts, $PID, $ck)
# NF-4: on POSIX the cmd file is BORN 0600 — created by a child `sh` under
# umask 077 (the .sh sibling's O_CREAT-0600 equivalent), so the command
# text never exists at default permissions, not even briefly, and privacy
# never depends on an after-the-fact chmod succeeding. WriteAllText then
# truncates the EXISTING inode, preserving its mode.
try {
    if ($isPosix) {
        $global:LASTEXITCODE = 0
        & sh -c 'umask 077; : > "$1"' sh $cmdFile 2>$null
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $cmdFile)) { exit 0 }
    }
    [System.IO.File]::WriteAllText($cmdFile, $cmd)
} catch { exit 0 }

function Q([string]$s) { "'" + $s.Replace("'", "''") + "'" }
$wrapped = "powershell -NoProfile -ExecutionPolicy Bypass -File {0} {1} {2} {3} {4}" -f `
    (Q $teePath), (Q $LeanCtxBin), (Q $rawDir), (Q $cmdFile), (Q $leanCtxTtl)

$payload.tool_input | Add-Member -NotePropertyName command -NotePropertyValue $wrapped -Force
$outObj = [pscustomobject]@{
    hookSpecificOutput = [pscustomobject]@{
        hookEventName = "PreToolUse"
        updatedInput  = $payload.tool_input
    }
}
Write-Output ($outObj | ConvertTo-Json -Depth 8 -Compress)
exit 0
