#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# V52-O.2 — Code-graph collection reset + re-walk helper.
#
# Drops the five polluted Code* collections in Weaviate and re-walks
# one or two source roots with the V52-O.1 (ignore-set), V52-O.3 (UUID5
# cross-root dedup), and V52-O.4 (file_path property) fixes applied.
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
# Configuration (env-driven; sensible defaults for fresh installs):
#   V52_PRIMARY_REPO    — primary repo to walk (default: this script's repo root)
#   V52_EXTRA_PATH      — optional extra-path (default: unset, walks primary only)
#   V52_PROJECT_NAME    — project name used to derive collection prefix
#                         (default: VibeCodedOrchestrator)
#   WEAVIATE_HOST       — default localhost
#   WEAVIATE_HTTP_PORT  — default 8081
#   WEAVIATE_GRPC_PORT  — default 50052 (NOT the upstream-default 50051;
#                         see knowledge/tools/weaviate-grpc-port-50052-gotcha.md)
#
# Prereqs:
#   - Weaviate running at http://localhost:8081 (default port).
#   - .claude/scripts/code-graph-analyze available + executable.

set -euo pipefail

# --- Config ----------------------------------------------------------------

# Primary repo to walk. Default: this script's repo root (i.e. the
# checkout the operator runs the script from). Override with
# V52_PRIMARY_REPO env var when an out-of-tree repo is the intended target.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_PRIMARY_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
PRIMARY_REPO="${V52_PRIMARY_REPO:-$DEFAULT_PRIMARY_REPO}"

# Optional extra-path (e.g. dual-clone setups where the operator wants
# to walk an additional source tree in the same rewalk). Unset by
# default; set V52_EXTRA_PATH to enable.
EXTRA_PATH="${V52_EXTRA_PATH:-}"

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

# V52-O.11.L (v0.2.52, 2026-06-09): also drop legacy snapshot collections
# left behind by v0.2.43-era code-graph exploration work. Audit found
# these lurking — they're not produced by any current analyzer code path,
# they bloat Weaviate disk + memory, and they show up in collection
# listings polluting the operator's mental model. Drop on first reset.
LEGACY_COLLECTIONS=(
    "Vco_v0243_A_install_CodeFunction"
    "Vco_v0243_A_install_CodeClass"
    "Vco_v0243_A_install_CodeModule"
    "Vco_v0243_A_install_CodeAPI"
    "Vco_v0243_A_install_CodeInteraction"
    "Vco_v0243_B_rust_CodeFunction"
    "Vco_v0243_B_rust_CodeClass"
    "Vco_v0243_B_rust_CodeModule"
    "Vco_v0243_B_rust_CodeAPI"
    "Vco_v0243_B_rust_CodeInteraction"
    "Vco_v0243_C_cleanup_CodeFunction"
    "Vco_v0243_C_cleanup_CodeClass"
    "Vco_v0243_C_cleanup_CodeModule"
    "Vco_v0243_C_cleanup_CodeAPI"
    "Vco_v0243_C_cleanup_CodeInteraction"
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
echo "Extra-path:      ${EXTRA_PATH:-(none — single-root walk)}"
echo "Project name:    $PROJECT_NAME"
echo "Weaviate:        http://${WEAVIATE_HOST}:${WEAVIATE_PORT}"
echo
echo "Will DROP the following Weaviate collections:"
for c in "${COLLECTIONS[@]}"; do
    echo "  - $c"
done
echo
echo "Will also DROP these v0.2.43-era legacy snapshot collections"
echo "(left behind by old code-graph exploration work — V52-O.11.L):"
for c in "${LEGACY_COLLECTIONS[@]}"; do
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

if [ -n "$EXTRA_PATH" ] && [ ! -d "$EXTRA_PATH" ]; then
    echo "ERROR: V52_EXTRA_PATH set to '$EXTRA_PATH' but directory not found" >&2
    echo "       Unset V52_EXTRA_PATH to walk PRIMARY_REPO only." >&2
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
# Candidate list: primary repo's venv first, then optional extra-path
# venv (skipped if EXTRA_PATH is empty), then system python.
CANDIDATES=("$PRIMARY_REPO/claude_mcp_servers/.venv/bin/python")
if [ -n "$EXTRA_PATH" ]; then
    CANDIDATES+=("$EXTRA_PATH/claude_mcp_servers/.venv/bin/python")
fi
CANDIDATES+=("$(command -v python3)" "$(command -v python)")
for candidate in "${CANDIDATES[@]}"; do
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
LEGACY_COLLS="${LEGACY_COLLECTIONS[*]}" \
WEAVIATE_GRPC_PORT="${WEAVIATE_GRPC_PORT:-50052}" \
"$PYTHON" - <<'PY'
import os, sys
import weaviate
# VCO uses non-standard Weaviate gRPC port 50052 (per CLAUDE.md). The default
# weaviate-python-client `connect_to_local()` assumes 50051 — fails when the
# Weaviate container is mapped to 50052 instead (V52-O.2 bug found 2026-06-09
# during the operator-approved rewalk: gRPC ping refused on 50051).
host = os.environ.get("WEAVIATE_HOST", "localhost")
port = int(os.environ.get("WEAVIATE_PORT", "8081"))
grpc_port = int(os.environ.get("WEAVIATE_GRPC_PORT", "50052"))
colls = os.environ.get("COLLS", "").split()
legacy_colls = os.environ.get("LEGACY_COLLS", "").split()
client = weaviate.connect_to_custom(
    http_host=host,
    http_port=port,
    http_secure=False,
    grpc_host=host,
    grpc_port=grpc_port,
    grpc_secure=False,
)
try:
    # Drop current-name collections first.
    for coll in colls:
        try:
            if client.collections.exists(coll):
                client.collections.delete(coll)
                print(f"  dropped {coll}")
            else:
                print(f"  skip {coll} (not present)")
        except Exception as e:
            print(f"  ERROR dropping {coll}: {e}", file=sys.stderr)
    # V52-O.11.L: also drop legacy v0.2.43-era snapshot collections.
    # These are NOT produced by any current analyzer path; they're
    # debris from an old code-graph exploration cycle. Most installs
    # won't have them — the ``not present`` branch is the common path
    # and emits a single info line each.
    if legacy_colls:
        print("  --- legacy v0.2.43-era snapshots ---")
    for coll in legacy_colls:
        try:
            if client.collections.exists(coll):
                client.collections.delete(coll)
                print(f"  dropped legacy {coll}")
            else:
                print(f"  skip legacy {coll} (not present)")
        except Exception as e:
            print(f"  ERROR dropping legacy {coll}: {e}", file=sys.stderr)
finally:
    client.close()
PY

# --- Re-walk both source roots --------------------------------------------

echo
if [ -n "$EXTRA_PATH" ]; then
    echo "Re-walking primary repo + extra-path..."
else
    echo "Re-walking primary repo..."
fi
echo "(this re-creates schemas + populates rows)"
echo

if [ -n "$EXTRA_PATH" ]; then
    "$ANALYZER" "$PRIMARY_REPO" \
        --project "$PROJECT_NAME" \
        --extra-path "$EXTRA_PATH"
else
    "$ANALYZER" "$PRIMARY_REPO" \
        --project "$PROJECT_NAME"
fi

echo
echo "================================================================="
echo "V52-O.2 reset + re-walk complete."
echo "================================================================="
