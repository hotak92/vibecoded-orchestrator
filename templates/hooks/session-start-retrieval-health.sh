#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# KG-3 (v0.2.73): SessionStart hook that reports a ONE-LINE retrieval-health
# status — is the KG collection reachable + populated, is the code graph
# built. Answers the "did retrieval silently degrade?" question that D-map
# finding R8 flagged (schema-missing / all-below-floor warnings go to MCP
# stderr, which the hook contract drops — the user never sees them).
#
# Output (exactly one line, examples):
#   Retrieval: KG 412 nodes, codegraph 3897 functions.
#   Retrieval: KG collection 'X' empty/missing; codegraph 3897 functions.
#   Retrieval: unavailable (weaviate down).
#
# Fast (<1s: a single GraphQL Aggregate round-trip with a 0.8s timeout) and
# soft-fail (any error → the "unavailable" line, NEVER an exception / never
# blocks session start; exit 0 always).
#
# Collection names resolve from env (the per-project .claude/settings.json
# env channel sets KG_COLLECTION / CODE_GRAPH_PROJECT / WEAVIATE_URL for the
# Claude session and its hooks). Missing env → sane defaults; a wrong guess
# just reports the collection as empty/missing, never crashes.
#
# MUST MATCH session-start-retrieval-health.ps1.

# Scrub sensitive env before any subprocess (canonical HK-2 list — MUST
# MATCH _lib/scrub-env.sh; enforced by the scrub parity gate).
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # no Python → silent no-op (stdlib-only script)

"$PY" - <<'PYEOF' 2>/dev/null || true
import json
import os
import sys
import urllib.request

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8081").rstrip("/")
KG_COLLECTION = os.environ.get("KG_COLLECTION", "")
# Code graph functions live in the `CodeFunction` class (shared across
# projects; per-project filtering is done at query time via a prefix, but a
# raw count is a fine "is the code graph built at all" signal).
CODE_CLASS = "CodeFunction"

TIMEOUT = 0.8  # keep total well under 1s


def _aggregate_count(class_name: str):
    """Return the object count for a Weaviate class, or None on any error
    (unreachable, schema-missing, malformed response). Never raises."""
    if not class_name:
        return None
    query = '{ Aggregate { %s { meta { count } } } }' % class_name
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        f"{WEAVIATE_URL}/v1/graphql",
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
            return 0  # class exists in schema but has no objects
        return int(agg[0]["meta"]["count"])
    except (KeyError, TypeError, IndexError, ValueError):
        # class not in schema (GraphQL error) → treat as "not built".
        return None


kg_count = _aggregate_count(KG_COLLECTION)
code_count = _aggregate_count(CODE_CLASS)

# If BOTH probes failed at the transport level, Weaviate is down.
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
PYEOF

exit 0
