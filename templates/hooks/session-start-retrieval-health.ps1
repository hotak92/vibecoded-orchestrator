# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# KG-3 (v0.2.73): SessionStart hook — ONE-LINE retrieval-health status (KG
# reachable + populated, code graph built). MUST MATCH
# session-start-retrieval-health.sh (same GraphQL Aggregate probe, same
# output shapes, same <1s fast + soft-fail-always contract).

$PSScriptRootLocal = $PSScriptRoot
$LibDir = Join-Path $PSScriptRootLocal "_lib"

$ScrubLib = Join-Path $LibDir "scrub-env.ps1"
if (Test-Path $ScrubLib) {
    . $ScrubLib
    Invoke-VctScrubSecretEnv
} else {
    foreach ($v in 'SUPABASE_KEY','SUPABASE_URL','GITHUB_TOKEN','GH_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','AWS_SECRET_ACCESS_KEY','AWS_ACCESS_KEY_ID','TELEGRAM_BOT_TOKEN','POSTGRES_PASSWORD','VERCEL_TOKEN','CLAUDE_API_KEY') {
        if (Test-Path "Env:$v") { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }
    }
}

if ($env:VCT_DISABLE_HOOKS) { exit 0 }

$FindPy = Join-Path $LibDir "find-python.ps1"
if (Test-Path $FindPy) { . $FindPy }
if (-not $PY) { exit 0 }

$pyCode = @'
import json
import os
import sys
import urllib.request

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8081").rstrip("/")
KG_COLLECTION = os.environ.get("KG_COLLECTION", "")
CODE_CLASS = "CodeFunction"
TIMEOUT = 0.8


def _aggregate_count(class_name):
    if not class_name:
        return None
    query = '{ Aggregate { %s { meta { count } } } }' % class_name
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        WEAVIATE_URL + "/v1/graphql",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        agg = payload["data"]["Aggregate"][class_name]
        if not agg:
            return 0
        return int(agg[0]["meta"]["count"])
    except (KeyError, TypeError, IndexError, ValueError):
        return None


kg_count = _aggregate_count(KG_COLLECTION)
code_count = _aggregate_count(CODE_CLASS)

if kg_count is None and code_count is None:
    print("Retrieval: unavailable (weaviate down).")
    sys.exit(0)

if kg_count is None:
    kg_part = "KG collection '%s' empty/missing" % (KG_COLLECTION or "<unset>")
elif kg_count == 0:
    kg_part = "KG collection '%s' empty" % (KG_COLLECTION or "<unset>")
else:
    kg_part = "KG %d nodes" % kg_count

if code_count is None:
    code_part = "codegraph not built"
elif code_count == 0:
    code_part = "codegraph empty"
else:
    code_part = "codegraph %d functions" % code_count

print("Retrieval: %s, %s." % (kg_part, code_part))
'@

try {
    & $PY -c $pyCode 2>$null
} catch { }
exit 0
