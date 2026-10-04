# lean-ctx-tee.ps1 - LOSSLESS compression wrapper for allow-listed commands.
# Windows sibling of templates/hooks/_lib/lean-ctx-tee.sh - see that file
# for the full flow rationale (v0.2.101 allow-list + tee/pointer design).
#
# Invoked as the rewritten Bash-tool command built by
# .claude/hooks/lean-ctx-rewrite.ps1:
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File lean-ctx-tee.ps1 `
#       <lean-ctx-bin> <raw-dir> <cmd-file> <ttl-hours>
#
# Flow (MUST MATCH the .sh sibling):
#   1. read the original command text from <cmd-file> (written verbatim
#      by the hook - no re-quoting of user text at any hop),
#   2. TTL-sweep *.log / *.cmd older than <ttl-hours> (0 = keep forever;
#      invalid = 168),
#   3. run the command in a CHILD PowerShell (N-1: an in-process
#      ScriptBlock would let a command-text `exit N` kill the wrapper
#      before the tee write; the .sh sibling's `bash -c` child never had
#      that hole), tee the FULL raw output to
#      <raw-dir>/<utc-ts>-<pid>-<sha8>.log (born 0600 on POSIX hosts, dir
#      0700 - SF-1), preserve the exit code,
#   4. print the output compressed via `lean-ctx -c cat` fed on stdin
#      (no dependency on lean-ctx's own tee_mode, which varies by
#      version); fall back to printing the raw file when compression
#      fails - output is NEVER lost,
#   5. end with exactly one pointer line,
#   6. exit with the ORIGINAL command's exit code.
#
# Conservative arms: raw-dir not creatable -> run the command directly,
# uncompressed, no pointer; cmd-file unreadable -> loud stderr + exit 2;
# raw output > 32 MiB -> pointer only; empty output -> pointer only.
#
# Execution-time helper, not a hook (no VCT_DISABLE_HOOKS gate here - the
# hook upstream is the gate). MUST MATCH lean-ctx-tee.sh; behavioural
# parity locked by tests/test_v02101_lean_ctx_allowlist_tee.py.

param(
    [string]$LcBin = "",
    [string]$RawDir = "",
    [string]$CmdFile = "",
    [string]$TtlHours = "168"
)

# Native-command stdin/stdout fidelity (PS 5.1 re-encodes pipes otherwise).
try {
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch { }

if (-not $LcBin -or -not $RawDir -or -not $CmdFile) {
    [Console]::Error.WriteLine("lean-ctx-tee: usage: lean-ctx-tee.ps1 <lean-ctx-bin> <raw-dir> <cmd-file> <ttl-hours>")
    exit 2
}
if (-not (Test-Path -LiteralPath $CmdFile -PathType Leaf)) {
    [Console]::Error.WriteLine("lean-ctx-tee: cannot read command file: $CmdFile")
    exit 2
}
$CMD = ""
try { $CMD = [System.IO.File]::ReadAllText($CmdFile) } catch {
    [Console]::Error.WriteLine("lean-ctx-tee: cannot read command file: $CmdFile")
    exit 2
}
if ($TtlHours -notmatch '^[0-9]+$') { $TtlHours = "168" }
$ttl = [int]$TtlHours

# SF-1 (v0.2.101 review): raw OUTPUT of allow-listed curl/wget/test runs can
# carry credentials (SEC-RAW guards the command text, not the output). On
# POSIX hosts (pwsh on Linux/macOS) tighten the tee dir to 0700 and files to
# 0600 via chmod; on native Windows there is no POSIX mode and the user
# profile ACLs are the equivalent protection (files under the user's own
# tree are not world-readable by default) - documented divergence, same
# threat outcome. Git never sees the dir (bundle add/update writes /.claude/
# to .git/info/exclude) and the code graph classifies .claude/state/ as
# transient (TRANSIENT_STATE_MARKER).
function Set-PosixMode([string]$Path, [string]$Mode) {
    if (Get-Command chmod -ErrorAction SilentlyContinue) {
        & chmod $Mode $Path 2>$null
    }
}

# N-1 (v0.2.101 review): run the command in a CHILD process, not an
# in-process ScriptBlock - a command text of the form `exit N` would exit
# the WHOLE wrapper before the tee write (the .sh sibling's `bash -c` child
# never had that hole). The trailing guard propagates a native command's
# non-zero exit code through the child (verified: `bash -c "exit 4"` -> 4).
$childExe = $null
try { $childExe = (Get-Process -Id $PID).Path } catch { }
if (-not $childExe) { $childExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source }
if (-not $childExe) { $childExe = (Get-Command powershell -ErrorAction SilentlyContinue).Source }
$childSrc = $CMD + "`nif (`$LASTEXITCODE -ne `$null -and `$LASTEXITCODE -ne 0) { exit `$LASTEXITCODE }"

