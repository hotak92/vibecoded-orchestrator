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
#   Retrieval: KG collection 'X' empty, codegraph not built (no class 'Y_CodeFunction').
#   Retrieval: KG 412 nodes, codegraph unknown (CODE_GRAPH_PROJECT unset).
#   Retrieval: unavailable (weaviate down).
#
# Fast (<1s: two GraphQL Aggregate round-trips with a 0.8s timeout each) and
# soft-fail (any error → an "unknown"/"unavailable" line, NEVER an exception /
# never blocks session start; exit 0 always).
#
# THREE-STATE CONTRACT (v0.2.92 — the KG-3 correctness fix). A probe has to
# distinguish three outcomes that the pre-fix code collapsed into one:
#
#   built      the class exists and holds objects   → "N functions" / "N nodes"
#   NOT built  the class is absent from the schema  → "not built" / "missing"
#   UNKNOWN    the probe could not run at all       → "unknown (...)"
#
# The pre-fix code mapped BOTH "absent from schema" AND "request failed" onto
# `None` and printed "not built" — so a check that could not be performed was
# indistinguishable from a project that genuinely has no graph, and a live
# Weaviate missing only the KG class was reported as "weaviate down".
#
# WHICH CLASS TO PROBE (the actual v0.2.92 defect). Until the per-project
# code-graph rollout the graph lived in ONE global `CodeFunction` class; it has
# been PER-PROJECT (`<prefix>_CodeFunction`) ever since, and this hook was never
# updated — so it probed a class from a retired naming generation, the aggregate
# errored, and EVERY project on EVERY install reported a permanent "codegraph
# not built" regardless of its real state.
#
# The prefix is READ, never re-derived: `CODE_GRAPH_PROJECT` already carries the
# authoritative `project_codegraph_bindings.collection_prefix` (see
# `vco_lib/config_projection.py`) — the same value the analyzer WROTE the classes
# with and the same value `query_code_graph.py` / the MCP read back. Re-deriving
# it here (from `KG_COLLECTION`, from a project name, from the folder basename)
# would apply the WRONG sanitizer: KG collection names DROP underscores,
# code-graph class names PRESERVE them. That exact mismatch is the bug family
# that told a project to drop its own live collections. Read the projection; do
# not recompute it.
#
# The probe therefore mirrors what the READERS query: if this line says the graph
# is not built, a `code-graph-query` / `search_code_graph` call in the same
# environment returns nothing. That equivalence is the point — a probe that
# reports on a class no reader queries (a bare `CodeFunction`, or "some other
# project on this machine has a graph") would be a false POSITIVE, which is
# exactly as useless as the false negative it replaced.
#
# Collection names resolve from env (the per-project .claude/settings.json env
# channel sets KG_COLLECTION / CODE_GRAPH_PROJECT / WEAVIATE_URL for the Claude
# session and its hooks). Missing env → "unknown", never a guess.
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
PYEOF

exit 0
