#!/bin/bash
# KG-update nudge hook — fires a stderr <system-reminder> when the assistant
# has used >150k tokens since the last knowledge-graph write OR
# store_knowledge_node call. Defensive ergonomics: the user has noticed
# Claude often forgets to update the KG even after substantial work.
#
# Reset triggers:
#   - tool_name == "mcp__weaviate-kg__store_knowledge_node"
#   - (Write or Edit) AND file_path matches **/knowledge/**/*.md
#
# Bypass: KG_NUDGE_OFF=1 in the environment disables the nudge entirely.
# Threshold tweak: KG_NUDGE_THRESHOLD=<int> overrides the 150k default.
#
# State: ~/.claude/metrics/kg_update_tokens.jsonl
#   One JSON line per session_id: {"session_id": "...", "tokens": 12345, "updated_at": "..."}
#   Atomic rename + fcntl lock to handle concurrent writes from sibling agents.
#
# Always exits 0 — never blocks the tool call.
#
# Registered in .claude/settings.json under PostToolUse matcher "*"
# with background: true and timeout: 3.

set -uo pipefail

# Bypass switch.
[ "${KG_NUDGE_OFF:-0}" = "1" ] && exit 0

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

THRESHOLD="${KG_NUDGE_THRESHOLD:-150000}"
METRICS_DIR="$HOME/.claude/metrics"
METRICS_FILE="$METRICS_DIR/kg_update_tokens.jsonl"

mkdir -p "$METRICS_DIR" 2>/dev/null || exit 0

# Embed Python for JSON parsing + atomic state update. Bash + jq alone is
# fragile when handling missing keys / nested structures across Claude Code
# versions. Python is available everywhere this hook runs.
python3 <<PYTHON_EOF || true
import json
import os
import sys
import fcntl
import tempfile
from datetime import datetime, timezone

INPUT = """$INPUT"""
THRESHOLD = $THRESHOLD
METRICS_FILE = "$METRICS_FILE"

try:
    payload = json.loads(INPUT)
except (json.JSONDecodeError, TypeError):
    sys.exit(0)

session_id = payload.get("session_id") or "unknown"
tool_name = payload.get("tool_name") or ""
tool_input = payload.get("tool_input") or {}
tool_response = payload.get("tool_response") or {}

# Token usage — try several known shapes across Claude Code versions.
usage = (
    payload.get("usage")
    or payload.get("message", {}).get("usage")
    or tool_response.get("usage")
    or {}
)
in_tok = int(usage.get("input_tokens") or 0)
out_tok = int(usage.get("output_tokens") or 0)
new_tokens = in_tok + out_tok
# Note: cache_read_input_tokens excluded — they're cheap, don't represent new work.

# Detect "knowledge update" — counter reset trigger.
def is_knowledge_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\\\", "/")
    return "/knowledge/" in p and p.endswith(".md")

is_knowledge_update = False
if tool_name == "mcp__weaviate-kg__store_knowledge_node":
    is_knowledge_update = True
elif tool_name in ("Write", "Edit"):
    for k in ("file_path", "path", "filePath"):
        v = tool_input.get(k) or ""
        if is_knowledge_path(v):
            is_knowledge_update = True
            break

# Read existing state.
state = {}
if os.path.exists(METRICS_FILE):
    try:
        with open(METRICS_FILE, "r") as f:
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

current = state.get(session_id, {"session_id": session_id, "tokens": 0})

if is_knowledge_update:
    new_total = 0
    fired = False
else:
    new_total = int(current.get("tokens", 0)) + new_tokens
    # Threshold check — fire stderr nudge, then reset counter so we don't re-fire
    # immediately on every subsequent tool call.
    fired = False
    if new_total >= THRESHOLD:
        fired = True
        kilo = round(new_total / 1000)
        msg = f"""📚 KG-update nudge: you've used ~{kilo}k tokens since the last knowledge-graph update (counter resets on writes to knowledge/**/*.md OR store_knowledge_node calls).

Before continuing, review what you've learned in this session and:
  - UPDATE existing nodes if they're now outdated (status, content, valid_until)
  - CREATE new nodes for non-obvious facts: project state, architecture decisions, gotchas, patterns
  - Use store_knowledge_node OR write directly to knowledge/**/*.md (hook auto-syncs to Weaviate)

This is a soft nudge — if you've genuinely learned nothing worth recording, write a brief comment to that effect and continue. But don't silently skip the prompt."""
        print(msg, file=sys.stderr)
        # Reset counter after firing so subsequent nudges only fire on FRESH 150k.
        new_total = 0

current["tokens"] = new_total
current["updated_at"] = datetime.now(timezone.utc).isoformat()
state[session_id] = current

# Atomic write: write to tmpfile in same dir, fsync, rename.
try:
    dir_ = os.path.dirname(METRICS_FILE)
    fd, tmppath = tempfile.mkstemp(prefix=".kg_update_tokens.", dir=dir_)
    try:
        with os.fdopen(fd, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for entry in state.values():
                f.write(json.dumps(entry) + "\\n")
            f.flush()
            os.fsync(f.fileno())
            fcntl.flock(f, fcntl.LOCK_UN)
        os.rename(tmppath, METRICS_FILE)
    except OSError:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
except (OSError, ValueError):
    pass

sys.exit(0)
PYTHON_EOF

exit 0
