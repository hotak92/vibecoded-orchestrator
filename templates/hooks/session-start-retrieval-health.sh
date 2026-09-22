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
# Output (the retrieval line, examples):
#   Retrieval: KG 412 nodes, codegraph 3897 functions.
#   Retrieval: KG collection 'X' empty, codegraph not built (no class 'Y_CodeFunction').
#   Retrieval: KG 412 nodes, codegraph unknown (CODE_GRAPH_PROJECT unset).
#   Retrieval: unavailable (weaviate down).
#
# …plus, since v0.2.95, a WRITE-path line and (only when there is one) a
# kg-sync failure notice — see the two blocks at the bottom of this file.
#
# Soft-fail (any error → an "unknown"/"unavailable" line, NEVER an exception /
# never blocks session start; exit 0 always).
#
# COST: two GraphQL Aggregate round-trips with a 0.8s timeout each, plus (from
# v0.2.95) ONE interpreter spawn for the write-path probe — ~0.4s on a warm
# install, once per session. That probe is the SAME `python -c` gate kg-sync
# runs, deliberately: a cheaper approximation (``importlib.util.find_spec``)
# could report a healthy write path for an environment kg-sync then refuses,
# and a probe whose verdict differs from the reader's is the false positive
# this hook's own header spends a paragraph warning about.
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

# MIRROR (category C) — must match vco_lib/weaviate_helpers.py::
# weaviate_url_default, the SHARED HOME. Precedence: WEAVIATE_URL >
# WEAVIATE_PORT > the canonical port; empty/whitespace at either level is
# UNSET, not a literal. A mirror because this probe is stdlib-only Python
# embedded in a hook: it runs under whatever interpreter find-python resolves,
# which is NOT guaranteed to be the VCO venv, so vco_lib may be absent.
# Pinned by tests/test_v0296_weaviate_url_port_precedence.py::
# TestShippedMirrorParity, which EXECUTES these lines against the shared home.
_WEAVIATE_URL_ENV = (os.environ.get("WEAVIATE_URL") or "").strip()
_WEAVIATE_PORT_ENV = (os.environ.get("WEAVIATE_PORT") or "").strip()
WEAVIATE_URL = (_WEAVIATE_URL_ENV or f"http://localhost:{_WEAVIATE_PORT_ENV or 8081}").rstrip("/")
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

# ── KG WRITE path (v0.2.95) ────────────────────────────────────────────────
#
# The two probes above are RETRIEVAL only. Nothing checked whether a knowledge
# node written in this session could reach Weaviate at all — and that is the
# half that failed silently in the field (field report 2026-09-14): `kg-sync` refused
# with exit 3 on every edit for a whole session, behind a `|| true`, while the
# retrieval line kept reporting a healthy (stale) index. Retrieval health is
# not write health; reporting only the first is how the second stays hidden.
#
# No network and no Weaviate write: the question is ONLY "is there an
# interpreter that can run the sync", which is exactly what the shared ladder
# answers. Probe + refusal text come from `vct_venv_ladder.sh` — the same file
# `kg-sync` sources — so this line cannot drift from what the sync will do.
#
# STDOUT, not stderr: Claude Code injects a SessionStart hook's stdout as a
# system-reminder and discards its stderr. The ladder's refusal printer writes
# to stderr (correct for a wrapper), so it is captured and re-emitted here
# rather than re-worded — one home for the prose, two streams.
#
# MUST MATCH session-start-retrieval-health.ps1.
KG_WRITE_PROBE="import weaviate, weaviate_mcp, vco_lib"
LADDER_SCRIPTS_DIR="$(cd "$SCRIPT_DIR/../scripts" 2>/dev/null && pwd || printf '')"
if [ -n "$LADDER_SCRIPTS_DIR" ] && [ -r "$LADDER_SCRIPTS_DIR/vct_venv_ladder.sh" ]; then
    # shellcheck source=../scripts/vct_venv_ladder.sh disable=SC1091
    . "$LADDER_SCRIPTS_DIR/vct_venv_ladder.sh"
    vct_venv_ladder_resolve "$LADDER_SCRIPTS_DIR" "$KG_WRITE_PROBE" || true
    if [ -n "${LADDER_PYTHON:-}" ]; then
        echo "KG write path: OK (${LADDER_PYTHON}, tier: ${LADDER_TIER:-unknown})"
    else
        echo "KG write path: REFUSED — every knowledge/ edit this session will fail to sync."
        vct_venv_ladder_refusal "kg-sync" "$KG_WRITE_PROBE" "$LADDER_SCRIPTS_DIR" 2>&1 \
            | sed 's/^/  /'
    fi
