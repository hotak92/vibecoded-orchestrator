#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# py-compile-check.sh — PostToolUse syntax check for Python writes.
#
# P2a (v0.2.75): replaces the INLINE `python3 -c "...py_compile..."` entry
# that lived at settings.json.linux.template:397. That inline entry was the
# ONLY hook registration without a VCT_DISABLE_HOOKS guard (so the
# documented per-shell opt-out was incomplete — D-5 empirically compiled
# files while "disabled"), ran with UNSCRUBBED env, was not basename-
# supersedable by a bundle update, and printed a MISLEADING "Syntax error"
# on ANY failure — including malformed stdin, which is not a syntax error at
# all. This named sibling fixes all four: self-guarded above, env-scrubbed
# above, basename-supersedable, and accurate error text below.
#
# Fires after Write on *.py (matcher gated in settings.json). Non-blocking:
# a compile failure is surfaced to the model as text, never an exit-2 block.
# MUST MATCH templates/hooks/py-compile-check.ps1.

# Resolve a Python interpreter portably (python3 → python → py). Windows
# ships python.exe / py but not python3.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh" ] && . "$(dirname "${BASH_SOURCE[0]}")/_lib/find-python.sh"
[ -z "${PY:-}" ] && exit 0  # No Python — silent no-op (nothing to check with)

# Hook input arrives as JSON on stdin per Claude Code v2.1.x spec.
HOOK_STDIN=$(cat 2>/dev/null || echo "")

# Parse the edited file_path AND distinguish the two failure classes the
# inline version conflated:
#   * MALFORMED STDIN (not valid JSON / no file_path) → nothing to compile,
#     exit 0 silently. This is NOT a syntax error.
#   * a real py_compile failure → surface the actual compiler message.
# The Python child prints one of:
#   ""            → no file to check (malformed/absent file_path) → exit 0
#   "OK"          → compiled clean
#   "ERR:<msg>"   → real syntax/compile error with the true message
RESULT=$(printf '%s' "$HOOK_STDIN" | "$PY" -c '
import json, sys, py_compile
try:
    d = json.loads(sys.stdin.read())
except Exception:
    # Malformed stdin — NOT a syntax error. Emit nothing → caller exits 0.
    sys.exit(0)
fp = (d.get("tool_input") or {}).get("file_path", "") if isinstance(d, dict) else ""
if not fp:
    sys.exit(0)
if not fp.endswith(".py"):
    # v0.2.80: the settings matcher is bare `Write` (the old `if:
    # "Write(*.py)"` never matched — hook-if regression fix), so the
    # .py gate lives HERE. Without it every non-Python Write produced a
    # false "SyntaxError" and compiling a Python-parsable non-.py file
    # dropped __pycache__ next to user files (final-review B1).
    sys.exit(0)
try:
    py_compile.compile(fp, doraise=True)
    sys.stdout.write("OK")
except py_compile.PyCompileError as e:
    # Real syntax/compile error — surface the TRUE message, not a lie.
    sys.stdout.write("ERR:" + str(e).strip().replace("\n", " "))
except FileNotFoundError:
    # File vanished between write and hook — nothing to report.
    sys.exit(0)
except Exception as e:
    sys.stdout.write("ERR:" + str(e).strip().replace("\n", " "))
' 2>/dev/null || echo "")

case "$RESULT" in
    ERR:*)
        printf 'py_compile: %s\n' "${RESULT#ERR:}"
        ;;
    *)
        : # OK or empty (nothing to check) — say nothing.
        ;;
esac
exit 0
