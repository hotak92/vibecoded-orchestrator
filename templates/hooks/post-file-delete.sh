#!/usr/bin/env bash
# post-file-delete.sh — PostToolUse(Bash) hook that detects deletes of
# .mmd / .excalidraw files under .claude/diagrams/ and cascades the
# delete across SQLite + sidecar + Weaviate via
# `vco_lib.diagram_indexer drop <file>`.
#
# Triggers on Bash commands matching:
#   - `rm [-flags] <path>`        (single + multiple targets)
#   - `unlink <path>`
#   - `mv <src> <dest>`           (when src is under .claude/diagrams/)
#
# False positives are tolerated (`vco_lib.diagram_indexer drop` is
# idempotent — calling it on a path that has no DB row / sidecar /
# Weaviate object is a no-op). False negatives — a delete via Python
# script or other non-Bash path — are caught by the session-start
# `cleanup-orphan-diagrams.sh` hook (sweeps for sidecars whose target
# file no longer exists).
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

# Parse the command to extract candidate paths. Pure-Python parser
# avoids bash word-splitting surprises with quoted paths + globs.
PATHS=$(printf '%s' "$COMMAND" | "$PY" -c "
import json, shlex, sys, glob, os
cmd = sys.stdin.read().strip()
# Strip leading env assignments + sudo etc — find the actual verb.
try:
    tokens = shlex.split(cmd, posix=True)
except ValueError:
    sys.exit(0)
if not tokens:
    sys.exit(0)
# Skip leading env-style 'KEY=VAL' assignments.
i = 0
while i < len(tokens) and '=' in tokens[i] and not tokens[i].startswith('-'):
    eq = tokens[i].index('=')
    head = tokens[i][:eq]
    if head and head.replace('_','').isalnum() and head[0].isalpha():
        i += 1
        continue
    break
if i >= len(tokens):
    sys.exit(0)
# Support multi-command chains separated by ; && ||. We only inspect
# the first verb to keep matching tight; a chain that deletes diagrams
# in the second clause is missed (caught by session-start sweeper).
verb = os.path.basename(tokens[i])
if verb not in ('rm', 'unlink', 'mv'):
    sys.exit(0)
# Collect positional arguments (skip flags).
args = []
for tok in tokens[i+1:]:
    if tok.startswith('-'):
        # rm -rf, -f, -i, etc. — skip
        continue
    if tok in (';', '&&', '||', '|'):
        break
    args.append(tok)
# For 'mv', only the SOURCE (first positional) counts as a delete.
# Destination is irrelevant — if dest is also under .claude/diagrams/
# the post-file-edit hook will re-index it.
if verb == 'mv' and len(args) >= 2:
    args = args[:1]
# Expand globs (the shell already did this for the actual rm, but we
# need to enumerate too).
expanded = []
for a in args:
    if any(c in a for c in '*?['):
        expanded.extend(glob.glob(a))
    else:
        expanded.append(a)
# Filter to .mmd / .excalidraw under .claude/diagrams.
for p in expanded:
    if not p:
        continue
    if not p.endswith('.mmd') and not p.endswith('.excalidraw'):
        continue
    norm = os.path.normpath(p)
    if '.claude/diagrams/' not in norm and '.claude' + os.sep + 'diagrams' + os.sep not in norm:
        continue
    print(norm)
" 2>/dev/null)

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