else
    echo "KG write path: unknown (no .claude/scripts/vct_venv_ladder.sh — broken install;"
    echo "  re-run the orchestrator install, or update this project's bundle)."
fi

# The OTHER half of "can this session write": an interpreter that can run the
# sync is useless if the hooks that DECIDE to call it are not on disk. Three
# `_lib/` files carry that decision — routing a touched path, recovering what
# a CLI command wrote, and telling code from prose — and a project missing any
# of them keeps working, silently, minus that whole leg, while the ladder
# probe above still reports OK because the interpreter is fine. That
# combination is the blind spot the v0.2.95 review found twice: MAJOR-1 for
# the routing lib, and MAJOR-2 because the fix (and this probe) covered ONLY
# that one, so this line printed OK while every CLI write was being dropped.
#
# The set is NOT enumerated here: it is `vco_required_hook_libs` in
# `_lib/emit-context.sh`, the same home the in-session notices take their
# wording from, so a fourth required lib is probed without editing this file.
# `vco_hook_lib_role` supplies the consequence sentence per file.
#
# Cheap by construction: one `[ -r ]` test per file, no subprocess.
# MUST MATCH session-start-retrieval-health.ps1.
#
# The project root is resolved here rather than assumed: the printed fix is a
# command the user is meant to RUN, so its `--folder` has to be a real path.
# CLAUDE_PROJECT_DIR first (worktree-isolated sessions), script-relative
# otherwise — the same order post-file-edit.sh and post-tool-security.sh use.
_RH_PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || printf '<project>')}"
# shellcheck source=_lib/emit-context.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/emit-context.sh" ] && . "$SCRIPT_DIR/_lib/emit-context.sh"
if ! command -v vco_required_hook_libs >/dev/null 2>&1; then
    # The file that holds the set is itself missing: that is the same broken
    # install, one layer up, and saying "unknown" beats printing OK about a
    # list we cannot read.
    echo "KG write routing: unknown (no .claude/hooks/_lib/emit-context.sh —"
    echo "  broken install; update this project's bundle to restore it)."
else
    _RH_MISSING=""
    _RH_PRESENT=""
    for _rh_lib in $(vco_required_hook_libs); do
        if [ -r "$SCRIPT_DIR/_lib/${_rh_lib}.sh" ]; then
            if [ -n "$_RH_PRESENT" ]; then
                _RH_PRESENT="$_RH_PRESENT, _lib/${_rh_lib}.sh"
            else
                _RH_PRESENT="_lib/${_rh_lib}.sh"
            fi
        else
            _RH_MISSING="$_RH_MISSING $_rh_lib"
        fi
    done
    if [ -z "$_RH_MISSING" ]; then
        echo "KG write routing: OK ($_RH_PRESENT present)"
    else
        echo "KG write routing: BROKEN — this project's hooks are incomplete."
        for _rh_lib in $_RH_MISSING; do
            echo "  .claude/hooks/_lib/${_rh_lib}.sh is missing or unreadable: it is the"
            echo "  one home for $(vco_hook_lib_role "$_rh_lib")."
        done
        echo "  Fix: python -m vco_lib.project_init install-bundle --folder $_RH_PROJECT_ROOT \\"
        echo "         --orchestrator-root <orchestrator-root> --update"
        echo "  (or the launcher's per-project Settings page → \"Update bundle\")."
    fi
