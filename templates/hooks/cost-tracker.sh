#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# cost-tracker.sh — Stop hook: append token/cost data to ~/.claude/metrics/costs.jsonl
# Reads Claude's stdin JSON payload (stop event) and logs cost data.
# Fires on every Stop event (end of each Claude response).
#
# Payload format (from Claude Code Stop event):
#   {"session_id":"...","message":{"usage":{"input_tokens":N,"output_tokens":N,"cache_read_input_tokens":N},"model":"..."},...}
#
# Output format (costs.jsonl):
#   {"timestamp":"ISO","session_id":"...","model":"...","input_tokens":N,"output_tokens":N,"cache_read_tokens":N,"cost_usd":N}

set -euo pipefail

# Read stdin payload
PAYLOAD=$(cat)

# Extract fields using python (already in PATH)
python3 - <<'PYEOF' "$PAYLOAD"
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

# Pricing table (per 1M tokens) — update as models change
PRICING = {
    "claude-haiku-4-5": (0.80, 4.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-5": (15.00, 75.00),
    "claude-opus-4-6": (15.00, 75.00),
    "claude-opus-4-7": (5.00, 25.00),
}

# Find matching price (prefix match)
input_price, output_price = 3.00, 15.00  # default: sonnet
for model_key, (ip, op) in PRICING.items():
    if model_key in model:
        input_price, output_price = ip, op
        break

cost_usd = (input_tokens * input_price + output_tokens * output_price) / 1_000_000

record = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "session_id": session_id,
    "model": model,
    "input_tokens": input_tokens,
    "output_tokens": output_tokens,
    "cache_read_tokens": cache_read_tokens,
    "cost_usd": round(cost_usd, 6),
}

metrics_dir = pathlib.Path.home() / ".claude" / "metrics"
metrics_dir.mkdir(parents=True, exist_ok=True)
costs_file = metrics_dir / "costs.jsonl"

with open(costs_file, "a") as f:
    f.write(json.dumps(record) + "\n")
PYEOF
