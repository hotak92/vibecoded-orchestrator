# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# KG-3 (v0.2.73): SessionStart hook — ONE-LINE retrieval-health status (KG
# reachable + populated, code graph built). MUST MATCH
# session-start-retrieval-health.sh (same GraphQL Aggregate probe, same
# output shapes, same <1s fast + soft-fail-always contract).
#
# The Python payload below is a BYTE-IDENTICAL copy of the .sh sibling's
# heredoc body — pinned by tests/test_retrieval_health_probe_v0292.py, which
# extracts both and compares them. Edit the .sh, then re-copy; do not hand-edit
# one side. The defect this hook shipped with (probing the retired global
# `CodeFunction` class, so every project reported a permanent false "codegraph
# not built") was a .sh/.ps1 SHARED defect — keeping the payload literally
# identical is what stops one side being fixed and the other left behind.

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
import re
import sys
import urllib.request

WEAVIATE_URL = os.environ.get("WEAVIATE_URL", "http://localhost:8081").rstrip("/")
KG_COLLECTION = os.environ.get("KG_COLLECTION", "").strip()
# Authoritative code-graph class prefix — READ from the launcher's env
# projection, never re-derived here (see the header block for why).
CODE_GRAPH_PREFIX = os.environ.get("CODE_GRAPH_PROJECT", "").strip()

# A Weaviate class name is a GraphQL field name. Anything outside this shape
# cannot be interpolated into the query without changing the query's SHAPE, so
# an invalid name is "could not check", never "not built".
_CLASS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

TIMEOUT = 0.8  # keep total well under 1s

OK, ABSENT, ERROR = "ok", "absent", "error"


def _aggregate(class_name):
    """Probe one Weaviate class. Returns a (state, count) pair:

      (OK, n)         class exists in the schema and holds n objects
      (ABSENT, None)  class is not in the schema  -> genuinely not built
      (ERROR, None)   could not check (unreachable / unparseable / other)

    Never raises."""
    if not class_name or not _CLASS_RE.match(class_name):
        return (ERROR, None)
    query = '{ Aggregate { %s { meta { count } } } }' % class_name
    try:
        # Request() itself raises on a malformed WEAVIATE_URL ("unknown url
        # type"), so it belongs INSIDE the guard: a bad env value must degrade
        # to "unknown", not kill the hook before it prints anything.
        req = urllib.request.Request(
            "%s/v1/graphql" % WEAVIATE_URL,
            data=json.dumps({"query": query}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return (ERROR, None)  # transport / parse failure -> could not check
    if not isinstance(payload, dict):
        return (ERROR, None)
    try:
        agg = payload["data"]["Aggregate"][class_name]
    except (KeyError, TypeError, IndexError):
        # No data for this class. A GraphQL VALIDATION error naming the field
        # is Weaviate's way of saying the class is not in the schema
        # ('Cannot query field "X" on type "AggregateObjectsObj"'). Anything
        # else we do not recognise stays ERROR -- conservative by design:
        # never report "not built" on a guess.
        errors = payload.get("errors") or []
        for err in errors:
            try:
                msg = str(err.get("message", ""))
            except Exception:
                continue
            if "Cannot query field" in msg and class_name in msg:
                return (ABSENT, None)
        return (ERROR, None)
    if not agg:
        return (OK, 0)  # class exists in schema but has no objects
    try:
        return (OK, int(agg[0]["meta"]["count"]))
    except (KeyError, TypeError, IndexError, ValueError):
        return (ERROR, None)


# --- KG side ---------------------------------------------------------
if KG_COLLECTION:
    kg_state, kg_count = _aggregate(KG_COLLECTION)
else:
    kg_state, kg_count = (ERROR, None)

# --- code-graph side -------------------------------------------------
# `<prefix>_CodeFunction` when the projection gave us a prefix; the bare legacy
# class when it did not -- that is what a reader with no project context
# queries, so the probe stays equal to the reader in BOTH cases.
if CODE_GRAPH_PREFIX and not _CLASS_RE.match(CODE_GRAPH_PREFIX):
    CODE_CLASS = ""
    code_state, code_count = (ERROR, None)
else:
    CODE_CLASS = (
        "%s_CodeFunction" % CODE_GRAPH_PREFIX if CODE_GRAPH_PREFIX else "CodeFunction"
    )
    code_state, code_count = _aggregate(CODE_CLASS)

# Weaviate is only "down" when BOTH probes actually ran and BOTH failed at the
# transport level. A missing class is NOT a down server (the pre-fix code
# conflated them and reported a live Weaviate as down whenever the KG class was
# absent too, because the codegraph probe could never succeed).
if kg_state == ERROR and code_state == ERROR and KG_COLLECTION and CODE_CLASS:
    print("Retrieval: unavailable (weaviate down).")
    sys.exit(0)

if not KG_COLLECTION:
    kg_part = "KG unknown (KG_COLLECTION unset)"
elif kg_state == OK and kg_count:
    kg_part = "KG %d nodes" % kg_count
elif kg_state == OK:
    kg_part = "KG collection '%s' empty" % KG_COLLECTION
elif kg_state == ABSENT:
    kg_part = "KG collection '%s' missing" % KG_COLLECTION
else:
    kg_part = "KG unknown (probe failed for '%s')" % KG_COLLECTION

if not CODE_CLASS:
    code_part = (
        "codegraph unknown (CODE_GRAPH_PROJECT='%s' is not a valid class prefix)"
        % CODE_GRAPH_PREFIX
    )
elif code_state == OK and code_count:
    code_part = "codegraph %d functions" % code_count
elif code_state == OK:
    code_part = "codegraph empty (class '%s')" % CODE_CLASS
elif code_state == ABSENT and not CODE_GRAPH_PREFIX:
    code_part = (
        "codegraph not built (no class '%s'; CODE_GRAPH_PROJECT unset)" % CODE_CLASS
    )
elif code_state == ABSENT:
    code_part = "codegraph not built (no class '%s')" % CODE_CLASS
else:
    code_part = "codegraph unknown (probe failed for '%s')" % CODE_CLASS

print("Retrieval: %s, %s." % (kg_part, code_part))
'@

try {
    & $PY -c $pyCode 2>$null
} catch { }
exit 0
