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
# How (0.1.7 final, post-fork-readiness sweep 2026-05-08):
#   Two resolution paths, in order:
#
#   1. Env-first (canonical 0.1.7 path): if $GITHUB_TOKEN is already
#      exported, use it. The launcher's `write_project_env_files`
#      writes GITHUB_TOKEN to `.claude/env`, `.claude/settings.json`
#      `env`, and `.vscode/settings.json` `claude-code.env` for every
#      registered project, sourced from the keychain entry at
#      `vct._user_shared_.shared.installer/github_pat`. Subprocesses
#      spawned in any registered project's Claude Code session inherit
#      it directly — no resolver call needed.
#
#   2. Resolver helper (`vct_secrets_resolve.sh <path> github_pat`):
#      reads from the launcher's hub HTTP API at
#      `GET /api/v1/projects/{id}/env?key=github_pat`, which:
#         - resolves the keychain at SENTINEL_SHARED + module_id="installer"
#           (matches what `register_github_pat` writes to)
#         - applies the cross-launcher active-flag gate
#         - finds the manifest declaration via the orchestrator's
#           `vct-module.json::bundled_secrets[]` block (NEW in
#           0.1.7 fork-readiness sweep, item H1)
#      End-to-end working for every base-host project the moment the
#      launcher starts — no module-install required.
#
# The legacy `~/.vct-secrets/shared/github_pat` file fallback that
# existed in earlier 0.1.7 pre-releases (gated behind
# $VCT_LEGACY_FILE_FALLBACK=1) has been REMOVED in the 0.1.7
# fork-readiness sweep (item H4, 2026-05-08). Both canonical paths
# above (env-first and resolver) work end-to-end now, so the file
# fallback is no longer needed. Users with a stale
# `~/.vct-secrets/shared/github_pat` file from a pre-fix install will
# have it migrated into the keychain by the next
# `register_github_pat` call (see `migrate_github_pat_file_to_keychain`
# in commands/installer.rs); manual migration via the
# OnboardingWizard works too.
#
# Configuration via env (override defaults):
#   VCT_PROJECT_PATH       project folder used to resolve the secret
#                          (default: $PWD; the launcher sets this
#                          for `vibecoded`-spawned wrappers).
#   VCT_HUB_PORT           override hub port (else read ~/.vct/hub.port).
#   SEARCH_MCP_PYTHON      default: $REPO_ROOT/claude_mcp_servers/.venv/bin/python
#   SEARCH_MCP_SERVER      default: $REPO_ROOT/claude_mcp_servers/search_mcp/server.py
#
# REPO_ROOT is computed from this script's location (../../ from
# search_mcp/wrapper.sh).

set -euo pipefail

# ── Resolve paths ────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Python resolution chain: explicit override, then the legacy MCP-stack
# venv (pre-unification installs), then the repo-root venv (canonical
# since the venv unification — installs where claude_mcp_servers/.venv
# was retired). A single hardcoded default left the server unable to
# start on root-venv layouts ("Failed to connect" with no visible cause).
PYTHON_BIN="${SEARCH_MCP_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in \
        "$REPO_ROOT/claude_mcp_servers/.venv/bin/python" \
        "$REPO_ROOT/.venv/bin/python"; do
        if [[ -x "$candidate" ]]; then
            PYTHON_BIN="$candidate"
            break
        fi
    done
    # Keep the legacy default for the error message below when neither
    # candidate exists.
    PYTHON_BIN="${PYTHON_BIN:-$REPO_ROOT/claude_mcp_servers/.venv/bin/python}"
fi
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
# Two paths, in order (legacy file fallback retired in 0.1.7 final, item H4):
#   1. $GITHUB_TOKEN already exported in the environment — canonical 0.1.7
#      path. Set by the launcher's `write_project_env_files` for every
#      registered project, sourced from the keychain entry at
#      `vct._user_shared_.shared.installer/github_pat`.
#   2. Resolver helper (`vct_secrets_resolve.sh <path> github_pat`) —
#      reads the keychain via the launcher hub. Works end-to-end for
#      every base-host project after 0.1.7 H1: the orchestrator's
#      `vct-module.json::bundled_secrets[]` declares `github_pat`, so
#      the hub's `/projects/{id}/env` resolver finds it without any
#      module install required. The resolver itself uses SENTINEL_SHARED
#      for the keychain lookup (matches the writer side).
project_path="${VCT_PROJECT_PATH:-$PWD}"

# Path 1: env-first.
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    : # already in env via launcher's per-project env-file emission
elif [[ -n "$RESOLVER" ]]; then
    # Capture ONLY stdout (the secret value) via command substitution. The
    # resolver's stderr carries the operator-facing diagnostic (e.g. "keychain
    # locked") — we deliberately do NOT swallow it with `2>/dev/null` so a
    # locked/unreadable keychain reaches the operator instead of vanishing into
    # a bare "could not resolve github_pat". Only stdout is captured by $(...),
    # so letting stderr through does not pollute GITHUB_TOKEN.
    set +e
    GITHUB_TOKEN=$("$RESOLVER" "$project_path" github_pat)
    rc=$?
    set -e
    case "$rc" in
        0)
            : # ok — resolver returned the value
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
            echo "[search-mcp-wrapper] WARN: github_pat not declared by any installed module nor by the orchestrator's bundled_secrets for $project_path" >&2
            ;;
        5)
            # Hub refused the token on /env — a scoped hub.token.<id> is
            # required (or the token was for the wrong project). See
            # vct_secrets_resolve.sh's exit-code contract (5 = forbidden).
            echo "[search-mcp-wrapper] ERROR: hub refused the token resolving github_pat for $project_path (forbidden — a project-scoped hub token is required); restart the launcher/session so a fresh scoped token is minted" >&2
            exit 1
            ;;
        6)
            # OS keychain is locked or a per-key read failed (hub 503
            # keychain_locked / keychain_error). The resolver already printed a
            # keychain-specific diagnostic to stderr (now visible — see above).
            echo "[search-mcp-wrapper] ERROR: OS keychain is locked or unreadable resolving github_pat for $project_path; unlock the login keychain (or open the launcher) and retry" >&2
            exit 1
            ;;
        *)
            echo "[search-mcp-wrapper] WARN: resolver exited with code $rc" >&2
            ;;
    esac
else
    echo "[search-mcp-wrapper] WARN: vct_secrets_resolve.sh not found; orchestrator may not be installed" >&2
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    echo "[search-mcp-wrapper] ERROR: could not resolve github_pat" >&2
    echo "[search-mcp-wrapper] Canonical fix (0.1.7+):" >&2
    echo "  1. Make sure the VCO Launcher is running" >&2
    echo "  2. Open the launcher's OnboardingWizard or Settings → GitHub Token" >&2
    echo "     (writes to keychain at vct._user_shared_.shared.installer/github_pat)" >&2
    echo "  3. The launcher auto-writes GITHUB_TOKEN to every registered project's" >&2
    echo "     .claude/env, .claude/settings.json env, and .vscode/settings.json" >&2
    echo "     claude-code.env. Verify: grep '^export GITHUB_TOKEN=' .claude/env" >&2
    echo "  4. If the env var is set but you're still seeing this error, restart" >&2
    echo "     your Claude Code session (env files are read at session start)." >&2
    echo "  5. As a last resort, run vct_secrets_resolve.sh \"\$PWD\" github_pat" >&2
    echo "     directly to see the resolver's exit code + diagnostic output." >&2
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
