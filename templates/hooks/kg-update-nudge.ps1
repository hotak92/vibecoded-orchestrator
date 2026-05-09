# Parity-touch 2026-05-08: bash shebang of sibling .sh switched from #!/bin/bash to #!/usr/bin/env bash for macOS portability. PS1 has no shebang to change; this comment is the parity-required modification.
#!/usr/bin/env pwsh
# KG-update nudge — PowerShell sibling of kg-update-nudge.sh
# Same v10.1 logic; cross-OS sibling for Windows. Both shells delegate to
# the same Python heredoc that uses claude_token_counter.py.

# Scrub sensitive env vars before any subprocess spawning.
. "$PSScriptRoot/_lib/stderr-cap.ps1"

$secretEnvNames = @(
    "SUPABASE_KEY", "SUPABASE_URL", "GITHUB_TOKEN", "GH_TOKEN",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID", "TELEGRAM_BOT_TOKEN", "POSTGRES_PASSWORD",
    "VERCEL_TOKEN", "CLAUDE_API_KEY"
)
foreach ($name in $secretEnvNames) {
    if (Test-Path "Env:$name") { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}

# VCT_DISABLE_HOOKS escape hatch.
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# Per-hook bypass.
if ($env:KG_NUDGE_OFF -eq "1") { exit 0 }

# VCO-CENTRALIZED-KG: counter-only hook (PR #171 / 0.1.7).
#   Does NOT query Weaviate KG or codegraph collections. Reads the
#   transcript JSONL via claude_token_counter.py (work_units_total) and
#   detects KG-write events by tool_name ('mcp__weaviate-kg__store_knowledge_node',
#   'Write', 'Edit') + file_path matching knowledge/**/*.md. The access
#   matrix (VCT_KG_ACCESS_LIST / VCT_CODE_GRAPH_ACCESS_LIST) is N/A here —
#   no collections are touched. No centralization possible or needed.

# Read stdin JSON payload.
$input_json = [Console]::In.ReadToEnd()
if (-not $input_json) { exit 0 }

# Defaults — match the .sh sibling exactly.
$FIRST_THRESHOLD = if ($env:KG_NUDGE_FIRST) { $env:KG_NUDGE_FIRST } else { "175000" }
$INTERVAL        = if ($env:KG_NUDGE_INTERVAL) { $env:KG_NUDGE_INTERVAL } else { "50000" }
$METRIC_VERSION  = "v10"

# Resolve metrics dir (cross-OS via $HOME).
$metricsDir = Join-Path $HOME ".claude/metrics"
if (-not (Test-Path $metricsDir)) {
    New-Item -ItemType Directory -Force -Path $metricsDir | Out-Null
}
$metricsFile = Join-Path $metricsDir "kg_update_tokens.jsonl"

# Find a Python launcher: prefer the project venv if BASH_ENV path hints
# at it (lean-ctx shim), else fall back to py / python3 / python.
$pythonCmd = $null
foreach ($candidate in @("py", "python3", "python")) {
    $resolved = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($resolved) { $pythonCmd = $resolved.Source; break }
}
if (-not $pythonCmd) {
    # No Python available — fail silently (hook must not block the prompt).
    exit 0
}

# Hand the payload to Python via stdin; pass thresholds + paths via env.
# Note (audit fix 2026-05-07): the .ps1 sibling already passes INPUT via env
# var rather than interpolating it into the Python heredoc, so the bash-side
# bug (Python SyntaxError on triple-quoted INPUT) does not exist here.
# Parity-touch only — no behavioural change.
$env:_KG_NUDGE_INPUT       = $input_json
$env:_KG_NUDGE_FIRST       = $FIRST_THRESHOLD
$env:_KG_NUDGE_INTERVAL    = $INTERVAL
$env:_KG_NUDGE_METRICS     = $metricsFile
$env:_KG_NUDGE_VERSION     = $METRIC_VERSION

$pythonScript = @'
"""KG-update nudge body — shared by .sh and .ps1 hook shims."""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Read inputs from environment (set by the calling shell shim).
INPUT = os.environ.get("_KG_NUDGE_INPUT", "")
FIRST_THRESHOLD = int(os.environ.get("_KG_NUDGE_FIRST", "175000"))
INTERVAL = int(os.environ.get("_KG_NUDGE_INTERVAL", "50000"))
METRICS_FILE = os.environ.get("_KG_NUDGE_METRICS", "")
METRIC_VERSION = os.environ.get("_KG_NUDGE_VERSION", "v10")

if not INPUT or not METRICS_FILE:
    sys.exit(0)

try:
    payload = json.loads(INPUT)
except (json.JSONDecodeError, TypeError):
    sys.exit(0)

session_id = payload.get("session_id") or "unknown"
tool_name = payload.get("tool_name") or ""
tool_input = payload.get("tool_input") or {}
transcript_path = payload.get("transcript_path") or ""

is_post_tool = bool(tool_name)
is_user_prompt = (not tool_name) and ("prompt" in payload)
is_session_compact = (
    payload.get("hook_event_name") == "SessionStart"
    and payload.get("source") == "compact"
)

# --- Detect KG-write (counter reset) ---
def is_knowledge_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/")
    return "/knowledge/" in p and p.endswith(".md")

is_knowledge_update = False
if is_post_tool:
    if tool_name == "mcp__weaviate-kg__store_knowledge_node":
        is_knowledge_update = True
    elif tool_name in ("Write", "Edit"):
        for k in ("file_path", "path", "filePath"):
            v = tool_input.get(k) or ""
            if is_knowledge_path(v):
                is_knowledge_update = True
                break

# --- Scan transcript for work_units_total + escape markers ---
session_total = 0
escape_marker_token_total = 0

import re
NO_KG_UPDATE_MARKER_RE = re.compile(r"\[No KG update needed:\s*\S[^\]]*\]")

# v9: delegate to shared module (cross-OS path resolution).
_have_scanner = False
_scripts_dir = os.path.join(os.getcwd(), ".claude", "scripts")
if os.path.isfile(os.path.join(_scripts_dir, "claude_token_counter.py")):
    sys.path.insert(0, _scripts_dir)
    try:
        from claude_token_counter import (
            TranscriptScanner,
            iter_assistant_text_for_marker_scan,
        )
        _have_scanner = True
    except ImportError:
        try:
            from claude_token_counter import TranscriptScanner, iter_assistant_text
            iter_assistant_text_for_marker_scan = iter_assistant_text
            _have_scanner = True
        except ImportError:
            pass

if _have_scanner and transcript_path and os.path.exists(transcript_path):
    _escape_holder = [0]
    def _on_msg(entry, msg, running_units):
        for t in iter_assistant_text_for_marker_scan(msg):
            if NO_KG_UPDATE_MARKER_RE.search(t):
                _escape_holder[0] = running_units
                break
    scan_result = TranscriptScanner().scan(transcript_path, on_assistant_message=_on_msg)
    session_total = scan_result.work_units_total
    escape_marker_token_total = _escape_holder[0]

# --- Read existing state ---
state = {}
if os.path.exists(METRICS_FILE):
    try:
        with open(METRICS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    sid = entry.get("session_id")
                    if sid:
                        state[sid] = entry
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass

existing = state.get(session_id)
if existing and existing.get("metric_version") != METRIC_VERSION:
    existing = None
current = existing if existing else {
    "session_id": session_id,
    "baseline": 0,
    "last_nudge_at": 0,
    "last_seen_total": 0,
    "fired_once": False,
    "metric_version": METRIC_VERSION,
}

# --- Escape-hatch: marker in latest assistant turn ---
escape_hatch_active = (
    escape_marker_token_total > 0
    and escape_marker_token_total > int(current.get("baseline", 0))
)

# --- Branch logic ---
if is_session_compact:
    current["baseline"] = session_total
    current["last_nudge_at"] = 0
    current["fired_once"] = False
elif is_post_tool and is_knowledge_update:
    base = session_total if session_total else int(current.get("last_seen_total", 0))
    current["baseline"] = base
    current["last_nudge_at"] = 0
    current["fired_once"] = False
elif escape_hatch_active:
    current["baseline"] = escape_marker_token_total
    current["last_nudge_at"] = 0
    current["fired_once"] = False
elif is_user_prompt:
    delta = session_total - int(current.get("baseline", 0))
    fired_once = bool(current.get("fired_once", False))
    last_nudge_at = int(current.get("last_nudge_at", 0))

    should_fire = False
    if not fired_once and delta >= FIRST_THRESHOLD:
        should_fire = True
    elif fired_once and (session_total - last_nudge_at) >= INTERVAL:
        should_fire = True

    if should_fire:
        kilo = round(delta / 1000)
        if not fired_once:
            preamble = (
                f"📚 KG-update nudge: ~{kilo}k work units accumulated since the last "
                f"knowledge-graph update (cumulative session work, not live context size — "
                f"work units = output tokens + intake from Read/Web/Agent/Bash + file edits authored)."
            )
        else:
            since_last = round((session_total - last_nudge_at) / 1000)
            preamble = (
                f"📚 KG-update nudge ({kilo}k work units since last KG-write, "
                f"{since_last}k since last nudge — escalating because no KG node was written between nudges)."
            )
        msg = preamble + """ Counter resets on Write or Edit to knowledge/**/*.md OR store_knowledge_node calls.

DEFAULT IS TO RUN THE SEARCH. Most sessions of this size produce at least one durable lesson — non-obvious gotcha, post-incident finding, design decision rationale, or discovery that rewrites an old assumption. Treat "nothing to record" as the surprising case, not the default.

Workflow (do these in order — don't skip):
  1. SEARCH: list 2-5 candidate lessons from this session (forensic findings, design decisions, gotchas, anything that future-you would thank you for). For each, run hybrid_search('<topic phrase>') against the KG to find nodes that should ABSORB the new info. If your initial list is empty, look harder — what failed and got fixed? What surprised you?
  2. UPDATE: for each match, Edit the existing node — extend content, set 'valid_until' if old content was superseded. Re-grouping into existing nodes prevents duplicate-KG drift.
  3. CREATE only if no existing node fits. Two near-duplicate nodes hurt future-grep more than the missing node would.

If — after running step 1's hybrid_search calls — you've genuinely learned nothing worth recording, write [No KG update needed: <one-line reason naming what you searched for>] in your reply (top-level text, not in a tool call). The reason must name the topic(s) you searched and why it didn't yield candidates — bare reasons like "nothing new" or "orthogonal work" are insufficient signal that the search was actually done. Example of acceptable: [No KG update needed: searched 'PR merge order' and 'CI re-trigger flow' — both already covered in workflow-discipline.md].

The escape hatch is for the truly orthogonal turn (deploys, status reports, scrub-only). Default is to write the node."""
        print(msg)
        current["last_nudge_at"] = session_total
        current["fired_once"] = True

# Bookkeeping
if session_total:
    current["last_seen_total"] = session_total
current["updated_at"] = datetime.now(timezone.utc).isoformat()
current["metric_version"] = METRIC_VERSION
state[session_id] = current

# --- Atomic write (cross-OS via os.replace + temp file in same dir) ---
try:
    dir_ = os.path.dirname(METRICS_FILE)
    fd, tmppath = tempfile.mkstemp(prefix=".kg_update_tokens.", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for entry in state.values():
                f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmppath, METRICS_FILE)
    except OSError:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
except (OSError, ValueError):
    pass

sys.exit(0)
'@

# Run the Python script. Output (the nudge message) goes to stdout, which
# Claude Code surfaces as a system-reminder for UserPromptSubmit hooks.
& $pythonCmd -c $pythonScript

# Cleanup the temp env vars we set (per-process, harmless but tidy).
Remove-Item Env:_KG_NUDGE_INPUT, Env:_KG_NUDGE_FIRST, Env:_KG_NUDGE_INTERVAL, Env:_KG_NUDGE_METRICS, Env:_KG_NUDGE_VERSION -ErrorAction SilentlyContinue

exit 0
