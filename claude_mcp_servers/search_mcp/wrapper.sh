#!/usr/bin/env bash
# search-mcp wrapper — runs the search MCP server with GITHUB_TOKEN
# populated from the launcher's keychain (via the hub HTTP API) when one
# resolves, never leaking the secret to the calling shell. The token is
# OPTIONAL: the server does not read it, so the server starts without it
# (v0.2.97 — see "Path 1" below).
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
#      exported, use it (exported by the user, or injected by
#      `vct exec --secret github_pat=GITHUB_TOKEN`). The launcher writes
#      NO secret values into project env files (v0.2.73 write
#      invariant), so in a launcher-managed session this is normally
#      unset and path 2 answers.
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
#   1. $GITHUB_TOKEN already exported in the environment (by the user or
#      `vct exec`; the launcher never writes it into project files).
#   2. Resolver helper (`vct_secrets_resolve.sh <path> github_pat`) —
#      reads the keychain via the launcher hub. Works end-to-end for
#      every base-host project after 0.1.7 H1: the orchestrator's
#      `vct-module.json::bundled_secrets[]` declares `github_pat`, so
#      the hub's `/projects/{id}/env` resolver finds it without any
#      module install required. The resolver itself uses SENTINEL_SHARED
#      for the keychain lookup (matches the writer side).
project_path="${VCT_PROJECT_PATH:-$PWD}"

# Path 1: env-first.
#
# GITHUB_TOKEN is OPTIONAL here. The search server reads no GitHub token —
# it exposes `search_papers` only (OpenAlex + arXiv; the GitHub-backed tools
# were retired in v0.2.11, see mcp_registration.rs). The wrapper passes one
# through for anything that wants it, so a token it cannot resolve is
# REPORTED on stderr and the server still starts. Until v0.2.97 every
# resolution failure was fatal (`exit 1`), so on a machine with no registered
# PAT — or with the PAT paused for this project — the MCP failed to start
# ("Failed to connect") for a secret it never uses.
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    : # already exported by the user or injected by `vct exec --secret`
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
    [[ "$rc" -eq 0 ]] || GITHUB_TOKEN=""
    case "$rc" in
        0)
            : # ok — resolver returned the value
            ;;
        1)
            echo "[search-mcp-wrapper] NOTE: launcher hub unreachable; the launcher must be running to resolve secrets" >&2
            ;;
        2)
            echo "[search-mcp-wrapper] NOTE: project $project_path not registered with the launcher" >&2
            ;;
        3)
            echo "[search-mcp-wrapper] NOTE: github_pat is paused for project $project_path (reactivate it in the launcher's Secrets panel if a tool here needs it)" >&2
            ;;
        4)
            echo "[search-mcp-wrapper] NOTE: github_pat not declared by any installed module nor by the orchestrator's bundled_secrets for $project_path" >&2
            ;;
        5)
            # Hub refused the token on /env — a scoped hub.token.<id> is
            # required (or the token was for the wrong project). See
            # vct_secrets_resolve.sh's exit-code contract (5 = forbidden).
            echo "[search-mcp-wrapper] NOTE: hub refused the token resolving github_pat for $project_path (forbidden — a project-scoped hub token is required); restart the launcher/session so a fresh scoped token is minted" >&2
            ;;
        6)
            # OS keychain is locked or a per-key read failed (hub 503
            # keychain_locked / keychain_error). The resolver already printed a
            # keychain-specific diagnostic to stderr (now visible — see above).
            echo "[search-mcp-wrapper] NOTE: OS keychain is locked or unreadable resolving github_pat for $project_path; unlock the login keychain (or open the launcher) and retry" >&2
            ;;
        *)
            echo "[search-mcp-wrapper] NOTE: resolver exited with code $rc" >&2
            ;;
    esac
else
    echo "[search-mcp-wrapper] NOTE: vct_secrets_resolve.sh not found; orchestrator may not be installed" >&2
fi

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
    # A printed command is shipped code: every line below names something
    # that exists and works (v0.2.97 — this block used to promise that "the
    # launcher auto-writes GITHUB_TOKEN to every registered project's
    # .claude/env", which no writer has done since v0.2.73).
    unset GITHUB_TOKEN
    echo "[search-mcp-wrapper] NOTE: starting WITHOUT GITHUB_TOKEN — the search server does not need it." >&2
    echo "  To make one available: register the PAT in the launcher (OnboardingWizard, or" >&2
    echo "  Preferences -> Special Secrets). It lives in the OS keychain and is resolved at" >&2
    echo "  need through vct-hub — VCO writes no secret value into project files (v0.2.73)." >&2
    echo "  Check that it resolves (the exit code names the reason):" >&2
    echo "    ${RESOLVER:-$REPO_ROOT/.claude/scripts/vct_secrets_resolve.sh} \"$project_path\" github_pat" >&2
    echo "  Or inject one for a single command: vct exec --secret github_pat=GITHUB_TOKEN -- <cmd>" >&2
else
    export GITHUB_TOKEN
fi

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
