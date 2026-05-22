# parity-confirmation 2026-05-10: .sh sibling now uses _lib/find-python.sh; this .ps1 already used _lib/find-python.ps1 — true parity, no asymmetric fix.
# Scrub sensitive env vars before any subprocess spawning
foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
    if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
}
if ($env:VCT_DISABLE_HOOKS) { exit 0 }
# cost-tracker.ps1 — Stop hook: append token/cost data to ~/.claude/metrics/costs.jsonl
# Reads Claude's stdin JSON payload (stop event) and logs cost data.
# Mirror of cost-tracker.sh; delegates the heavy lifting to a Python heredoc
# inline so we keep parity with the bash version's pricing table.
#
# Auth mode detection (2026-05-01): claude.ai OAuth subscription has no
# per-token cost. We record auth_mode in each entry; cost_usd is only
# populated when auth_mode == "api". Subscription rows still log token
# counts (useful for usage analysis) but cost_usd is null.

. "$PSScriptRoot/_lib/stderr-cap.ps1"

$LibDir = Join-Path $PSScriptRoot "_lib"
$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }

$Payload = ""
try { $Payload = [Console]::In.ReadToEnd() } catch { }
if (-not $Payload) { exit 0 }

# Pass the payload as a single argv element to a small Python program
# (matches the cost-tracker.sh approach of `python3 - "$PAYLOAD"`).
$pyCode = @'
import sys, json, pathlib
from datetime import datetime, timezone

payload_str = sys.argv[1] if len(sys.argv) > 1 else ""
if not payload_str:
    sys.exit(0)
try:
    payload = json.loads(payload_str)
except json.JSONDecodeError:
    sys.exit(0)

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
# Absence -> assume API-key billing. Mere file presence is the signal
# Claude Code itself uses; we don't parse the (undocumented) schema.
home = pathlib.Path.home()
oauth_paths = list(home.glob(".claude/credentials*"))
auth_mode = "subscription" if oauth_paths else "api"

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
    input_price, output_price = 3.00, 15.00
    for k, (ip, op) in PRICING.items():
        if k in model:
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

metrics_dir = pathlib.Path.home() / ".claude" / "metrics"
metrics_dir.mkdir(parents=True, exist_ok=True)
with open(metrics_dir / "costs.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\n")
'@

try {
    & $PY -c $pyCode $Payload 2>$null | Out-Null
} catch { }
exit 0