function Invoke-TeeCommand {
    $global:LASTEXITCODE = 0
    if ($script:childExe) {
        # -EncodedCommand, NOT -Command <string>: PS 5.1's legacy native
        # argument passing can mangle embedded quotes in a re-parsed command
        # string (the Start-Process/-Command failure class recorded in the
        # KG). Base64 UTF-16LE is immune on both powershell.exe and pwsh.
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script:childSrc))
        return @(& $script:childExe -NoProfile -NonInteractive -EncodedCommand $enc 2>&1)
    }
    # Last-resort fallback (no resolvable PowerShell executable - not
    # expected on any supported host): in-process ScriptBlock.
    try { return @(& ([scriptblock]::Create($CMD)) 2>&1) } catch { return @("$_") }
}

if (-not (Test-Path -LiteralPath $RawDir)) {
    try { New-Item -ItemType Directory -Path $RawDir -Force -ErrorAction Stop | Out-Null } catch { }
}
if (Test-Path -LiteralPath $RawDir) { Set-PosixMode $RawDir "700" }
if (-not (Test-Path -LiteralPath $RawDir)) {
    # No state dir -> no tee -> no compression. Run the command as-is and
    # let its output flow through untouched (MUST MATCH the .sh `exec bash
    # -c "$CMD"` arm - never assign to $null here, that would swallow the
    # output and lose it).
    Invoke-TeeCommand | ForEach-Object { Write-Output "$_" }
    $ec = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    exit $ec
}

# TTL sweep (stale tee files + orphaned cmd files from denied rewrites).
if ($ttl -gt 0) {
    try {
        $cutoff = (Get-Date).AddHours(-$ttl)
        Get-ChildItem -LiteralPath $RawDir -File -ErrorAction SilentlyContinue |
            Where-Object { ($_.Extension -eq ".log" -or $_.Extension -eq ".cmd") -and $_.LastWriteTime -lt $cutoff } |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } catch { }
}

$ts = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$ck = "00000000"
try {
    $sha = [System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($CMD))
    $ck = ([System.BitConverter]::ToString($sha) -replace '-', '').Substring(0, 8).ToLowerInvariant()
} catch { }
$raw = Join-Path $RawDir ("{0}-{1}-{2}.log" -f $ts, $PID, $ck)

# SF-1 + NF-4 (re-review): the tee file is BORN 0600 - on POSIX hosts via
# umask 077 in a child sh (MUST MATCH the .sh sibling's
# `( umask 077; : >"$RAW" )`, and independent of a working chmod);
# elsewhere via .NET (native Windows: profile ACLs are the equivalent).
# If the PRIVATE tee cannot be created there is no tee at all: the command
# runs as-is, uncompressed, output passing through (lossless by not
# compressing) - the same arm as the .sh pre-create failure.
$born = $false
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    if (Get-Command sh -ErrorAction SilentlyContinue) {
        $global:LASTEXITCODE = 0
        & sh -c 'umask 077; : > "$1"' sh $raw 2>$null
        $born = ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $raw))
    }
} else {
    try { [System.IO.File]::WriteAllText($raw, ""); $born = $true } catch { $born = $false }
}
if (-not $born) {
    Invoke-TeeCommand | ForEach-Object { Write-Output "$_" }
    $ec = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    exit $ec
}

$items = Invoke-TeeCommand
$ec = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
$text = ($items | ForEach-Object { "$_" }) -join "`n"
if ($text.Length -gt 0) { $text += "`n" }
try { [System.IO.File]::WriteAllText($raw, $text) } catch {
    # Tee write failed after the run: print what we captured, no pointer.
    if ($text.Length -gt 0) { Write-Output $text.TrimEnd("`n") }
    exit $ec
}

# N-2: the 32 MiB gate counts BYTES (parity with the .sh `wc -c`), not .NET
# string chars. Line/byte COUNTS stay a convention: the pipeline capture
# newline-normalizes the buffer, so counts can differ by one from the .sh
# byte-exact tee for output lacking a trailing newline (cosmetic; the FILE
# holds every line, and the pointer's promise of "full output at <path>" is
# exact either way).
$rawBytes = [System.Text.Encoding]::UTF8.GetByteCount($text)
$rawLines = ([regex]::Matches($text, "`n")).Count

if ($rawBytes -eq 0) {
    Write-Output ("[lean-ctx-tee] 0 raw lines -> 0 shown; full output: {0} (kept {1}h)" -f $raw, $TtlHours)
    exit $ec
}
if ($rawBytes -gt 33554432) {
    Write-Output ("[lean-ctx-tee] output too large to echo ({0} bytes); full output: {1} (kept {2}h) - grep/tail that file, never re-run" -f $rawBytes, $raw, $TtlHours)
    exit $ec
}

$comp = $null
$compOk = $false
try {
    $comp = @($text | & $LcBin -c cat 2>$null)
    $compOk = ($null -eq $LASTEXITCODE -or $LASTEXITCODE -eq 0)
} catch { $compOk = $false }
if ($compOk -and $comp -and $comp.Count -gt 0) {
    $comp | ForEach-Object { Write-Output "$_" }
    $shown = $comp.Count
} else {
    # Compressor failed - print the raw output verbatim (never lose it).
    Get-Content -LiteralPath $raw | ForEach-Object { Write-Output $_ }
    $shown = $rawLines
}
Write-Output ("[lean-ctx-tee] {0} raw lines -> {1} shown; full output: {2} (kept {3}h)" -f $rawLines, $shown, $raw, $TtlHours)
exit $ec
