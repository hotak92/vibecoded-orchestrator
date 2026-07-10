# vct_secrets_resolve.ps1 — agent-facing secret resolver. PowerShell
# counterpart of `vct_secrets_resolve.sh`.
#
# ONE RESOLUTION CHAIN (v0.2.73 unification — MUST MATCH the other two
# implementations: `templates/scripts/vct_secrets_resolve.sh` and
# `vco_lib/agent_secrets.py::get`; keep tier order, fall-through rules,
# and the tier-3 parsing rule identical across all three):
#
#   Tier 1  vct-hub  GET /api/v1/projects/{id}/env?key=NAME
#           (OS-keychain values, per-(secret x requester) active-flag
#           gated -- the launcher's permission matrix).
#   Tier 2  file store  $Env:VCT_SECRETS_DIR (default ~\.vct-secrets):
#           projects\<NAME>\<key>  ->  shared\<key>.
#   Tier 3  the project's own `.env` -- READ-ONLY, lowest priority.
#           Only consulted when the first arg is a FOLDER (a bare
#           project id gives no folder to read; tier 3 is skipped and
#           the miss diagnostic says so).
#
# Fall-through: tier 1 -> 2 on hub unreachable / project not registered /
# 401 / key_not_active; tier 2 -> 3 on file absent/unreadable. All-miss
# -> non-zero exit preserving the tier-1 exit-code contract below (exit
# 3 `key_not_active` only after tiers 2 and 3 also missed). Errors name
# the KEY and the tiers consulted -- NEVER the value.
#
# Tier-3 parsing rule (identical x3): line-oriented; accept `KEY=VALUE`
# and `export KEY=VALUE`; strip one matching pair of single/double
# quotes; NO variable expansion, NO command substitution; first match
# wins. The value is never logged, cached to disk, or re-exported into
# any VCO-written file.
#
# Usage:
#   .\vct_secrets_resolve.ps1 <project_id_or_folder> <secret_key>
#       Print the value of <secret_key> on stdout. Exits with:
#         0  success
#         1  hub unreachable
#         2  project not registered
#         3  key not active for this project
#         4  key not found in hub response
#       Non-zero codes mean the key ALSO missed the file store and the
#       project `.env`.
#
#   .\vct_secrets_resolve.ps1 resolve-project <folder>
#       Print the project_id for <folder>. Same exit codes (0/1/2).
#
# Hub discovery:
#   1. $Env:VCT_HUB_PORT
#   2. ${VCT_STATE_DIR or $HOME\.vct}\hub.port
#   3. Default 7700.
#
# Auth (H5, 2026-05-08):
#   Every request carries `Authorization: Bearer <token>` where
#   <token> is read from:
#     1. $Env:VCT_HUB_TOKEN (tests / dev harnesses)
#     2. ${VCT_STATE_DIR or $HOME\.vct}\hub.token (written by the
#        launcher on startup). Mirrors the .sh helper.
#   If the file is missing → exit 1 ("hub unreachable"). If the hub
#   returns 401 (token rotated by a launcher restart) → also exit 1.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Arg1,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Arg2
)

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# Mirrors `templates/scripts/vct_secrets_resolve.sh` — kept in lockstep
# by the template-drift gate. Both copies (templates/ + .claude/) are
# byte-identical; the hub owns the project-root lookup.
# VCO-REWIRE-END: orchestrator-root-resolution

function Write-Err {
    param([string]$Message)
    [Console]::Error.WriteLine("[vct-secrets-resolve] $Message")
}

function Get-HubPort {
    if ($Env:VCT_HUB_PORT) {
        return [int]$Env:VCT_HUB_PORT
    }
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME ".vct" }
    $portFile = Join-Path $stateDir "hub.port"
    if (Test-Path $portFile) {
        $raw = (Get-Content -Raw -Path $portFile).Trim()
        if ($raw -match '^\d+$') {
            return [int]$raw
        }
    }
    return 7700
}

