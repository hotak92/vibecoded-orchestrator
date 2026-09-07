#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# cost-tracker.sh — Stop hook: append token/cost data to <metrics dir>/costs.jsonl
# Reads Claude's stdin JSON payload (stop event) and logs cost data.
# Fires on every Stop event (end of each Claude response).
#
# Payload format (from Claude Code Stop event):
#   {"session_id":"...","message":{"usage":{"input_tokens":N,"output_tokens":N,"cache_read_input_tokens":N},"model":"..."},...}
#
# Auth mode detection (2026-05-01): claude.ai OAuth subscription has no
# per-token cost — this script was originally written for MAO API-key
# usage. We now record auth_mode in each entry; cost_usd is only
# populated when auth_mode == "api". Subscription rows still log token
# counts (useful for usage analysis) but cost_usd is null.
#
# Output format (costs.jsonl):
#   {"timestamp":"ISO","session_id":"...","model":"...","input_tokens":N,"output_tokens":N,"cache_read_tokens":N,"auth_mode":"api|subscription","cost_usd":N|null}
#
# Portability note (audit F19, 2026-04-30 · reworked v0.2.92 W7): the metrics
# directory is resolved by `_lib/metrics-dir.sh` — the ONE shell-side home for
# that decision, mirrored by `_lib/metrics-dir.ps1` and pinned to
# `vco_lib/paths.py` by a parity test. It honours $VCT_STATE_DIR and falls back
# to $HOME/%USERPROFILE%, so it works on every OS. The resolved path is passed
# INTO the Python below rather than recomputed there: a second resolution in
# the heredoc would be a third copy of the rule, and the pre-v0.2.92 hooks had
# already drifted on the home-directory question alone.

set -euo pipefail

# Read stdin payload
. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"
# Resolve Python portably — bare `python3` is missing on Windows.
# shellcheck source=_lib/find-python.sh disable=SC1091
. "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op

# Metrics home (v0.2.92 W7 — no longer under ~/.claude). Missing helper =>
# silent no-op, the same discipline as the missing-Python case above: a
# partial install must not make the hook guess a path.
_CT_LIB="$(dirname "${BASH_SOURCE[0]}")/_lib/metrics-dir.sh"
[ -f "$_CT_LIB" ] || exit 0
# shellcheck source=_lib/metrics-dir.sh disable=SC1091
. "$_CT_LIB"
COSTS_DIR="$(vco_metrics_dir 2>/dev/null || printf '')"
[ -n "$COSTS_DIR" ] || exit 0

PAYLOAD=$(cat)

# Extract fields using python (resolved portably above)
COSTS_DIR="$COSTS_DIR" "$PY" - <<'PYEOF' "$PAYLOAD"
import sys, json, os, pathlib
from datetime import datetime, timezone

payload_str = sys.argv[1] if len(sys.argv) > 1 else ""
if not payload_str:
    sys.exit(0)

try:
    payload = json.loads(payload_str)
except json.JSONDecodeError:
    sys.exit(0)

# Extract from payload
session_id = payload.get("session_id", "")
message = payload.get("message", {})
model = message.get("model", "unknown")
usage = message.get("usage", {})
input_tokens = usage.get("input_tokens", 0)
output_tokens = usage.get("output_tokens", 0)
cache_read_tokens = usage.get("cache_read_input_tokens", 0)

if input_tokens == 0 and output_tokens == 0:
    sys.exit(0)

# Auth-mode detection. claude.ai OAuth credentials live at
# ~/.claude/credentials*. Presence -> subscription (no per-token cost).
# Absence -> assume API-key billing (cost calc applies). We deliberately
# don't parse the JSON because the credential schema isn't documented
# stable; mere file presence is the signal Claude Code itself uses.
home = pathlib.Path.home()
oauth_paths = list(home.glob(".claude/credentials*"))
auth_mode = "subscription" if oauth_paths else "api"

# Pricing table (per 1M tokens) — update as models change. Only used
# when auth_mode == "api"; subscription tokens are free (included in
# claude.ai subscription).
PRICING = {
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-opus-4-7": (5.00, 25.00),
}

cost_usd = None
if auth_mode == "api":
    input_price, output_price = 3.00, 15.00  # default: sonnet
    for model_key, (ip, op) in PRICING.items():
        if model_key in model:
            input_price, output_price = ip, op
            break
    cost_usd = round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)

record = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "session_id": session_id,
    "model": model,
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
    "cache_read_tokens": cache_read_tokens,
    "auth_mode": auth_mode,
    "cost_usd": cost_usd,
}

# Resolved by _lib/metrics-dir.sh and handed in via the environment — the ONE
# home for the decision. No inline reconstruction here.
metrics_dir = pathlib.Path(os.environ["COSTS_DIR"])
metrics_dir.mkdir(parents=True, exist_ok=True)
costs_file = metrics_dir / "costs.jsonl"

# encoding pinned (v0.2.92 W7): the .ps1 sibling always wrote utf-8 while this
# side used the platform default, so a non-UTF-8 model name would have produced
# two different byte streams for the same record on Git Bash / Windows.
with open(costs_file, "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")
PYEOF
