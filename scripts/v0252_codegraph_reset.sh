#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# V52-O.2 — Code-graph collection reset + re-walk helper.
#
# Drops the five polluted VibeCodedOrchestrator_Code* collections in
# Weaviate and re-walks both source roots (VCO_dev primary repo +
# vibecoded-orchestrator public clone as --extra-path) with the V52-O.1
# (ignore-set), V52-O.3 (UUID5 cross-root dedup), and V52-O.4
# (file_path property) fixes from chore/v0252-codegraph-indexer-fix
# already applied.
#
# DESTRUCTIVE — requires explicit user confirmation (or --yes for
# automation). A --dry-run flag prints what would happen without
# touching Weaviate.
#
# Usage:
#   scripts/v0252_codegraph_reset.sh            # interactive prompt
#   scripts/v0252_codegraph_reset.sh --dry-run  # show plan, no changes
#   scripts/v0252_codegraph_reset.sh --yes      # skip prompt (CI / scripts)
#
# Prereqs:
#   - Weaviate running at http://localhost:8081 (default port).
#   - .claude/scripts/code-graph-analyze available + executable.
#   - VCO_dev clone present at the expected primary path.
#   - vibecoded-orchestrator public clone present at the expected
#     extra-path (this script's directory's parent, by default).

set -euo pipefail

# --- Config ----------------------------------------------------------------

# Primary repo to walk (VCO_dev) — operational private fork.
PRIMARY_REPO="${V52_PRIMARY_REPO:-/home/martino/Desktop/PROGETTI/VCO_dev}"

# Extra-path (vibecoded-orchestrator public clone). Default = the script's
# repo root, so running from inside the public clone "just works".
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_EXTRA_PATH="$(cd "$SCRIPT_DIR/.." && pwd)"
EXTRA_PATH="${V52_EXTRA_PATH:-$DEFAULT_EXTRA_PATH}"

# Project name used to derive the collection prefix.
PROJECT_NAME="${V52_PROJECT_NAME:-VibeCodedOrchestrator}"

# Weaviate connection (matches the orchestrator default).
WEAVIATE_HOST="${WEAVIATE_HOST:-localhost}"
WEAVIATE_PORT="${WEAVIATE_HTTP_PORT:-8081}"

# Collection names (must match analyze_code_graph.py's collection_name_for()
# output for `project=$PROJECT_NAME`).
COLLECTIONS=(
    "${PROJECT_NAME}_CodeFunction"
    "${PROJECT_NAME}_CodeClass"
    "${PROJECT_NAME}_CodeModule"
    "${PROJECT_NAME}_CodeAPI"
    "${PROJECT_NAME}_CodeInteraction"
)

# --- Arg parsing -----------------------------------------------------------

DRY_RUN=0
SKIP_PROMPT=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y)  SKIP_PROMPT=1 ;;
        -h|--help)
            sed -n '2,40p' "$0"
            exit 0
            ;;
        *)
            echo "ERROR: unknown arg: $arg" >&2
            echo "Run with --help for usage." >&2
            exit 2
            ;;
    esac
done

# --- Plan summary ----------------------------------------------------------

echo "================================================================="
echo "V52-O.2 — Code-graph collection reset + re-walk"
echo "================================================================="
echo "Primary repo:    $PRIMARY_REPO"
echo "Extra-path:      $EXTRA_PATH"
echo "Project name:    $PROJECT_NAME"
echo "Weaviate:        http://${WEAVIATE_HOST}:${WEAVIATE_PORT}"
echo
echo "Will DROP the following Weaviate collections:"
for c in "${COLLECTIONS[@]}"; do
    echo "  - $c"
done
echo
echo "Then re-walk both source roots (~800 source files, ~5min)"
echo "with V52-O.1 + V52-O.3 + V52-O.4 fixes applied."
echo "================================================================="
echo

if [ "$DRY_RUN" = "1" ]; then
    echo "[DRY-RUN] No changes will be made. Exiting."
    exit 0
fi

# --- Pre-flight: validate paths --------------------------------------------

if [ ! -d "$PRIMARY_REPO" ]; then
    echo "ERROR: primary repo not found at $PRIMARY_REPO" >&2
    echo "       Set V52_PRIMARY_REPO env var to override." >&2
    exit 3
fi

if [ ! -d "$EXTRA_PATH" ]; then
    echo "ERROR: extra-path not found at $EXTRA_PATH" >&2
    echo "       Set V52_EXTRA_PATH env var to override." >&2
    exit 3
fi

ANALYZER="$PRIMARY_REPO/.claude/scripts/code-graph-analyze"
if [ ! -x "$ANALYZER" ]; then
    echo "ERROR: code-graph-analyze not executable at $ANALYZER" >&2
    exit 3
fi

# --- Confirmation ----------------------------------------------------------

if [ "$SKIP_PROMPT" != "1" ]; then
    echo "This is DESTRUCTIVE — dropped collections cannot be undone."
    echo "Type 'yes' to proceed:"
    read -r confirm
    if [ "$confirm" != "yes" ]; then
        echo "Aborted."
        exit 1
    fi
fi

# --- Drop collections ------------------------------------------------------

echo
echo "Dropping collections..."

# Locate a Python with the weaviate client. Prefer the orchestrator's MCP
# venv (the analyzer ships with weaviate-client v4), then the system python.
PYTHON=""
for candidate in \
    "$PRIMARY_REPO/claude_mcp_servers/.venv/bin/python" \
    "$EXTRA_PATH/claude_mcp_servers/.venv/bin/python" \
    "$(command -v python3)" \
    "$(command -v python)"; do
    if [ -x "$candidate" ] && "$candidate" -c "import weaviate" 2>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: no python with weaviate-client installed found." >&2
    echo "       Tried MCP venvs + system python." >&2
    exit 4
fi

echo "Using Python: $PYTHON"

WEAVIATE_HOST="$WEAVIATE_HOST" WEAVIATE_PORT="$WEAVIATE_PORT" \
COLLS="${COLLECTIONS[*]}" \
"$PYTHON" - <<'PY'
import os, sys
import weaviate
host = os.environ.get("WEAVIATE_HOST", "localhost")
port = int(os.environ.get("WEAVIATE_PORT", "8081"))
colls = os.environ.get("COLLS", "").split()
client = weaviate.connect_to_local(host=host, port=port)
try:
    for coll in colls:
        try:
            if client.collections.exists(coll):
                client.collections.delete(coll)
                print(f"  dropped {coll}")
            else:
                print(f"  skip {coll} (not present)")
        except Exception as e:
            print(f"  ERROR dropping {coll}: {e}", file=sys.stderr)
finally:
    client.close()
PY

# --- Re-walk both source roots --------------------------------------------

echo
echo "Re-walking primary repo + extra-path..."
echo "(this re-creates schemas + populates rows)"
echo

"$ANALYZER" "$PRIMARY_REPO" \
    --project "$PROJECT_NAME" \
    --extra-path "$EXTRA_PATH"

echo
echo "================================================================="
echo "V52-O.2 reset + re-walk complete."
echo "================================================================="