function Get-HubToken {
    # Returns the auth token string, or $null if no token can be
    # resolved. Caller treats $null as "hub unreachable" (the launcher
    # hasn't written hub.token yet — it isn't running).
    #
    # v0.2.76 Part 4 — PER-PROJECT TOKEN PREFERENCE (MUST MATCH the bash
    # sibling `vct_secrets_resolve.sh::hub_token`,
    # `vct_project_config.ps1::Get-HubToken`, and
    # `vco_lib/project_config.py::_project_token`): accepts an OPTIONAL
    # -ProjectId. When given AND a readable `hub.token.<ProjectId>`
    # exists, that scoped token is returned in preference to the global
    # `hub.token`. `VCT_HUB_TOKEN` still wins over both. Falls back to the
    # global token when no per-project file exists (compat window). The
    # `by-path` lookup passes no id → global token; only the `/env` call
    # passes the resolved id.
    param([string]$ProjectId = '')
    if ($Env:VCT_HUB_TOKEN) {
        $t = $Env:VCT_HUB_TOKEN.Trim()
        if ($t.Length -gt 0) { return $t }
    }
    $stateDir = if ($Env:VCT_STATE_DIR) { $Env:VCT_STATE_DIR } else { Join-Path $HOME ".vct" }
    if ($ProjectId) {
        $projFile = Join-Path $stateDir "hub.token.$ProjectId"
        if (Test-Path $projFile) {
            try {
                $rawP = (Get-Content -Raw -Path $projFile -ErrorAction Stop).Trim()
                if ($rawP.Length -gt 0) { return $rawP }
            } catch {
                # Unreadable per-project file → fall through to global.
            }
        }
    }
    $tokenFile = Join-Path $stateDir "hub.token"
    if (Test-Path $tokenFile) {
        $raw = (Get-Content -Raw -Path $tokenFile).Trim()
        if ($raw.Length -gt 0) { return $raw }
    }
    return $null
}

