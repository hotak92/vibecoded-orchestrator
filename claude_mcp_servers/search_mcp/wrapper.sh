#!/usr/bin/env bash
# search-mcp wrapper — runs the search MCP server with GITHUB_TOKEN
# populated from the launcher's keychain (via the hub HTTP API), never
# leaking the secret to the calling shell.
#
# Why a wrapper:
#   Claude Code's ~/.claude.json `env:` block does not expand ${VAR}
#   substitutions (anthropics/claude-code#2065, #4276), so we can't write
#   `GITHUB_TOKEN: ${GITHUB_TOKEN}` and expect it to work. The accepted
#   workaround is a wrapper script that reads the secret and exec's the
#   real binary.
#
# How (post-Fix-#3 cleanup, 0.1.7):
#   1. Resolve GITHUB_TOKEN via the launcher hub:
#        vct_secrets_resolve.sh <project_path> github_pat
#      The launcher's keychain is the authoritative source-of-truth;
#      the hub adds the per-project active-flag gate. No file-side
#      mirror at ~/.vct-secrets/.
#   2. Fall back to the legacy ~/.vct-secrets/shared/github_pat read
#      ONLY if the hub is unreachable AND a $VCT_LEGACY_FILE_FALLBACK
#      env opt-in is set. This is a one-release-cycle migration safety
#      net for users who upgrade Fix-#3 → 0.1.7 with a stopped
#      launcher; it will be removed in 0.1.8.
#   3. Export GITHUB_TOKEN and exec the real server.
#
# Configuration via env (override defaults):
#   VCT_PROJECT_PATH       project folder used to resolve the secret
#                          (default: $PWD; the launcher sets this
#                          for `vibecoded`-spawned wrappers).
#   VCT_HUB_PORT           override hub port (else read ~/.vct/hub.port).
#   VCT_LEGACY_FILE_FALLBACK
#                          when set to "1", fall back to
#                          ~/.vct-secrets/shared/github_pat on hub
#                          failure. Off by default.
#   SEARCH_MCP_PYTHON      default: $REPO_ROOT/claude_mcp_servers/.venv/bin/python
#   SEARCH_MCP_SERVER      default: $REPO_ROOT/claude_mcp_servers/search_mcp/server.py
#
# REPO_ROOT is computed from this script's location (../../ from
# search_mcp/wrapper.sh).

set -euo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PYTHON_BIN="${SEARCH_MCP_PYTHON:-$REPO_ROOT/claude_mcp_servers/.venv/bin/python}"
SERVER_PY="${SEARCH_MCP_SERVER:-$REPO_ROOT/claude_mcp_servers/search_mcp/server.py}"

# ── Locate the resolver ──────────────────────────────────────────────────────
# Prefer the orchestrator-bundled copy at .claude/scripts/. Fall back
# to templates/scripts/ in the same repo (covers the developer running
# this wrapper from the source clone before they've installed the
# orchestrator into a project).
RESOLVER=""
for candidate in \
    "$REPO_ROOT/.claude/scripts/vct_secrets_resolve.sh" \
    "$REPO_ROOT/templates/scripts/vct_secrets_resolve.sh"; do
    if [[ -x "$candidate" ]]; then
        RESOLVER="$candidate"
        break
    fi
done

# ── Resolve GITHUB_TOKEN ─────────────────────────────────────────────────────
GITHUB_TOKEN=""
project_path="${VCT_PROJECT_PATH:-$PWD}"

if [[ -n "$RESOLVER" ]]; then
    set +e
    GITHUB_TOKEN=$("$RESOLVER" "$project_path" github_pat 2>/dev/null)
    rc=$?
    set -e
    case "$rc" in
        0)
            : # ok
            ;;
        1)
            echo "[search-mcp-wrapper] WARN: launcher hub unreachable; the launcher must be running to resolve secrets" >&2
            ;;
        2)
            echo "[search-mcp-wrapper] WARN: project $project_path not registered with the launcher" >&2
            ;;
        3)
            echo "[search-mcp-wrapper] ERROR: github_pat is paused for project $project_path; reactivate it via the launcher GUI" >&2
            exit 1
            ;;
        4)
            echo "[search-mcp-wrapper] WARN: github_pat not declared by any installed module for $project_path" >&2
            ;;
        *)
            echo "[search-mcp-wrapper] WARN: resolver exited with $rc; falling back to legacy file path" >&2
            ;;
    esac
else
    echo "[search-mcp-wrapper] WARN: vct_secrets_resolve.sh not found; orchestrator may not be installed" >&2
fi

# ── Legacy file fallback (gated, one-release-cycle migration safety) ─────────
if [[ -z "$GITHUB_TOKEN" && "${VCT_LEGACY_FILE_FALLBACK:-0}" == "1" ]]; then
    VCT_DIR="${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
    SECRET_FILE="$VCT_DIR/shared/github_pat"
    [[ ! -f "$SECRET_FILE" ]] && [[ -f "$VCT_DIR/github_pat" ]] && SECRET_FILE="$VCT_DIR/github_pat"
    if [[ -f "$SECRET_FILE" ]]; then
        perms=$(stat -c %a "$SECRET_FILE" 2>/dev/null || stat -f %Lp "$SECRET_FILE" 2>/dev/null || echo "")
        if [[ "$perms" == "600" || "$perms" == "400" ]]; then
            GITHUB_TOKEN=$(tr -d '[:space:]' < "$SECRET_FILE")
            echo "[search-mcp-wrapper] DEPRECATION: read github_pat from legacy $SECRET_FILE; this fallback will be removed in 0.1.8" >&2
        else
            echo "[search-mcp-wrapper] WARN: legacy file $SECRET_FILE has perms $perms (want 600); skipping" >&2
        fi
    fi
fi

if [[ -z "$GITHUB_TOKEN" ]]; then
    echo "[search-mcp-wrapper] ERROR: could not resolve github_pat" >&2
    echo "[search-mcp-wrapper] Fix:" >&2
    echo "  1. Make sure the VCO Launcher is running" >&2
    echo "  2. Open the launcher's Secrets panel and set 'github_pat' (shared scope)" >&2
    echo "  3. Verify with: $RESOLVER \"$project_path\" github_pat" >&2
    exit 1
fi
export GITHUB_TOKEN

# ── Sanity checks for the python runtime ─────────────────────────────────────
if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[search-mcp-wrapper] ERROR: python not found at $PYTHON_BIN" >&2
    echo "[search-mcp-wrapper] Set SEARCH_MCP_PYTHON env var, or create the venv:" >&2
    echo "                       python -m venv $REPO_ROOT/claude_mcp_servers/.venv" >&2
    exit 1
fi

if [[ ! -f "$SERVER_PY" ]]; then
    echo "[search-mcp-wrapper] ERROR: server.py not found at $SERVER_PY" >&2
    echo "[search-mcp-wrapper] Set SEARCH_MCP_SERVER env var, or check repo layout." >&2
    exit 1
fi

# ── Exec ─────────────────────────────────────────────────────────────────────
exec "$PYTHON_BIN" "$SERVER_PY" "$@"
