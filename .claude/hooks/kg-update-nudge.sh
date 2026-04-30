#!/bin/bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# KG-update nudge hook — fires a stderr <system-reminder> when the assistant
# has used substantial tokens since the last knowledge-graph write OR
# store_knowledge_node call.
#
# v3 (2026-04-30): two-event hook (PostToolUse + UserPromptSubmit).
#   - PostToolUse path: detects KG-writes (Write/Edit/store_knowledge_node)
#     and resets the baseline. Background-safe; never prints stderr.
#   - UserPromptSubmit path: reads state, computes delta against the live
#     transcript token total, prints stderr if threshold tripped. Stderr
#     from UserPromptSubmit hooks IS surfaced into the conversation as
#     a system-reminder (unlike background PostToolUse hooks where stderr
#     is detached). This is the v2→v3 fix — v2 fired correctly but the
#     stderr never reached the conversation.
#
# v2 (2026-04-30): rewritten because v1 read tokens from the PostToolUse
#   `usage` field, which Claude Code does not populate on PostToolUse. v2
#   reads cumulative session tokens from the live transcript JSONL passed
#   as `transcript_path` in the hook payload.
#
# Trigger ladder (per user 2026-04-30):
#   - First nudge: cumulative session tokens since last KG-write >= 150_000
#   - Subsequent nudges: every 10_000 additional tokens after first fire
#
# Counter reset triggers (PostToolUse):
#   - tool_name == "mcp__weaviate-kg__store_knowledge_node"
#   - (Write or Edit) AND file_path matches **/knowledge/**/*.md
#
# Bypass: KG_NUDGE_OFF=1 disables the nudge entirely.
# Threshold tweak:
#   KG_NUDGE_FIRST=<int>     overrides 150_000 first-fire threshold
#   KG_NUDGE_INTERVAL=<int>  overrides 10_000 subsequent interval
#
# State: ~/.claude/metrics/kg_update_tokens.jsonl
#   {session_id, baseline, last_nudge_at, last_seen_total, fired_once, updated_at}
#   Atomic rename + fcntl lock to handle concurrent writes from sibling agents.
#
# Always exits 0 — never blocks the tool call or prompt.

set -uo pipefail

# Bypass switch.
[ "${KG_NUDGE_OFF:-0}" = "1" ] && exit 0

INPUT="$(cat 2>/dev/null || true)"
[ -z "$INPUT" ] && exit 0

FIRST_THRESHOLD="${KG_NUDGE_FIRST:-150000}"
INTERVAL="${KG_NUDGE_INTERVAL:-25000}"
METRICS_DIR="$HOME/.claude/metrics"
METRICS_FILE="$METRICS_DIR/kg_update_tokens.jsonl"

mkdir -p "$METRICS_DIR" 2>/dev/null || exit 0

python3 <<PYTHON_EOF || true
import json
import os
import sys
import fcntl
import tempfile
from datetime import datetime, timezone

INPUT = """$INPUT"""
FIRST_THRESHOLD = $FIRST_THRESHOLD
INTERVAL = $INTERVAL
METRICS_FILE = "$METRICS_FILE"

try:
    payload = json.loads(INPUT)
except (json.JSONDecodeError, TypeError):
    sys.exit(0)

session_id = payload.get("session_id") or "unknown"
tool_name = payload.get("tool_name") or ""
tool_input = payload.get("tool_input") or {}
transcript_path = payload.get("transcript_path") or ""

# Branch by event type:
#   PostToolUse → has tool_name + tool_input. Mission: detect KG-write,
#                 reset baseline. NEVER print stderr (background ⇒ detached).
#   UserPromptSubmit → has prompt + (no tool_name). Mission: compute
#                      cumulative tokens, fire nudge to stderr if threshold.
is_post_tool = bool(tool_name)
is_user_prompt = (not tool_name) and ("prompt" in payload)
# v5 (2026-04-30): SessionStart hook with source=compact resets the
# nudge state for this session. Post-compact context is sparse and
# hallucination risk is high — forcing the first post-compact nudge
# to wait a fresh 150k tokens prevents agents from writing speculative
# KG nodes based on whatever the compactor preserved. source=startup
# and source=resume don't reset (they reattach to existing state).
is_session_compact = (
    payload.get("hook_event_name") == "SessionStart"
    and payload.get("source") == "compact"
)

