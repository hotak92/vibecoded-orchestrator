#!/usr/bin/env bash
# post-file-delete.sh — PostToolUse(Bash) hook that detects deletes of
# .mmd / .excalidraw files under .claude/diagrams/ and cascades the
# delete across SQLite + sidecar + Weaviate via
# `vco_lib.diagram_indexer drop <file>`.
#
# Triggers on Bash commands matching ANY segment in a chain whose
# (possibly-wrapped) verb is one of:
#   - `rm`/`unlink`/`mv`          (single + multiple targets)
#   - `Remove-Item`/`Move-Item`   (PowerShell — also handled in .ps1)
# AND whose target path is a .mmd / .excalidraw file under
# .claude/diagrams/. The parser handles chains (`cd /tmp && rm ...`),
# wrapper verbs (`sudo rm`, `nice rm`, `taskset`), env-prefix
# (`KEY=val rm ...`), and `bash -c "rm ..."` sub-commands — see
# `vco_lib/diagram_delete_parser.py` for the full rules + the B4
# regression note documenting why the previous "first-token-only"
# parser missed most real Claude-generated deletes.
#
# Retroactive cleanup of orphans
# ------------------------------
# False negatives (a delete via Python script or some other non-Bash
# path the parser doesn't recognise) can leave stale SQLite rows /
# sidecars / Weaviate objects behind. The canonical "sweep up" path
# for those is `vco rebuild-diagram-index --prune` (re-walks
# .claude/diagrams/, removes index entries for files that no longer
# exist on disk). This replaced the previously-planned
# `cleanup-orphan-diagrams.sh` SessionStart hook — the CLI subcommand
# is on-demand + visible + scriptable, which matched the v0.2.34
# design review's preference for explicit-user-invocation cleanups
# over silent SessionStart sweeps.
#
# False positives are tolerated (`vco_lib.diagram_indexer drop` is
# idempotent — calling it on a path that has no DB row / sidecar /
# Weaviate object is a no-op).
#
# Always exits 0 (never blocks the user's Bash). Silent when no diagram
# delete is detected.

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# stderr cap
if [ -f "$SCRIPT_DIR/_lib/stderr-cap.sh" ]; then
    # shellcheck source=_lib/stderr-cap.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/stderr-cap.sh"
fi

# Python interpreter
if [ -f "$SCRIPT_DIR/_lib/find-python.sh" ]; then
    # shellcheck source=_lib/find-python.sh disable=SC1091
    . "$SCRIPT_DIR/_lib/find-python.sh"
fi
if [ -z "${PY:-}" ]; then
    PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || command -v py 2>/dev/null || true)"
fi
[ -z "${PY:-}" ] && exit 0

PROJECT_ROOT="${CLAUDE_PROJECT_DIR:-$PWD}"

# Read the PostToolUse JSON envelope from stdin. We need `tool_input.command`
# (the executed Bash command string).
HOOK_STDIN=$(cat 2>/dev/null || echo "")
[ -z "$HOOK_STDIN" ] && exit 0

COMMAND=$(printf '%s' "$HOOK_STDIN" | "$PY" -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    cmd = (d.get('tool_input') or {}).get('command', '') or ''
    sys.stdout.write(cmd)
except Exception:
    pass
" 2>/dev/null)

[ -z "$COMMAND" ] && exit 0

# Quick reject: the command must mention .claude/diagrams or .mmd/.excalidraw
# AND a delete-flavoured verb. Saves us from spawning the parser on every
# Bash invocation.
case "$COMMAND" in
    *.claude/diagrams*|*.mmd*|*.excalidraw*) ;;
    *) exit 0 ;;
esac
case "$COMMAND" in
    *rm\ *|*rm\	*|*unlink\ *|*mv\ *) ;;
    *) exit 0 ;;
esac

# Parse the command via vco_lib.diagram_delete_parser. The parser
# module is unit-tested (`tests/test_post_file_delete_parser.py`) so we
# can rely on it to handle chains (`cd /tmp && rm ...`), wrapper verbs
# (`sudo rm`, `nice rm`), and `bash -c "rm ..."` sub-commands. PYTHONPATH
# is set to PROJECT_ROOT so the import resolves even from outside an
# installed package context.
PATHS=$(printf '%s' "$COMMAND" | \
    PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" -m vco_lib.diagram_delete_parser 2>/dev/null)

[ -z "$PATHS" ] && exit 0

# Cascade-delete each detected path. Run with PYTHONPATH set so vco_lib
# is importable even outside an installed package context.
while IFS= read -r path; do
    [ -z "$path" ] && continue
    # Make path absolute relative to PROJECT_ROOT if it's a relative.
    case "$path" in
        /*) abs_path="$path" ;;
        *) abs_path="$PROJECT_ROOT/$path" ;;
    esac
    PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
        "$PY" -m vco_lib.diagram_indexer drop "$abs_path" >/dev/null 2>&1 || true
done <<< "$PATHS"

exit 0
