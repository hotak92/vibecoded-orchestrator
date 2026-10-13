# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# user-prompt-submit-reminder.ps1
# Context preservation reminder triggered on output volume (work units).
#
# v0.2.102 W6-F: the counter COUNTS again. Pre-W6-F the .sh sibling
# hardwired WORDS_THIS_TURN=0 and this file read a tool_name the
# UserPromptSubmit payload never carries, so the session counter never grew
# and neither the checkpoint nor the staleness nudge could fire. The counter
# is now the session's cumulative WORK UNITS from the live transcript,
# scanned INCREMENTALLY (per-session byte offset) by the same
# .claude/scripts/claude_token_counter.py the KG-update nudge uses - one
# home, no second counting implementation. Thresholds keep the retired
# word-scale's documented token equivalents: 300 000 work units for the
# checkpoint (was "200K words (~300K tokens)"), a 180 000-unit delta for
# the CONTEXT_STATE-staleness window (was 120K words).

. "$PSScriptRoot/_lib/stderr-cap.ps1"

# Hook input contract (v2.1.x): JSON on stdin, NOT $env:CLAUDE_TOOL_NAME or
# $env:CLAUDE_SESSION_ID env vars. Drain stdin once and parse session_id
# from the JSON payload — session_id is the canonical per-conversation key.
# Falling back to "default" only on malformed JSON keeps concurrent
# Claude Code sessions on the same project from sharing state files
# (PR #176 cross-OS sweep: hooks read session_id from the stdin JSON
# payload, never $env:CLAUDE_SESSION_ID — the hook runner does not
# populate it, so an env-var read silently collapses every session
# onto "default").
# UserPromptSubmit hooks don't see tool_name in the payload either, so
# per-tool accounting is moot under v2.1.x — kept as a defensive default
# in case a future runtime ever populates the env var. Today
# $env:CLAUDE_TOOL_NAME is always empty. Verified empirically 2026-05-08
# via stdin-capture diagnostic.
$HookStdin = ""
try { $HookStdin = [Console]::In.ReadToEnd() } catch { }
$SessionId = ""
try {
    $payload = $HookStdin | ConvertFrom-Json -ErrorAction Stop
    if ($payload) {
        if ($payload.session_id) { $SessionId = [string]$payload.session_id }
    }
} catch {
    # Empty/malformed stdin - fall through to defaults
}
if (-not $SessionId) { $SessionId = "default" }

# Project root: the ONE rule (_lib/project-root.ps1, must match
# vco_lib/project_root.py): CLAUDE_PROJECT_DIR, else the nearest marked VCO
# project above the current location. None -> nothing to do (no state is written).
. (Join-Path $PSScriptRoot "_lib/project-root.ps1")
$ProjectDir = Get-VcoProjectRoot
if (-not $ProjectDir) { exit 0 }
$ProjectName = Split-Path $ProjectDir -Leaf
$ContextFile = Join-Path $ProjectDir ".claude/CONTEXT_STATE.md"
$Tmp = if ($env:TMPDIR) { $env:TMPDIR } elseif ($env:TEMP) { $env:TEMP } else { "C:\Windows\Temp" }
$SessionLog = Join-Path $Tmp "claude-session-$ProjectName-$SessionId"
$OffsetFile = "$SessionLog.scan_offset"

if (-not (Test-Path $SessionLog)) {
    Set-Content -Path $SessionLog -Value "0" -Encoding ascii
}

$TotalWords = 0
try { $TotalWords = [int](Get-Content $SessionLog -Raw -ErrorAction Stop).Trim() } catch { $TotalWords = 0 }

# Accumulate the work units the transcript gained since the last prompt
# (W6-F). The counter module resolves from the HOOK'S OWN bundle
# (../scripts) or $env:CLAUDE_PROJECT_DIR/.claude/scripts - never cwd (same
# ladder as kg-update-nudge). A transcript to count with no importable
# counter is a broken install: the embedded Python prints a loud stderr
# line and exits 1, propagated below (non-blocking for UserPromptSubmit;
# only exit 2 blocks a prompt). No Python at all is environmental - the
# counter silently keeps its last value, as before.
$FindPy = Join-Path $PSScriptRoot "_lib/find-python.ps1"
if (Test-Path -LiteralPath $FindPy -PathType Leaf) { . $FindPy }
if ($PY) {
    $env:_UPSR_HOOK_DIR = $PSScriptRoot
    $env:_UPSR_STATE = $SessionLog
    $env:_UPSR_OFFSET = $OffsetFile
    $scanSrc = @'
import json, os, sys

state_file = os.environ.get("_UPSR_STATE", "")
offset_file = os.environ.get("_UPSR_OFFSET", "")

def _read_num(path):
    try:
        with open(path, "r") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0

def _write_num(path, value):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(value))
        os.replace(tmp, path)
    except OSError:
        pass

total = _read_num(state_file)
try:
    payload = json.loads(sys.stdin.read() or "{}")
except Exception:
    payload = {}
transcript = ""
if isinstance(payload, dict):
    transcript = payload.get("transcript_path") or ""
if not transcript or not os.path.exists(transcript):
    print(total)  # nothing to count: keep the accumulated value
    sys.exit(0)

_counter_dir = ""
for _d in (
    os.path.normpath(os.path.join(os.environ.get("_UPSR_HOOK_DIR", "x"),
                                  os.pardir, "scripts")),
    os.path.join(os.environ.get("CLAUDE_PROJECT_DIR", "x"),
                 ".claude", "scripts"),
):
    if os.path.isfile(os.path.join(_d, "claude_token_counter.py")):
        _counter_dir = _d
        break
