# stderr-cap.ps1 — structural defense against runaway hook stderr.
#
# Sibling to _lib/stderr-cap.sh. Same purpose: cap any single hook's
# stderr at 1 MB so a buggy loop can never reproduce the 2026-05-07
# Claude Code GUI freeze (anthropics/claude-code#23053 / #51560).
#
# Usage (one line at the top of every .ps1 hook):
#   . "$PSScriptRoot/_lib/stderr-cap.ps1"
#
# Behaviour:
# - Wraps [Console]::Error with a counting TextWriter. Writes up to
#   $env:STDERR_CAP_BYTES (default 1048576 = 1 MB), drops the rest
#   silently. Subsequent writes return without raising — no analog of
#   SIGPIPE to manage.
# - PowerShell's Write-Error / Write-Warning / external-command stderr
#   all funnel through [Console]::Error, so they are all capped.
# - Bypass with $env:STDERR_CAP_DISABLE = "1".
#
# Why a TextWriter wrapper rather than process redirection: PowerShell
# 5.1 has no clean equivalent of bash process substitution `>(…)` and
# tee-style redirects don't survive across the whole script's lifetime
# without a try/finally wrapping every command. A wrapped Console.Error
# applies for the entire script automatically, no per-command code.

if ($env:STDERR_CAP_DISABLE -eq "1") { return }

$capBytes = if ($env:STDERR_CAP_BYTES) {
    [int64]$env:STDERR_CAP_BYTES
} else {
    1048576  # 1 MB default
}

# Idempotent: don't re-wrap if already wrapped (allows multiple sources).
if ([Console]::Error.GetType().Name -eq "CappedTextWriter") { return }

# Build a TextWriter that forwards to the real stderr until the cap is
# hit, then silently drops further writes.
#
# A PowerShell class, never C# through Add-Type (v0.2.107): Windows
# PowerShell 5.1 has no cross-process cache for Add-Type, so every hook fire
# started csc.exe + cvtres.exe, wrote a temporary assembly and had Defender
# scan it -- measured ~150 ms per fire under 5.1 and ~310 ms under 7, on
# every tool call (pre-tool-use loads this file). A PowerShell class is
# emitted in-process (Reflection.Emit): no compiler process, no file. Same
# members and the same counting as the C# writer it replaces.
#
# The class source is compiled through [scriptblock]::Create inside the
# try below rather than written as a top-level `class` statement: a
# top-level class is processed when this file is PARSED, so a host that
# refuses type definitions (Constrained Language Mode) would fail the
# dot-source itself -- and with it the hook. Here a refusal is caught and
# stderr stays uncapped, as the Add-Type failure path did.
# The abstract Encoding property is overridden by its getter method
# (get_Encoding), the way PowerShell classes implement abstract properties.
$vcoCapClassSource = @'
class CappedTextWriter : System.IO.TextWriter {
    hidden [System.IO.TextWriter] $Inner
    hidden [long] $Cap
    hidden [long] $Written = 0

    CappedTextWriter([System.IO.TextWriter] $inner, [long] $cap) {
        $this.Inner = $inner
        $this.Cap = $cap
    }

    [System.Text.Encoding] get_Encoding() { return $this.Inner.Encoding }

    [void] Write([char] $value) {
        if ($this.Written -ge $this.Cap) { return }
        $this.Inner.Write($value)
        $this.Written++
    }

    [void] Write([string] $value) {
        if ($null -eq $value -or $this.Written -ge $this.Cap) { return }
        $remaining = $this.Cap - $this.Written
        if ($value.Length -le $remaining) {
            $this.Inner.Write($value)
            $this.Written += $value.Length
        } else {
            $this.Inner.Write($value.Substring(0, [int]$remaining))
            $this.Written = $this.Cap
        }
    }

    [void] Write([char[]] $buffer, [int] $index, [int] $count) {
        if ($this.Written -ge $this.Cap) { return }
        $remaining = $this.Cap - $this.Written
        $toWrite = [int][Math]::Min([long]$count, $remaining)
        $this.Inner.Write($buffer, $index, $toWrite)
        $this.Written += $toWrite
    }

    [void] Flush() { $this.Inner.Flush() }
}
[CappedTextWriter]
'@

try {
    $vcoCapType = . ([scriptblock]::Create($vcoCapClassSource))
    [Console]::SetError($vcoCapType::new([Console]::Error, $capBytes))
} catch {
    # If the type cannot be defined (a locked-down host), don't block the
    # hook — just leave stderr uncapped. Better to risk a freeze than break
    # a hook on a platform we didn't anticipate.
    return
}
