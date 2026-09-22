# OS-EXEMPT-PARITY: 2026-05-22 BOM-only addition for Windows PS 5.1 (commit 97eceaf) — .sh sibling reads bytes not codepages, so no Bash-side change needed.
# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# stop-failure-notify.ps1
# Fires on StopFailure event — when a turn ends due to API error.
# Sends urgent desktop notification and logs the failure.
#
# v0.2.96 WP-8 (register issue 14 — the 2026-09-20 304-toast storm):
#   * dedup — at most ONE desktop notification per 5 minutes per
#     (project, error class). Events inside the window are counted and the
#     next notification after the window carries "(N suppressed)". The
#     LEDGER line is still written for EVERY event — suppression is of the
#     toast, not the record.
#   * hardened extraction — payloads that lack the expected shape log a
#     TRUNCATED raw payload (<=500 chars) instead of "unknown: No details".
#   * individual kill switch — VCO_STOP_FAILURE_NOTIFY=0 suppresses the
#     desktop notification only (the ledger keeps recording); distinct from
#     VCT_DISABLE_HOOKS, which exits before any work.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$ProjectDir = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { (Get-Location).Path }
$ProjectName = Split-Path $ProjectDir -Leaf

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }

# v0.2.92 W7: the metrics home moved out of ~/.claude; `_lib/metrics-dir.ps1`
# is the ONE PowerShell-side resolver (lockstep sibling of
# `_lib/metrics-dir.sh`). A missing helper leaves $LogDir empty — the dedup
# state cannot be persisted, so the core below fails OPEN (every event
# notifies) rather than silently suppressing on a state it cannot read.
$MetricsLib = Join-Path $PSScriptRoot "_lib/metrics-dir.ps1"
$LogDir = ""
if (Test-Path -LiteralPath $MetricsLib -PathType Leaf) {
    . $MetricsLib
    $LogDir = Get-VcoMetricsDir
}

# v0.2.96 WP-8 core — MUST MATCH the block between the same
# VCO_STOP_FAILURE_CORE markers in stop-failure-notify.sh byte-for-byte
# (pinned by tests/test_v0296_stop_failure_dedup.py::test_ps1_core_matches_sh_core).
$StopFailureCore = @'
# v0.2.96 WP-8 (register issue 14): ONE python core shared byte-for-byte
# between stop-failure-notify.sh and stop-failure-notify.ps1. argv:
#   [1] metrics dir ("" = no ledger / no dedup state; the notification must
#       still go out)
#   [2] project name
# stdin: the raw StopFailure payload. Prints two lines on stdout:
#   line 1: "1" (notify) or "0" (suppressed by the dedup window)
#   line 2: "<error class>: <message>" for the desktop notification
import json
import os
import re
import sys
import time

WINDOW_SECS = 300  # dedup: at most one notification per (project, class)
RAW_CAP = 500      # truncated raw payload length for unexpected shapes


def _one_line(s):
    return " ".join(s.split())