function Invoke-Hub {
    # v0.2.76 Part 4: optional -ProjectId for a per-project route
    # (`/env`). When set, Get-HubToken prefers the scoped
    # `hub.token.<id>`. The `by-path` lookup passes no id → global token.
    param([string]$PathAndQuery, [string]$ProjectId = '')
    $port = Get-HubPort
    $token = Get-HubToken -ProjectId $ProjectId
    if ($null -eq $token) {
        # Sentinel result the caller can detect alongside the
        # connection-refused $null. We use a distinct shape (Status=0)
        # so error-message wiring can give a more useful message
        # ("token missing" vs "connection refused").
        return @{ Status = 0; Body = "hub.token missing" }
    }
    $url = "http://127.0.0.1:$port/api/v1/$PathAndQuery"
    $headers = @{ "Authorization" = "Bearer $token" }
    try {
        $response = Invoke-WebRequest -Uri $url -Method Get -UseBasicParsing `
            -Headers $headers -TimeoutSec 5 -ErrorAction Stop
        return @{
            Status = [int]$response.StatusCode
            Body   = $response.Content
        }
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($resp -ne $null) {
            $stream = $resp.GetResponseStream()
            $reader = New-Object System.IO.StreamReader($stream)
            $body = $reader.ReadToEnd()
            return @{
                Status = [int]$resp.StatusCode
                Body   = $body
            }
        }
        return $null  # connection refused / DNS / etc.
    } catch {
        # Newer .NET wraps the same in System.Net.Http.HttpRequestException.
        if ($_.Exception.Response) {
            $resp = $_.Exception.Response
            return @{
                Status = [int]$resp.StatusCode
                Body   = ($resp.Content.ReadAsStringAsync().Result)
            }
        }
        return $null
    }
}

function Test-LooksLikePath {
    param([string]$Value)
    # NOTE: the Windows-drive clause MUST be parenthesized. PowerShell's `-and`
    # binds tighter than `-or`, but a bare trailing `... -or A -ge 2 -and B`
    # across a line-continuation still mis-evaluates the whole expression to
    # $false even when an earlier `-or` term is $true (verified live 2026-07-04:
    # a "/tmp/…"-prefixed folder returned $false, so tier-3 `.env` resolution was
    # silently skipped). Wrap the `-and` sub-clause so the `-or` chain is honored.
    return ($Value.StartsWith("/") -or $Value.StartsWith("./") -or
            $Value.StartsWith("../") -or $Value.Contains("/") -or
            $Value.Contains("\") -or ($Value.Length -ge 2 -and $Value.Substring(1, 1) -eq ":"))
}

function Resolve-ProjectId {
    param([string]$ArgValue)
    if (-not (Test-LooksLikePath $ArgValue)) {
        return @{ ExitCode = 0; Value = $ArgValue }
    }
    $encoded = [System.Uri]::EscapeDataString($ArgValue)
    $result = Invoke-Hub "projects/by-path?path=$encoded"
    if ($null -eq $result) {
        Write-Err "hub unreachable; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
        return @{ ExitCode = 1 }
    }
    switch ($result.Status) {
        0 {
            # Status=0 is our internal sentinel from Get-HubToken
            # returning $null. Surface as exit 1 with the token-missing
            # diagnostic — same family as "hub unreachable" because
            # both mean "launcher isn't fully up".
            Write-Err "hub.token missing; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
            return @{ ExitCode = 1 }
        }
        401 {
            Write-Err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again. If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
            return @{ ExitCode = 1 }
        }
        200 {
            try {
                $obj = $result.Body | ConvertFrom-Json
            } catch {
                Write-Err "hub returned 200 but body is not JSON; body=$($result.Body)"
                return @{ ExitCode = 2 }
            }
            if (-not $obj.id) {
                Write-Err "hub returned 200 but no .id field; body=$($result.Body)"
                return @{ ExitCode = 2 }
            }
            return @{ ExitCode = 0; Value = $obj.id }
        }
        404 {
            Write-Err "no project registered at path: $ArgValue"
            return @{ ExitCode = 2 }
        }
        400 {
            Write-Err "hub rejected path query: $($result.Body)"
            return @{ ExitCode = 2 }
        }
        Default {
            Write-Err "hub returned status $($result.Status) for by-path lookup; body=$($result.Body)"
            return @{ ExitCode = 1 }
        }
    }
}

function Read-KeyHub {
    # Tier 1: hub (keychain). Returns @{ ExitCode; Value } — exit codes
    # preserve the historical contract (1=hub unreachable, 2=project
    # not registered, 3=key_not_active, 4=key missing from a 200 body).
    # The chain caller remembers this code and reuses it as the final
    # exit when tiers 2 and 3 also miss.
    param([string]$ProjectArg, [string]$Key)
    $resolved = Resolve-ProjectId -ArgValue $ProjectArg
    if ($resolved.ExitCode -ne 0) {
        return @{ ExitCode = $resolved.ExitCode }
    }
    $pid_ = $resolved.Value
    $encodedPid = [System.Uri]::EscapeDataString($pid_)
    $encodedKey = [System.Uri]::EscapeDataString($Key)
    # Per-project route → pass the resolved project id so Get-HubToken
    # prefers the scoped `hub.token.<id>` (v0.2.76 Part 4).
    $result = Invoke-Hub -PathAndQuery "projects/$encodedPid/env?key=$encodedKey" -ProjectId $pid_
    if ($null -eq $result) {
        Write-Err "hub unreachable; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
        return @{ ExitCode = 1 }
    }
    switch ($result.Status) {
        0 {
            Write-Err "hub.token missing; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
            return @{ ExitCode = 1 }
        }
        401 {
            Write-Err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again. If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
            return @{ ExitCode = 1 }
        }
        200 {
            try {
                $obj = $result.Body | ConvertFrom-Json
            } catch {
                Write-Err "hub returned 200 but body is not JSON; body=$($result.Body)"
                return @{ ExitCode = 4 }
            }
            $val = $obj.PSObject.Properties[$Key]
            if ($null -eq $val) {
                Write-Err "hub returned 200 but no $Key field; body=$($result.Body)"
                return @{ ExitCode = 4 }
            }
            return @{ ExitCode = 0; Value = $val.Value }
        }
        404 {
            try {
                $obj = $result.Body | ConvertFrom-Json
                $code = $obj.error.code
            } catch { $code = "unknown" }
            switch ($code) {
                "project_not_found" {
                    Write-Err "project $pid_ not found in launcher.db"
                    return @{ ExitCode = 2 }
                }
                "key_not_active" {
                    Write-Err "key $Key not active for project $pid_ (paused for this project, or not declared by any installed module)"
                    return @{ ExitCode = 3 }
                }
                Default {
                    Write-Err "hub 404 with unknown code $code; body=$($result.Body)"
                    return @{ ExitCode = 4 }
                }
            }
        }
        400 {
            Write-Err "hub rejected request: $($result.Body)"
            return @{ ExitCode = 4 }
        }
        Default {
            Write-Err "hub returned status $($result.Status); body=$($result.Body)"
            return @{ ExitCode = 1 }
        }
    }
}

# ── Tier 2: file store ($Env:VCT_SECRETS_DIR, default ~\.vct-secrets) ───
#
# Mirrors `vco_lib/agent_secrets.py::_file_store_get` and the .sh
# `file_store_get` (must match): projects\<NAME>\<key> first (when a
# project NAME applies), then shared\<key>. Strips exactly ONE trailing
# newline (vct-exec semantics). A simple first arg (no path separators)
# is used verbatim as the file-store project name; a path walks up for
# a `.vct-project` marker file.
function Get-SecretsRoot {
    if ($Env:VCT_SECRETS_DIR -and $Env:VCT_SECRETS_DIR.Trim().Length -gt 0) {
        return $Env:VCT_SECRETS_DIR
    }
    return (Join-Path $HOME ".vct-secrets")
}

function Get-FileProjectName {
    param([string]$ArgValue)
    if ($ArgValue -and -not (Test-LooksLikePath $ArgValue)) {
        return $ArgValue
    }
    try {
        $cur = (Resolve-Path -LiteralPath $ArgValue -ErrorAction Stop).Path
    } catch {
        return $null
    }
    if (Test-Path -LiteralPath $cur -PathType Leaf) {
        $cur = Split-Path -Parent $cur
    }
    while ($cur) {
        $marker = Join-Path $cur ".vct-project"
        if (Test-Path -LiteralPath $marker -PathType Leaf) {
            try {
                $line = (Get-Content -LiteralPath $marker -TotalCount 1 -ErrorAction Stop)
            } catch {
                return $null
            }
            if ($null -ne $line) {
                $name = ($line -replace '\s', '')
                if ($name.Length -gt 0) { return $name }
            }
            return $null
        }
        $parent = Split-Path -Parent $cur
        if ($parent -eq $cur) { break }
        $cur = $parent
    }
    return $null
}

function Read-FileStripOneNewline {
    param([string]$FilePath)
    try {
        $raw = Get-Content -Raw -LiteralPath $FilePath -ErrorAction Stop
    } catch {
        return $null
    }
    if ($null -eq $raw) { return "" }
    if ($raw.EndsWith("`n")) {
        $raw = $raw.Substring(0, $raw.Length - 1)
    }
    return $raw
}

