#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
# Ensure all required containers are running (background, non-blocking)
# Called by SessionStart hook — checks and starts any stopped containers

# Resolve COMPOSE_DIR relative to this hook (script lives at <repo>/.claude/hooks/),
# or honor an override for non-standard layouts.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_DIR="${VCT_COMPOSE_DIR:-$REPO_ROOT/claude_mcp_servers}"

# Container names — tunable per-project via VCT_REQUIRED_CONTAINERS (space-separated).
# Defaults to the free-tier set (no neo4j_claude — that's RL/instinct-tier only).
read -ra REQUIRED_CONTAINERS <<<"${VCT_REQUIRED_CONTAINERS:-weaviate_claude ollama_claude code_embed_claude}"

# Container runtime: prefer docker if podman isn't around (some users only have one).
RUNTIME="${VCT_CONTAINER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
    if command -v podman >/dev/null 2>&1; then RUNTIME=podman
    elif command -v docker >/dev/null 2>&1; then RUNTIME=docker
    else echo "ensure-containers: neither podman nor docker found, skipping" >&2; exit 0
    fi
fi

# Compose binary: podman-compose or docker compose (v2). User can override.
COMPOSE_CMD="${VCT_COMPOSE_CMD:-}"
if [ -z "$COMPOSE_CMD" ]; then
    if [ "$RUNTIME" = "podman" ] && command -v podman-compose >/dev/null 2>&1; then COMPOSE_CMD="podman-compose"
    elif [ "$RUNTIME" = "docker" ]; then COMPOSE_CMD="docker compose"
    else COMPOSE_CMD=""
    fi
fi

started=0
needs_compose=false
for container in "${REQUIRED_CONTAINERS[@]}"; do
    status=$($RUNTIME inspect "$container" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
    if [ "$status" = "running" ]; then
        continue
    elif [ "$status" = "missing" ]; then
        # Container doesn't exist — needs compose to create it
        needs_compose=true
    else
        # Container exists but stopped — try starting it
        $RUNTIME start "$container" 2>/dev/null && started=$((started + 1))
    fi
done

# If any container was missing entirely, bring up the full compose stack
if [ "$needs_compose" = true ] && [ -n "$COMPOSE_CMD" ] && [ -d "$COMPOSE_DIR" ]; then
    (cd "$COMPOSE_DIR" && $COMPOSE_CMD up -d) 2>/dev/null
    echo "Ran '$COMPOSE_CMD up -d' in $COMPOSE_DIR (missing containers detected)"
fi

if [ "$started" -gt 0 ]; then
    echo "Started $started container(s) via $RUNTIME"
fi

exit 0