def _trunc(s, cap):
    s = _one_line(s)
    if len(s) > cap:
        s = s[: cap - 3] + "..."
    return s


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def main():
    metrics_dir = (sys.argv[1] if len(sys.argv) > 1 else "") or ""
    project = (sys.argv[2] if len(sys.argv) > 2 else "") or "?"
    raw = sys.stdin.read()

    etype = "unknown"
    emsg = ""
    sid = ""
    try:
        d = json.loads(raw)
        if not isinstance(d, dict):
            raise ValueError("payload is not a JSON object")
    except Exception:
        d = None
    if d is not None:
        err = d.get("error")
        if isinstance(err, dict):
            t = err.get("type")
            m = err.get("message")
            if isinstance(t, str) and t:
                etype = _one_line(t)
            if isinstance(m, str) and m:
                emsg = _one_line(m)[:120]
        v = d.get("session_id")
        if isinstance(v, str):
            sid = v[:8]
    if not emsg:
        # 2026-09-20 storm: 304 identical "unknown: No details" toasts
        # because the trust-failure payload has NO `error` key and the old
        # parser had no fallback. Log the truncated RAW payload instead --
        # a payload we cannot parse is the evidence, not noise.
        emsg = "raw payload: " + _trunc(raw, RAW_CAP)

    notify = True
    suppressed_note = 0
    if metrics_dir:
        # Dedup key = (project, error class). The window authority is THIS
        # pair, not session_id: the 2026-09-20 storm generated a FRESH
        # session_id on every event, so a per-session key would have let
        # all 304 through. The payload's session_id (read from stdin, never
        # env -- env is untrusted context here) is still recorded in the
        # state file for diagnosis.
        def _safe(x):
            x = re.sub(r"[^A-Za-z0-9_.-]", "_", x or "x")
            return x[:64]

        state_path = os.path.join(
            metrics_dir,
            "stop_failure_dedup_%s@%s.json" % (_safe(project), _safe(etype)),
        )
        now = int(time.time())
        state = {}
        try:
            with open(state_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                state = loaded
        except Exception:
            state = {}
        last_ts = state.get("ts", 0)
        prev_suppressed = state.get("suppressed", 0)
        if not _is_int(last_ts) or not _is_int(prev_suppressed):
            last_ts = 0
            prev_suppressed = 0
        notify = (now - last_ts) >= WINDOW_SECS
        if notify:
            # The first notification after a quiet stretch reports how many
            # events were swallowed by the window (they are ALL still in the
            # ledger -- suppression is of the toast, not the record).
            suppressed_note = prev_suppressed
            new_state = {"ts": now, "suppressed": 0, "session_id": sid}
        else:
            new_state = {
                "ts": last_ts,
                "suppressed": prev_suppressed + 1,
                "session_id": sid,
            }
        try:
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(new_state, fh)
            os.replace(tmp, state_path)
        except Exception:
            # Fail OPEN: if the state cannot be persisted we cannot dedup,
            # and a silent hook is worse than a repeated toast.
            notify = True

    if metrics_dir:
        line = json.dumps(
            {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "project": project,
                "session_id": sid,
                "error_type": etype,
                "error_message": emsg,
            }
        )
        try:
            with open(
                os.path.join(metrics_dir, "failures.jsonl"), "a", encoding="utf-8"
            ) as fh:
                fh.write(line + "\n")
        except Exception:
            pass

    msg = etype + ": " + emsg
    if notify and suppressed_note > 0:
        msg += " (%d suppressed)" % suppressed_note
    sys.stdout.write(("1" if notify else "0") + "\n" + msg + "\n")


main()
'@

$Payload = ""
try { $Payload = [Console]::In.ReadToEnd() } catch { }

# Parse + dedup + ledger in ONE interpreter run. The old hook spent three
# separate `& $PY -c` calls re-parsing the payload and built the ledger line
# by string interpolation -- any quote in a message corrupted the JSON.
$NotifyFlag = "1"
$NotifyMsg = "unknown: (core unavailable; see metrics ledger)"
if ($PY -and $Payload -ne "") {
    try {
        $CoreOut = ($Payload | & $PY -c $StopFailureCore $LogDir $ProjectName 2>$null)
        $CoreLines = @($CoreOut)
        if ($CoreLines.Count -ge 2) {
            $NotifyFlag = [string]$CoreLines[0]
            $NotifyMsg = ($CoreLines | Select-Object -Skip 1) -join " "
        }
    } catch { }
}

# Individual kill switch (v0.2.96 WP-8): VCO_STOP_FAILURE_NOTIFY=0 suppresses
# ONLY the desktop notification -- the ledger above still records every event
# (it is the diagnostic the 2026-09-20 storm diagnosis depended on). Distinct
# from VCT_DISABLE_HOOKS, which exits before any work.
if ($env:VCO_STOP_FAILURE_NOTIFY -eq "0") { exit 0 }

# Urgent cross-platform notification.
$NotifyScript = Join-Path $ProjectDir ".claude/scripts/notify.py"
if ($NotifyFlag -eq "1" -and $PY -and (Test-Path $NotifyScript)) {
    try {
        & $PY $NotifyScript "Claude API Error -- $ProjectName" "$NotifyMsg" `
            --urgency critical --icon dialog-error --expire-time 15000 2>$null | Out-Null
    } catch { }
}
exit 0