function Get-FileStoreValue {
    # Returns @{ Found = $true/$false; Value = ... }
    param([string]$ProjectArg, [string]$Key)
    $root = Get-SecretsRoot
    $name = Get-FileProjectName -ArgValue $ProjectArg
    if ($name) {
        $f = Join-Path (Join-Path (Join-Path $root "projects") $name) $Key
        if (Test-Path -LiteralPath $f -PathType Leaf) {
            $v = Read-FileStripOneNewline -FilePath $f
            if ($null -ne $v) { return @{ Found = $true; Value = $v } }
        }
    }
    $f = Join-Path (Join-Path $root "shared") $Key
    if (Test-Path -LiteralPath $f -PathType Leaf) {
        $v = Read-FileStripOneNewline -FilePath $f
        if ($null -ne $v) { return @{ Found = $true; Value = $v } }
    }
    return @{ Found = $false }
}

# ── Tier 3: the project's own .env (READ-ONLY, lowest priority) ─────────
#
# Parsing rule (must match vct_secrets_resolve.sh::dotenv_get and
# agent_secrets.py::_parse_dotenv_value): line-oriented; accept
# `KEY=VALUE` and `export KEY=VALUE`; strip one matching pair of
# single/double quotes; NO variable expansion, NO command substitution;
# first match wins. Never writes, never caches, never re-exports.
function Get-DotenvValue {
    # Returns @{ Found = $true/$false; Value = ... }
    param([string]$Folder, [string]$Key)
    $f = Join-Path $Folder ".env"
    if (-not (Test-Path -LiteralPath $f -PathType Leaf)) {
        return @{ Found = $false }
    }
    try {
        $lines = Get-Content -LiteralPath $f -ErrorAction Stop
    } catch {
        return @{ Found = $false }
    }
    foreach ($line in @($lines)) {
        $s = "$line".Trim()
        if ($s.StartsWith("export ")) {
            $s = $s.Substring(7).TrimStart()
        }
        if ($s.Length -eq 0 -or $s.StartsWith("#")) { continue }
        $eq = $s.IndexOf("=")
        if ($eq -lt 0) { continue }
        $k = $s.Substring(0, $eq).TrimEnd()
        if ($k -cne $Key) { continue }
        $v = $s.Substring($eq + 1).Trim()
        if ($v.Length -ge 2) {
            $first = $v[0]
            $last = $v[$v.Length - 1]
            if (($first -eq $last) -and (($first -eq '"') -or ($first -eq "'"))) {
                $v = $v.Substring(1, $v.Length - 2)
            }
        }
        return @{ Found = $true; Value = $v }
    }
    return @{ Found = $false }
}

