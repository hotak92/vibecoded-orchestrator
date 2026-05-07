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
# hit, then silently drops further writes. We compile this inline so the
# helper has no external file dependencies.
$source = @"
using System;
using System.IO;
using System.Text;

public class CappedTextWriter : TextWriter
{
    private readonly TextWriter _inner;
    private readonly long _cap;
    private long _written;

    public CappedTextWriter(TextWriter inner, long cap)
    {
        _inner = inner;
        _cap = cap;
        _written = 0;
    }

    public override Encoding Encoding { get { return _inner.Encoding; } }

    public override void Write(char value)
    {
        if (_written >= _cap) return;
        _inner.Write(value);
        _written++;
    }

    public override void Write(string value)
    {
        if (value == null || _written >= _cap) return;
        long remaining = _cap - _written;
        if (value.Length <= remaining)
        {
            _inner.Write(value);
            _written += value.Length;
        }
        else
        {
            _inner.Write(value.Substring(0, (int)remaining));
            _written = _cap;
        }
    }

    public override void Write(char[] buffer, int index, int count)
    {
        if (_written >= _cap) return;
        long remaining = _cap - _written;
        int toWrite = (int)Math.Min(count, remaining);
        _inner.Write(buffer, index, toWrite);
        _written += toWrite;
    }

    public override void Flush() { _inner.Flush(); }
}
"@

# Add-Type is the standard PS 5.1+ way to compile inline C#. Re-running
# in the same session is a no-op (PS caches compiled types by name).
try {
    Add-Type -TypeDefinition $source -ErrorAction Stop
} catch {
    # If compilation fails (extremely unlikely), don't block the hook —
    # just leave stderr uncapped. Better to risk a freeze than break a
    # hook on a platform we didn't anticipate.
    return
}

[Console]::SetError([CappedTextWriter]::new([Console]::Error, $capBytes))
