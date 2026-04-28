#!/usr/bin/env bash
# Ensure the code embedding service (CodeSage-Large-v2) container is running.
# Uses flock to prevent race conditions when multiple sessions start simultaneously.
# Called by SessionStart hook (background, non-blocking).
#
# Optional service: only runs if the user has uncommented `code_embed` in
# claude_mcp_servers/compose.yaml. Free tier defaults to Ollama for code
# embeddings, so this hook silently no-ops when the container doesn't exist.

# Scrub sensitive env vars before any subprocess
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -euo pipefail

PORT="${CODE_EMBED_PORT:-11440}"
CONTAINER_NAME="${VCT_CODE_EMBED_CONTAINER:-code_embed}"
LOCKFILE="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/code_embed_service.lock"

# Resolve compose dir relative to the hook's own location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VCT_COMPOSE_DIR:-$(cd "$SCRIPT_DIR/../../claude_mcp_servers" && pwd)}"

# Container runtime: prefer podman if available, else docker.
RUNTIME="${VCT_CONTAINER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
    if command -v podman >/dev/null 2>&1; then RUNTIME=podman
    elif command -v docker >/dev/null 2>&1; then RUNTIME=docker
    else exit 0    # silent no-op: no container runtime
    fi
fi

# Compose binary
COMPOSE_CMD="${VCT_COMPOSE_CMD:-}"
if [ -z "$COMPOSE_CMD" ]; then
    if [ "$RUNTIME" = "podman" ] && command -v podman-compose >/dev/null 2>&1; then COMPOSE_CMD="podman-compose"
    elif [ "$RUNTIME" = "docker" ]; then COMPOSE_CMD="docker compose"
    else COMPOSE_CMD=""
    fi
fi

# Use flock to serialize startup attempts across concurrent sessions.
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "[code_embed] Another session is starting the service, skipping"
    exit 0
fi

# Check if container is already running
if $RUNTIME container inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null | grep -q "running"; then
    if timeout 3 bash -c "echo > /dev/tcp/localhost/${PORT}" 2>/dev/null; then
        echo "[code_embed] Already running on port ${PORT}"
        exit 0
    fi
    echo "[code_embed] Container running but not responding, restarting..."
    $RUNTIME restart "$CONTAINER_NAME" >/dev/null 2>&1
    exit 0
fi

# Check if port is in use by something else (e.g., bare-metal process)
if timeout 2 bash -c "echo > /dev/tcp/localhost/${PORT}" 2>/dev/null; then
    echo "[code_embed] Port ${PORT} already in use (external process)"
    exit 0
fi

# Container doesn't exist or is stopped — try start, then compose up
if $RUNTIME container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "[code_embed] Starting stopped container..."
    $RUNTIME start "$CONTAINER_NAME" >/dev/null 2>&1
    echo "[code_embed] Started container ${CONTAINER_NAME}"
    exit 0
fi

if [ -n "$COMPOSE_CMD" ] && [ -d "$COMPOSE_DIR" ]; then
    echo "[code_embed] Starting code embedding service via $COMPOSE_CMD..."
    (cd "$COMPOSE_DIR" && $COMPOSE_CMD up -d code_embed) 2>&1 | tail -3
    echo "[code_embed] Started container ${CONTAINER_NAME} on port ${PORT}"
fi
