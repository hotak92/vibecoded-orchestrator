#!/bin/bash
# Run one bounded headless Claude Code task billed to the Claude Console API
# key, and report what it cost (v0.2.104). Typical use: a final code review
# with Fable on API credits instead of the Claude subscription.
#
# Usage:
#   bash .claude/scripts/claude-api-run.sh --prompt-file review-prompt.md \
#       --report review.md [--model claude-fable-5-1] [--effort high] \
#       [--max-budget-usd 25] [--max-turns N] [--policy review|edit] \
#       [--cwd DIR] [--key-name NAME] [--json]
#
# The key is VCO's Shared secret ANTHROPIC_API_KEY_GATEWAY (Secrets panel,
# Shared scope), passed only in the child's environment. Each run appends one
# line (no prompt, no key) to <VCO metrics dir>/claude_api_runs.jsonl.
#
# Exit codes: whatever `python -m vco_lib.claude_api_run` exits (0 ok, 1 the
# run failed, 2 usage, 3 refused before spawning); 3 also = REFUSAL - no
# Python environment with vco_lib (wrapper-only).
# Every rule lives in ONE home (`vco_lib/claude_api_run.py`); this wrapper is
# a dumb forwarder that names the project it is shipped in as the requester
# of the key (`--project`; a later `--project` on the command line wins).
#
# PARITY: this file and `claude-api-run.ps1` must stay in lockstep (same
# tiers, same refusal text, same forwarding).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The probe a candidate interpreter must pass, verbatim as it is executed.
# `vco_lib.claude_api_run` needs only the standard library plus vco_lib.
LADDER_IMPORT="import vco_lib"

# The ladder lives in ONE home, shipped beside this wrapper. A missing lib is
# a broken install, not a reason to fall back to a bare interpreter.
if [ ! -r "$SCRIPT_DIR/vct_venv_ladder.sh" ]; then
    echo "claude-api-run: ERROR - missing $SCRIPT_DIR/vct_venv_ladder.sh" >&2
    echo "claude-api-run: (broken install - re-run the orchestrator install)" >&2
    echo "⚠️  claude-api-run did NOT run (exit 3): its shared venv ladder is missing. See the lines above." >&2
    exit 3
fi
# shellcheck source=vct_venv_ladder.sh
. "$SCRIPT_DIR/vct_venv_ladder.sh"

vct_venv_ladder_resolve "$SCRIPT_DIR" "$LADDER_IMPORT"

# NO BARE `python` FALLBACK - a shipped component does not get a silent
# fallback (standing rule). PARITY: the .ps1 sibling refuses identically.
if [ -z "$LADDER_PYTHON" ]; then
    vct_venv_ladder_refusal "claude-api-run" "$LADDER_IMPORT" "$SCRIPT_DIR"
    {
        echo "claude-api-run: Refusing to run with an unqualified interpreter -"
        echo "claude-api-run: nothing was spawned and nothing was billed."
    } >&2
    vct_venv_ladder_refusal_summary "claude-api-run" 3
    exit 3
fi

# The project this wrapper ships in (.claude/scripts/ -> project root).
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
exec "$LADDER_PYTHON" -m vco_lib.claude_api_run --project "$PROJECT_ROOT" "$@"