if not _counter_dir:
    print("[user-prompt-submit-reminder] BROKEN INSTALL: .claude/scripts/"
          "claude_token_counter.py is missing; the session work counter "
          "cannot run. Re-run the VCO bundle update to restore it.",
          file=sys.stderr)
    sys.exit(1)
sys.path.insert(0, _counter_dir)
try:
    from claude_token_counter import TranscriptScanner
except ImportError:
    print("[user-prompt-submit-reminder] BROKEN INSTALL: .claude/scripts/"
          "claude_token_counter.py failed to import; the session work "
          "counter cannot run. Re-run the VCO bundle update.",
          file=sys.stderr)
    sys.exit(1)

try:
    prev_off = _read_num(offset_file)
    res = TranscriptScanner().scan(transcript, start_offset=prev_off)
    # A scan that did not start at the persisted offset (rotated transcript
    # or the bounded tail window of a first scan over an oversized file) is
    # a fresh basis - its delta replaces the cumulative, never adds to it.
    base = total if res.start_offset_used == prev_off else 0
    total = base + res.work_units_total
    # W6-F fix-round: persist the end_offset UNCONDITIONALLY -- 0 IS
    # a valid offset (a transcript rotated to empty consumed nothing and
    # must reset the resume point to 0, else content regrown past the stale
    # offset is skipped mid-line). The old `or prev_off` kept the stale
    # offset after a rotate-to-empty -- the exact rotation defect class the
    # nudge fixed with its `_new_scan_offset is not None` shape; mirrored
    # here (the reminder has no "no scan ran" branch -- reaching this line
    # means a scan ran and end_offset is always an int).
    end = res.end_offset
    if end != prev_off:
        _write_num(offset_file, end)  # offset first: a kill between the two
        _write_num(state_file, total)  # writes under-counts (safe direction)
except Exception:
    # A scanning hiccup must never break the prompt: keep the old total.
    pass
print(total)
'@
    $scanOut = $HookStdin | & $PY -c $scanSrc
    $scanRc = $LASTEXITCODE
    Remove-Item Env:_UPSR_HOOK_DIR, Env:_UPSR_STATE, Env:_UPSR_OFFSET -ErrorAction SilentlyContinue
    if ($scanRc -ne 0) { exit 1 }  # broken install: the python side already said why on stderr
    if ($scanOut) {
        $last = ($scanOut | Select-Object -Last 1)
        try { $TotalWords = [int]([string]$last).Trim() } catch { }
    }
}

# 300K work units = the documented "~300K tokens" scale of the retired
# 200K-word threshold (W6-F: the counter now holds real work units).
if ($TotalWords -ge 300000) {
    $Marker = Join-Path $Tmp "claude-ctx-warn-$ProjectName-$SessionId"
    if (Test-Path $Marker) { exit 0 }
    Write-Output ""
    Write-Output "Session checkpoint (~300K work units this session)"
    Write-Output "   - /refresh-context - Update KG if topic shifted"
    Write-Output "   - Update CONTEXT_STATE.md with findings"
    Write-Output "   - Consider new session before context compression"
    Write-Output ""
    New-Item -ItemType File -Path $Marker -Force | Out-Null
    exit 0
}

# CONTEXT_STATE staleness - counter-based, one-shot per ~180K-work-unit
# window (the retired 120K-word window's token equivalent) since the LAST
# nudge or last actual edit (whichever is more recent).
#
# Replaces the prior time-based 30-min staleness check that fired 4-5x
# during long deep-work sessions. The new condition is "enough work has
# accumulated that a CONTEXT_STATE refresh is genuinely useful," not
# "the file is old."
#
# The staleness marker file is keyed by both ProjectName and SessionId
# so concurrent Claude Code sessions on the same project don't stomp on
# each other's counter (the same concurrency fix as PR #176 applied to
# 11 other hooks: key per-session state by the session_id parsed from
# the stdin JSON payload, not by project alone).
$StalenessMarker = Join-Path $Tmp "claude-ctx-staleness-$ProjectName-$SessionId"
$LastFireWords = 0
if (Test-Path $StalenessMarker) {
    try { $LastFireWords = [int](Get-Content $StalenessMarker -Raw -ErrorAction Stop).Trim() } catch { $LastFireWords = 0 }
    if (Test-Path $ContextFile) {
        $CtxMtime = (Get-Item $ContextFile -ErrorAction SilentlyContinue).LastWriteTime.ToFileTime()
        $MarkerMtime = (Get-Item $StalenessMarker -ErrorAction SilentlyContinue).LastWriteTime.ToFileTime()
        if ($CtxMtime -gt $MarkerMtime) {
            Set-Content -Path $StalenessMarker -Value $TotalWords -Encoding ascii
            $LastFireWords = $TotalWords
        }
    }
}
$Delta = $TotalWords - $LastFireWords
if ($Delta -ge 180000) {
    Write-Output ""
    Write-Output "~${Delta} work units since last CONTEXT_STATE update."
    Write-Output "   Append a 1-2 line progress note now (what shipped + what's next),"
    Write-Output "   before context grows further or /compact lands."
    Write-Output ""
    Set-Content -Path $StalenessMarker -Value $TotalWords -Encoding ascii
}
exit 0