function Read-Key {
    # Chain: hub (tier 1) -> file store (tier 2) -> project .env
    # (tier 3). The final exit code on all-miss is TIER 1's code,
    # preserving the historical contract (exit 3 = key_not_active only
    # after tiers 2 and 3 also missed).
    param([string]$ProjectArg, [string]$Key)
    $tier1 = Read-KeyHub -ProjectArg $ProjectArg -Key $Key
    if ($tier1.ExitCode -eq 0) {
        [Console]::Out.Write($tier1.Value)
        return 0
    }
    # Tier 2: file store.
    $tier2 = Get-FileStoreValue -ProjectArg $ProjectArg -Key $Key
    if ($tier2.Found) {
        [Console]::Out.Write($tier2.Value)
        return 0
    }
    # Tier 3: project .env — only when the first arg names a folder.
    if (Test-LooksLikePath $ProjectArg) {
        $envDir = $ProjectArg
        if (Test-Path -LiteralPath $envDir -PathType Leaf) {
            $envDir = Split-Path -Parent $envDir
        }
        $tier3 = Get-DotenvValue -Folder $envDir -Key $Key
        if ($tier3.Found) {
            [Console]::Out.Write($tier3.Value)
            return 0
        }
    } else {
        Write-Err "tier 3 (.env) skipped: first arg is a project id, not a folder — re-invoke with the project folder to consult its .env"
    }
    Write-Err "key $Key unresolved after hub (tier 1), file store (tier 2), and project .env (tier 3)"
    return $tier1.ExitCode
}

# ── Main ────────────────────────────────────────────────────────────────
$ErrorActionPreference = "Stop"

if ($Arg1 -eq "resolve-project") {
    $resolved = Resolve-ProjectId -ArgValue $Arg2
    if ($resolved.ExitCode -ne 0) {
        exit $resolved.ExitCode
    }
    [Console]::Out.Write($resolved.Value)
    exit 0
}

$rc = Read-Key -ProjectArg $Arg1 -Key $Arg2
exit $rc
