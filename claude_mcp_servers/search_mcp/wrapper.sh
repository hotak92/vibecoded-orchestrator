#!/usr/bin/env bash
# search-mcp wrapper — runs the search MCP server with GITHUB_TOKEN populated
# from VCT secrets, never leaking the secret to the calling shell.
#
# Why a wrapper:
#   Claude Code's ~/.claude.json `env:` block does not expand ${VAR}
#   substitutions (anthropics/claude-code#2065, #4276), so we can't write
#   `GITHUB_TOKEN: ${GITHUB_TOKEN}` and expect it to work. The accepted
#   workaround is a wrapper script that reads the secret and exec's the
#   real binary.
#
# How:
#   1. Locate the secret at ~/.vct-secrets/shared/github_pat (or legacy
#      flat ~/.vct-secrets/github_pat as a fallback).
#   2. Read it (must be mode 600 or 400; we refuse weaker perms).
#   3. Export GITHUB_TOKEN and exec the real server.
#
# Configuration via env (override defaults):
#   VCT_SECRETS_DIR  default: $HOME/.vct-secrets
#   SEARCH_MCP_PYTHON  default: $REPO_ROOT/claude_mcp_servers/.venv/bin/python
#   SEARCH_MCP_SERVER  default: $REPO_ROOT/claude_mcp_servers/search_mcp/server.py
#
# REPO_ROOT is computed from this script's location (../../ from search_mcp/wrapper.sh).

set -euo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

VCT_DIR="${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
SECRET_FILE="$VCT_DIR/shared/github_pat"
[[ ! -f "$SECRET_FILE" ]] && [[ -f "$VCT_DIR/github_pat" ]] && SECRET_FILE="$VCT_DIR/github_pat"

PYTHON_BIN="${SEARCH_MCP_PYTHON:-$REPO_ROOT/claude_mcp_servers/.venv/bin/python}"
SERVER_PY="${SEARCH_MCP_SERVER:-$REPO_ROOT/claude_mcp_servers/search_mcp/server.py}"

# ── Sanity checks ────────────────────────────────────────────────────────────
if [[ ! -f "$SECRET_FILE" ]]; then
    echo "[search-mcp-wrapper] ERROR: github_pat not found at $VCT_DIR/shared/ or $VCT_DIR/" >&2
    echo "[search-mcp-wrapper] Fix:  vct set --project SHARED --key github_pat   (paste token via stdin)" >&2
    exit 1
fi

perms=$(stat -c %a "$SECRET_FILE" 2>/dev/null || stat -f %Lp "$SECRET_FILE" 2>/dev/null || echo "")
if [[ "$perms" != "600" && "$perms" != "400" ]]; then
    echo "[search-mcp-wrapper] ERROR: $SECRET_FILE has perms $perms (want 600 or 400)" >&2
    echo "[search-mcp-wrapper] Fix:  chmod 600 $SECRET_FILE   (or run 'vct doctor')" >&2
    exit 1
fi

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

# ── Load secret + exec ───────────────────────────────────────────────────────
GITHUB_TOKEN=$(tr -d '[:space:]' < "$SECRET_FILE")
[[ -z "$GITHUB_TOKEN" ]] && { echo "[search-mcp-wrapper] ERROR: $SECRET_FILE is empty" >&2; exit 1; }
export GITHUB_TOKEN

exec "$PYTHON_BIN" "$SERVER_PY" "$@"
