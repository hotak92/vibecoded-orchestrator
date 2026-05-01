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
#   - First nudge: cumulative session tokens since last KG-write >= 175_000
#   - Subsequent nudges: every 10_000 additional tokens after first fire
#
# Counter reset triggers (PostToolUse):
#   - tool_name == "mcp__weaviate-kg__store_knowledge_node"
#   - (Write or Edit) AND file_path matches **/knowledge/**/*.md
#
# Bypass: KG_NUDGE_OFF=1 disables the nudge entirely.
# Threshold tweak:
#   KG_NUDGE_FIRST=<int>     overrides 175_000 first-fire threshold
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

FIRST_THRESHOLD="${KG_NUDGE_FIRST:-175000}"
INTERVAL="${KG_NUDGE_INTERVAL:-50000}"
METRIC_VERSION="v10"
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
METRIC_VERSION = "$METRIC_VERSION"

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
# to wait a fresh 175k tokens prevents agents from writing speculative
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
# v4 (2026-04-30): always scan when a transcript is available. v3 tried
# to skip the scan on KG-write events (assuming we'd just reset
# baseline), but that left baseline at 0 because last_seen_total
# wasn't kept fresh between events. Result: KG-writes appeared not to
# reset the counter at all in long sessions. Scanning unconditionally
# (~0.65s on 168 MB transcripts) is cheap; baseline now reflects the
# real cumulative-tokens-at-write-time.
#
# v6 (2026-04-30): also scan for an opt-out marker. If the assistant
# wrote literal "[No KG update needed]" in the most recent assistant
# message, treat that turn as if it were a KG write (reset baseline +
# fired_once). Cheap, transcript-based escape hatch — agent doesn't
# have to actually edit a knowledge file just to silence the nudge
# when the session genuinely had nothing worth recording.
session_total = 0
escape_marker_token_total = 0  # session_total at the time the marker was last seen; 0 if never
seen_request_ids = set()
# v9 (2026-05-01): counter is cache_creation_input_tokens summed
# over requestId-deduped entries. Web research + live transcript
# verification (16,349 raw entries -> 6,950 deduped, 57.5% redundant):
#   - input_tokens is a streaming placeholder, 75% are 0 or 1 — UNRELIABLE
#   - output_tokens undercounted ~10-17x vs statusbar — UNRELIABLE
#   - cache_creation_input_tokens matches statusbar 1x — RELIABLE
# v8's output-tokens-only counter still over-fired because Claude Code
# emits ~3 JSONL entries per actual API request during streaming;
# without requestId dedup, totals are ~3x inflated. v9 dedups by
# requestId and uses cache_creation as the genuine "new context the
# model had to digest" signal. New thresholds (500k first, 200k
# interval) reflect the cache_creation scale: a typical multi-hour
# substantive session lands ~500k-1M, casual chat sessions stay
# under 100k.
#
# v7 (2026-04-30): the escape marker now requires a non-empty reason — the
# bare "No KG update needed" string is no longer accepted. Pattern requires
# the marker bracket plus a colon plus at least one non-whitespace char.
# This forces the agent to articulate WHY the work was orthogonal instead
# of treating the escape hatch as a default. The regex below allows any
# character except a closing bracket and requires at least one non-whitespace char.
import re
NO_KG_UPDATE_MARKER_RE = re.compile(r"\[No KG update needed:\s*\S[^\]]*\]")

# v9: delegate transcript scanning to shared module
# (.claude/scripts/claude_token_counter.py). The module dedups by
# requestId and sums cache_creation_input_tokens — see its module
# docstring for field-reliability rationale. Hooks are invoked with
# cwd=project root, so the relative .claude/scripts path resolves.
_have_scanner = False
_scripts_dir = os.path.join(os.getcwd(), ".claude", "scripts")
if os.path.isfile(os.path.join(_scripts_dir, "claude_token_counter.py")):
    sys.path.insert(0, _scripts_dir)
    try:
        from claude_token_counter import TranscriptScanner, iter_assistant_text
        _have_scanner = True
    except ImportError:
        pass

if _have_scanner and transcript_path and os.path.exists(transcript_path):
    # v10.1: import the marker-scan helper that strips hook-body fingerprints
    # before regex match (B13 self-suppression fix). Falls back gracefully
    # if old version of the module is loaded.
    try:
        from claude_token_counter import iter_assistant_text_for_marker_scan
        _iter_for_marker = iter_assistant_text_for_marker_scan
    except ImportError:
        _iter_for_marker = iter_assistant_text  # legacy fallback (v10)

    _escape_holder = [0]
    def _on_msg(entry, msg, running_units):
        # Escape-hatch detection: only top-level assistant text, with
        # nudge-body fingerprint stripping. Capture work_units_total at
        # the time of the LATEST marker so the baseline-reset logic can
        # compare against current baseline.
        for t in _iter_for_marker(msg):
            if NO_KG_UPDATE_MARKER_RE.search(t):
                _escape_holder[0] = running_units
                break
    scan_result = TranscriptScanner().scan(transcript_path, on_assistant_message=_on_msg)
    # v10.1 (B1 fix): use work_units_total (production+intake) instead of
    # cache_creation_total (cost-only). cache_creation grew with hook-injected
    # context per turn, not with what the model actually produced or
    # processed — wrong signal for "work done since last KG save".
    session_total = scan_result.work_units_total
    escape_marker_token_total = _escape_holder[0]

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

# v10.1 (B14): drop pre-v10.1 entries on metric-version mismatch.
# Earlier versions stored cache_creation-scale baselines (~10× larger
# than v10.1's work_units_total). Without this migration, baseline can
# move backwards on first KG-write reset under v10.1, suppressing
# nudges indefinitely. Dropping the entry triggers a fresh baseline
# from current scan, which is correct.
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

# --- Escape-hatch: "[No KG update needed]" marker in latest assistant turn ---
# v6 (2026-04-30): if the assistant explicitly declared no-KG-needed for
# this batch of work, treat it like a KG write. The marker must appear
# AFTER the current baseline (otherwise it's stale from a previous
# escape that already reset). Cheap to honor — saves the agent from
# having to write a placeholder KG file just to silence the nudge.
escape_hatch_active = (
    escape_marker_token_total > 0
    and escape_marker_token_total > int(current.get("baseline", 0))
)

# --- Branch logic ---
if is_session_compact:
    # /compact (manual) and auto-compaction both fire SessionStart with
    # source=compact. Reset state to "fresh session" — baseline at
    # post-compact session_total, fired_once cleared, so the next nudge
    # waits the full FIRST_THRESHOLD (175k) instead of 50k. Rationale:
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
elif escape_hatch_active:
    # Treat the most recent escape-marker assistant turn as a baseline
    # reset point. The marker's session_total IS the new baseline so
    # subsequent work counts from there.
    current["baseline"] = escape_marker_token_total
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
current["metric_version"] = METRIC_VERSION
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