fi

# ── kg-sync failures recorded since the last session (v0.2.95) ─────────────
#
# The writer is `_lib/kg-sync-debounce.sh::_kg_debounce_record_failure`: when a
# debounced sync exits non-zero it appends ONE row per session per channel to
# `kg_sync_failures.jsonl` and keeps the full stderr in
# `.claude/logs/kg-sync-hook.log`. This is the reader — the same
# writer→jsonl→SessionStart-notice shape `embedding-failures-surface.sh` uses
# for embedding fidelity.
#
# Parsed with `$PY` (the plain interpreter `_lib/find-python.sh` found), NOT
# with the VCO venv: the condition being reported is frequently "there is no
# usable VCO venv", so a reader that needed one would go quiet exactly when it
# had something to say.
#
# Deduped by a byte-offset marker, so a row is announced once and a session
# with nothing new prints nothing.
KG_FAIL_JSONL=""
if [ -f "$SCRIPT_DIR/_lib/metrics-dir.sh" ]; then
    # shellcheck source=_lib/metrics-dir.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/metrics-dir.sh"
    KG_FAIL_JSONL="$(vco_metrics_read_file "kg_sync_failures.jsonl" 2>/dev/null || printf '')"
fi
if [ -n "$KG_FAIL_JSONL" ] && [ -f "$KG_FAIL_JSONL" ]; then
    KG_FAIL_ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd || printf '')}"
    KG_FAIL_MARKER="$KG_FAIL_ROOT/.claude/state/kg-sync-failures.seen"
    mkdir -p "$KG_FAIL_ROOT/.claude/state" 2>/dev/null || true
    KG_FAIL_JSONL="$KG_FAIL_JSONL" KG_FAIL_ROOT="$KG_FAIL_ROOT" \
    KG_FAIL_MARKER="$KG_FAIL_MARKER" "$PY" - <<'KGFAILEOF' 2>/dev/null || true
import json
import os

jsonl = os.environ.get("KG_FAIL_JSONL", "")
root = os.environ.get("KG_FAIL_ROOT", "")
marker = os.environ.get("KG_FAIL_MARKER", "")

try:
    size = os.path.getsize(jsonl)
except OSError:
    raise SystemExit(0)

seen = 0
try:
    with open(marker, "r", encoding="utf-8") as fh:
        seen = int((fh.read() or "0").strip() or 0)
except (OSError, ValueError):
    seen = 0
if seen < 0 or seen > size:
    seen = 0            # log rotated / truncated → re-read from the start
if size == seen:
    raise SystemExit(0)

rows = []
try:
    with open(jsonl, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(seen)
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("kind") != "kg_sync_failed":
                continue
            # Only THIS project's rows: the stream is machine-wide.
            if root and row.get("project_root") and row["project_root"] != root:
                continue
            rows.append(row)
except OSError:
    raise SystemExit(0)

# Advance the marker even when every new row belonged to another project —
# they will never become this project's rows, and re-reading them every
# session would be a permanent no-op cost.
try:
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(str(size))
except OSError:
    pass

if not rows:
    raise SystemExit(0)

last = rows[-1]
channels = sorted({str(r.get("channel", "?")) for r in rows})
print(
    "KG sync FAILED %d time(s) since the last session (channel(s): %s; last exit %s)."
    % (len(rows), ", ".join(channels), last.get("exit", "?"))
)
detail = str(last.get("last_stderr", "") or "").strip()
if detail:
    print("  last error: %s" % detail)
log = str(last.get("log", "") or "").strip()
if log:
    print("  full stderr: %s" % log)
print(
    "  Edits to knowledge/ were NOT indexed. Fix the environment (see the "
    "KG write path line above), then re-run `.claude/scripts/kg-sync --all`."
)
KGFAILEOF
fi

exit 0