# --- Detect KG-write (counter reset) — only relevant on PostToolUse ---
def is_knowledge_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\\\", "/")
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

# --- Read cumulative session tokens from live transcript ---
# Skip the costly scan on PostToolUse if it's a KG-write (we just need to
# reset and don't actually need the current total).
session_total = 0
# v4 (2026-04-30): always scan when a transcript is available. v3 tried
# to skip the scan on KG-write events (assuming we'd just reset
# baseline), but that left baseline at 0 because last_seen_total
# wasn't kept fresh between events. Result: KG-writes appeared not to
# reset the counter at all in long sessions. Scanning unconditionally
# (~0.65s on 168 MB transcripts) is cheap; baseline now reflects the
# real cumulative-tokens-at-write-time.
need_total = bool(transcript_path) and os.path.exists(transcript_path)
if need_total:
    try:
        size = os.path.getsize(transcript_path)
        if size < 256 * 1024 * 1024:
            with open(transcript_path, "r", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = d.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    in_tok = int(usage.get("input_tokens") or 0)
                    out_tok = int(usage.get("output_tokens") or 0)
                    cc_tok = int(usage.get("cache_creation_input_tokens") or 0)
                    session_total += in_tok + out_tok + cc_tok
    except OSError:
        pass

# --- Read existing state ---
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

current = state.get(session_id, {
    "session_id": session_id,
    "baseline": 0,
    "last_nudge_at": 0,
    "last_seen_total": 0,
    "fired_once": False,
})

# --- Branch logic ---
if is_session_compact:
    # /compact (manual) and auto-compaction both fire SessionStart with
    # source=compact. Reset state to "fresh session" — baseline at
    # post-compact session_total, fired_once cleared, so the next nudge
    # waits the full FIRST_THRESHOLD (150k) instead of 25k. Rationale:
    # compaction throws away most context, so agents have low-quality
    # signal for what's worth saving. Forcing them to do real work
    # before the next nudge prevents hallucinated/speculative KG nodes.
    current["baseline"] = session_total
    current["last_nudge_at"] = 0
    current["fired_once"] = False
elif is_post_tool and is_knowledge_update:
    # Reset baseline. Use last_seen_total as the new baseline if we
    # didn't recompute (we skipped the transcript scan above).
    base = session_total if session_total else int(current.get("last_seen_total", 0))
    current["baseline"] = base
    current["last_nudge_at"] = 0
    current["fired_once"] = False
elif is_user_prompt:
    # Threshold check + stderr fire.
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
            preamble = f"📚 KG-update nudge: ~{kilo}k tokens of work since the last knowledge-graph update."
        else:
            since_last = round((session_total - last_nudge_at) / 1000)
            preamble = (
                f"📚 KG-update nudge ({kilo}k tokens since last KG-write, "
                f"{since_last}k since last nudge — escalating because no KG node was written between nudges)."
            )
        msg = preamble + """ Counter resets on Write or Edit to knowledge/**/*.md OR store_knowledge_node calls.

Workflow (do these in order — don't skip steps):
  1. SEARCH: list what you've learned this session that's worth keeping. For each item, run hybrid_search('<topic phrase>') against the KG to find nodes that should ABSORB the new info.
  2. UPDATE: for each match, Edit the existing node — extend content, set 'valid_until' if the old content was superseded. Re-grouping into existing nodes prevents duplicate-KG drift.
  3. CREATE only if no existing node fits. Two near-duplicate nodes hurt future-grep more than the missing node would.

If after the search you've genuinely learned nothing worth recording, write a one-line comment saying so and continue — don't skip silently."""
        # UserPromptSubmit hooks surface STDOUT as <system-reminder>
        # context, not stderr. v2/v3 used stderr — the message was
        # generated correctly but never reached the conversation.
        # PostToolUse-background never surfaces either channel.
        print(msg)
        current["last_nudge_at"] = session_total
        current["fired_once"] = True
else:
    # PostToolUse non-KG-write: just bookkeeping the session_total.
    pass

# Update last_seen_total whenever we computed it.
if session_total:
    current["last_seen_total"] = session_total
current["updated_at"] = datetime.now(timezone.utc).isoformat()
state[session_id] = current

# --- Atomic write ---
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
