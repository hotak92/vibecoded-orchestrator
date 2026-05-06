#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# Ensure all required containers are running (background, non-blocking)
# Called by SessionStart hook — checks and starts any stopped containers.
#
# Compose-dir resolution order (PR-2 portability fix 2026-05-06):
#   1. $VCT_COMPOSE_DIR              — explicit override
#   2. $VCT_INFRASTRUCTURE_DIR       — orchestrator clone's infrastructure/
#   3. $VCT_ORCHESTRATOR_ROOT/infrastructure   — env-resolved orch root
#   4. <project>/infrastructure      — bundled compose copy (per-project)
#   5. <project>/claude_mcp_servers  — orchestrator clone fallback (legacy)
# Container names come from the shared `_lib/container-names.sh` registry
# so the hook and the bundled docker-compose.yml cannot disagree.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Source canonical container-name registry. Supplies $VCO_REQUIRED_CONTAINERS
# (and exports VCO_WEAVIATE_CONTAINER / VCO_OLLAMA_CONTAINER / VCO_CODE_EMBED_CONTAINER).
# shellcheck source=_lib/container-names.sh disable=SC1091
if [ -f "$SCRIPT_DIR/_lib/container-names.sh" ]; then
    . "$SCRIPT_DIR/_lib/container-names.sh"
else
    # Fallback if _lib is missing (very old install pre-PR-2). Mirror the
    # current canonical defaults; users can still override via
    # VCT_REQUIRED_CONTAINERS.
    if [ -n "${VCT_REQUIRED_CONTAINERS:-}" ]; then
        # shellcheck disable=SC2206
        read -ra VCO_REQUIRED_CONTAINERS <<<"$VCT_REQUIRED_CONTAINERS"
    else
        VCO_REQUIRED_CONTAINERS=(vco_weaviate vco_ollama vct_code_embed)
    fi
fi

# Resolve compose dir. The bundled per-project install puts compose files
# in <project>/infrastructure; the orchestrator's own clone has a sibling
# claude_mcp_servers/ with a compose.yaml. Prefer the bundled location so
# the hook works in user projects (the previous default of
# $REPO_ROOT/claude_mcp_servers only worked in the orchestrator clone).
COMPOSE_DIR="${VCT_COMPOSE_DIR:-}"
if [ -z "$COMPOSE_DIR" ]; then
    if [ -n "${VCT_INFRASTRUCTURE_DIR:-}" ] && [ -d "$VCT_INFRASTRUCTURE_DIR" ]; then
        COMPOSE_DIR="$VCT_INFRASTRUCTURE_DIR"
    elif [ -n "${VCT_ORCHESTRATOR_ROOT:-}" ] && [ -d "$VCT_ORCHESTRATOR_ROOT/infrastructure" ]; then
        COMPOSE_DIR="$VCT_ORCHESTRATOR_ROOT/infrastructure"
    elif [ -d "$REPO_ROOT/infrastructure" ]; then
        COMPOSE_DIR="$REPO_ROOT/infrastructure"
    elif [ -d "$REPO_ROOT/claude_mcp_servers" ]; then
        # Legacy fallback — only the orchestrator clone has this layout.
        COMPOSE_DIR="$REPO_ROOT/claude_mcp_servers"
    else
        COMPOSE_DIR=""
    fi
fi

# Container runtime: prefer docker if podman isn't around (some users only have one).
RUNTIME="${VCT_CONTAINER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
    if command -v podman >/dev/null 2>&1; then RUNTIME=podman
    elif command -v docker >/dev/null 2>&1; then RUNTIME=docker
    else echo "ensure-containers: neither podman nor docker found, skipping" >&2; exit 0
    fi
fi

# Compose binary: detect both v2 plugin and v1 standalone, for either runtime.
# User can override via VCT_COMPOSE_CMD.
COMPOSE_CMD="${VCT_COMPOSE_CMD:-}"
if [ -z "$COMPOSE_CMD" ]; then
    if [ "$RUNTIME" = "podman" ]; then
        if podman compose version >/dev/null 2>&1; then COMPOSE_CMD="podman compose"
        elif command -v podman-compose >/dev/null 2>&1; then COMPOSE_CMD="podman-compose"
        fi
    elif [ "$RUNTIME" = "docker" ]; then
        if docker compose version >/dev/null 2>&1; then COMPOSE_CMD="docker compose"
        elif command -v docker-compose >/dev/null 2>&1; then COMPOSE_CMD="docker-compose"
        fi
    fi
fi

started=0
needs_compose=false
for container in "${VCO_REQUIRED_CONTAINERS[@]}"; do
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

# If any container was missing entirely, bring up the full compose stack.
# Don't redirect stderr — surface failures so users can see what went wrong.
if [ "$needs_compose" = true ] && [ -n "$COMPOSE_CMD" ] && [ -n "$COMPOSE_DIR" ] && [ -d "$COMPOSE_DIR" ]; then
    (cd "$COMPOSE_DIR" && $COMPOSE_CMD up -d)
    echo "Ran '$COMPOSE_CMD up -d' in $COMPOSE_DIR (missing containers detected)"
elif [ "$needs_compose" = true ] && [ -z "$COMPOSE_CMD" ]; then
    echo "ensure-containers: $RUNTIME has no compose available (tried '$RUNTIME compose' and standalone) — install $RUNTIME-compose or the compose plugin" >&2
elif [ "$needs_compose" = true ] && [ -z "$COMPOSE_DIR" ]; then
    echo "ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT/infrastructure, $REPO_ROOT/infrastructure, $REPO_ROOT/claude_mcp_servers) — set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude/env" >&2
fi

if [ "$started" -gt 0 ]; then
    echo "Started $started container(s) via $RUNTIME"
fi

exit 0
