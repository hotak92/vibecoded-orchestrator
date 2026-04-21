#!/usr/bin/env bash
# Ensure infrastructure containers are running (background, non-blocking).
# Called by SessionStart hook — auto-detects Docker or Podman and starts the
# compose stack if containers are stopped or missing.
#
# This hook is Unix-only (bash). Windows users running without WSL should
# start containers manually via `docker compose up -d` in the infrastructure/
# directory before launching Claude Code.

# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY \
      AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD \
      VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null

COMPOSE_DIR="$(cd "$(dirname "$0")/../.." && pwd)/infrastructure"

# Auto-detect container runtime: prefer Podman on Linux, Docker elsewhere
if command -v podman >/dev/null 2>&1; then
    RUNTIME="podman"
elif command -v docker >/dev/null 2>&1; then
    RUNTIME="docker"
else
    # No container runtime available — silently exit (user may have --no-containers)
    exit 0
fi

# Compose command: try `<runtime> compose` (v2 plugin) first, then standalone
if "$RUNTIME" compose version >/dev/null 2>&1; then
    COMPOSE=("$RUNTIME" "compose")
elif command -v "${RUNTIME}-compose" >/dev/null 2>&1; then
    COMPOSE=("${RUNTIME}-compose")
else
    # No compose tool found — skip silently
    exit 0
fi

# Services defined in infrastructure/docker-compose.yml
REQUIRED_SERVICES=(weaviate ollama)

# Check service status via compose (portable across Docker and Podman)
cd "$COMPOSE_DIR" || exit 0

# Get list of running services (one per line)
running=$("${COMPOSE[@]}" ps --services --filter "status=running" 2>/dev/null || true)

missing_any=0
for service in "${REQUIRED_SERVICES[@]}"; do
    if ! echo "$running" | grep -qx "$service"; then
        missing_any=1
        break
    fi
done

if [ "$missing_any" -eq 1 ]; then
    # Bring up the stack in background; user sees output only on failure
    "${COMPOSE[@]}" up -d >/dev/null 2>&1 && echo "Started container stack via $RUNTIME" || true
fi

exit 0
