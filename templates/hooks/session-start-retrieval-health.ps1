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

# -- KG WRITE path (v0.2.95) ------------------------------------------------
#
# The two probes above are RETRIEVAL only. Nothing checked whether a knowledge
# node written in this session could reach Weaviate at all - and that is the
# half that failed silently in the field (field report 2026-09-14): `kg-sync` refused
# with exit 3 on every edit for a whole session, behind a swallowed failure,
# while the retrieval line kept reporting a healthy (stale) index.
#
# No network and no Weaviate write: the question is ONLY "is there an
# interpreter that can run the sync", which the shared ladder answers. Probe +
# refusal text come from `vct_venv_ladder.ps1` - the same file `kg-sync.ps1`
# dot-sources - so this line cannot drift from what the sync will do.
#
# STDOUT, not stderr: Claude Code injects a SessionStart hook's stdout as a
# system-reminder and discards its stderr. The ladder's refusal printer writes
# to stderr (correct for a wrapper), so it is captured and re-emitted here
# rather than re-worded - one home for the prose, two streams.
#
# MUST MATCH session-start-retrieval-health.sh.
$KgWriteProbe = "import weaviate, weaviate_mcp, vco_lib"
$LadderScriptsDir = Join-Path (Split-Path -Parent $PSScriptRootLocal) "scripts"
$LadderLib = Join-Path $LadderScriptsDir "vct_venv_ladder.ps1"
$ProjectRootLocal = Split-Path -Parent (Split-Path -Parent $PSScriptRootLocal)
if (Test-Path -LiteralPath $LadderLib) {
    try {
        . $LadderLib
        $LadderPython = Resolve-VctLadderPython -ScriptDir $LadderScriptsDir `
            -ProjectRoot $ProjectRootLocal -ImportProbe $KgWriteProbe
        if ($LadderPython) {
            $tier = if ($script:VctLadderTier) { $script:VctLadderTier } else { "unknown" }
            Write-Output "KG write path: OK ($LadderPython, tier: $tier)"
        } else {
            Write-Output "KG write path: REFUSED - every knowledge/ edit this session will fail to sync."
            $refusal = (Write-VctLadderRefusal -Tool "kg-sync" -ImportProbe $KgWriteProbe `
                -ScriptDir $LadderScriptsDir -ProjectRoot $ProjectRootLocal) 2>&1
            foreach ($line in @($refusal)) { Write-Output ("  " + $line) }
        }
    } catch {
        Write-Output "KG write path: unknown (the venv ladder could not be evaluated: $_)"
    }
} else {
    Write-Output "KG write path: unknown (no .claude\scripts\vct_venv_ladder.ps1 - broken install;"
    Write-Output "  re-run the orchestrator install, or update this project's bundle)."
}

# The OTHER half of "can this session write": an interpreter that can run the
# sync is useless if the hooks that DECIDE to call it are not on disk. Three
# `_lib/` files carry that decision (routing a touched path, recovering what a
# CLI command wrote, telling code from prose), and a project missing any of
# them keeps working minus that whole leg while the ladder probe above still
# reports OK. That blind spot is the v0.2.95 review's MAJOR-1, and MAJOR-2
# because this probe first covered only one of the three.
#
# The set is NOT enumerated here: it is Get-VcoRequiredHookLibs in
# `_lib/emit-context.ps1`, the same home the in-session notices take their
# wording from. Cheap: one Test-Path per file, no subprocess.
# MUST MATCH session-start-retrieval-health.sh.
$EmitContextLibPath = Join-Path $PSScriptRootLocal "_lib/emit-context.ps1"
if (Test-Path $EmitContextLibPath) { . $EmitContextLibPath }
if (-not (Get-Command Get-VcoRequiredHookLibs -ErrorAction SilentlyContinue)) {
    # The file that holds the set is itself missing: the same broken install,
    # one layer up. "unknown" beats printing OK about a list we cannot read.
    Write-Output "KG write routing: unknown (no .claude\hooks\_lib\emit-context.ps1 -"
    Write-Output "  broken install; update this project's bundle to restore it)."
} else {
    $RhMissing = @()
    $RhPresent = @()
    foreach ($rhLib in (Get-VcoRequiredHookLibs)) {
        if (Test-Path (Join-Path $PSScriptRootLocal "_lib/$rhLib.ps1")) {
            $RhPresent += "_lib/$rhLib.ps1"
        } else {
            $RhMissing += $rhLib
        }
    }
    if ($RhMissing.Count -eq 0) {
        Write-Output ("KG write routing: OK ({0} present)" -f ($RhPresent -join ", "))
    } else {
        Write-Output "KG write routing: BROKEN - this project's hooks are incomplete."
        foreach ($rhLib in $RhMissing) {
            Write-Output "  .claude\hooks\_lib\$rhLib.ps1 is missing or unreadable: it is the"
            Write-Output ("  one home for {0}." -f (Get-VcoHookLibRole $rhLib))
        }
        Write-Output "  Fix: python -m vco_lib.project_init install-bundle --folder $ProjectRootLocal ``"
        Write-Output "         --orchestrator-root <orchestrator-root> --update"
        Write-Output "  (or the launcher's per-project Settings page -> ""Update bundle"")."
    }
}

# -- kg-sync failures recorded since the last session (v0.2.95) -------------
#
# Reader for the rows `_lib/kg-sync-debounce.ps1::Write-KgDebounceFailureRow`
# appends when a debounced sync exits non-zero. Same
# writer->jsonl->SessionStart-notice shape `embedding-failures-surface.ps1`
# uses for embedding fidelity.
#
# The payload below is a BYTE-IDENTICAL copy of the .sh sibling's KGFAILEOF
# heredoc body, for the same reason $pyCode is: a shared defect must not be
# fixable on one side only. Edit the .sh, then re-copy.
#
# Parsed with $PY (the plain interpreter `_lib/find-python.ps1` found), NOT
# with the VCO venv: the condition being reported is frequently "there is no
# usable VCO venv", so a reader that needed one would go quiet exactly when it
# had something to say.
$kgFailCode = @'
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
'@

$MetricsLib = Join-Path $LibDir "metrics-dir.ps1"
if (Test-Path -LiteralPath $MetricsLib) {
    try {
        . $MetricsLib
        $KgFailJsonl = Get-VcoMetricsReadFile -Name "kg_sync_failures.jsonl"
        if ($KgFailJsonl -and (Test-Path -LiteralPath $KgFailJsonl -PathType Leaf)) {
            $KgFailRoot = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { $ProjectRootLocal }
            $KgFailState = Join-Path $KgFailRoot ".claude/state"
            if (-not (Test-Path -LiteralPath $KgFailState -PathType Container)) {
                New-Item -ItemType Directory -Force -Path $KgFailState -ErrorAction SilentlyContinue | Out-Null
            }
            $env:KG_FAIL_JSONL = $KgFailJsonl
            $env:KG_FAIL_ROOT = $KgFailRoot
            $env:KG_FAIL_MARKER = Join-Path $KgFailState "kg-sync-failures.seen"
            & $PY -c $kgFailCode 2>$null
        }
    } catch { }
}

exit 0
