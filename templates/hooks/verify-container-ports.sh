#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0
# verify-container-ports.sh — host-side container-port watchdog (2026-05-08).
#
# Wired as a SessionStart:startup hook. Detects two related failure modes
# where `<runtime> ps` reports a container as "running" but the host port
# isn't actually answering:
#
#   1. Podman state-DB desync (Podman-specific, observed 2026-05-07/08):
#      conmon vanished without writing the exit event, so Podman's state
#      DB stays stuck on "running" after the main PID died. Only a
#      force-rm + recreate clears it; `podman restart` is a no-op because
#      Podman thinks the container is already running.
#   2. App-level silent crash (engine-agnostic): the container is alive
#      and PID 1 is responsive, but the app inside has crashed/wedged
#      and stopped accepting connections on its port.
#
# Both look the same from the host's POV (TCP probe fails despite ps
# saying "running"). Recovery is engine-specific:
#   - Podman: distinguish via PID-alive cross-check; recover dead-PID
#     case with `podman rm -f` + compose up. Live PID = slow warm-up,
#     skip recovery.
#   - Docker: state-DB desync doesn't exist (daemon manages state
#     centrally). Live `<runtime> ps` is trustworthy. Recovery is just
#     `docker restart <name>` for the silent-crash case.
#
# Engine detection: prefer podman per project convention, fall back to
# docker. VCT_CONTAINER_RUNTIME env var explicitly overrides.
#
# Bypass: VCT_SKIP_PORT_WATCHDOG=1
# Verbose:  VCT_PORT_WATCHDOG_VERBOSE=1 (default: only prints when it
#           actually finds drift)

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

set -uo pipefail

[ "${VCT_SKIP_PORT_WATCHDOG:-0}" = "1" ] && exit 0

# Engine selection: explicit env override wins; otherwise prefer podman
# (project convention), fall back to docker. Bail if neither is on PATH.
RUNTIME="${VCT_CONTAINER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
    if command -v podman >/dev/null 2>&1; then
        RUNTIME="podman"
    elif command -v docker >/dev/null 2>&1; then
        RUNTIME="docker"
    else
        exit 0
    fi
fi
command -v "$RUNTIME" >/dev/null 2>&1 || exit 0

# Compose driver matches engine (podman-compose for podman, docker compose
# for docker). Both honour the same compose.yaml format.
case "$RUNTIME" in
    podman)
        COMPOSE_CMD=("podman-compose")
        command -v podman-compose >/dev/null 2>&1 || COMPOSE_CMD=("podman" "compose")
        ;;
    docker)
        COMPOSE_CMD=("docker" "compose")
        ;;
    *)
        exit 0
        ;;
esac

# Container | host_port | probe_kind | probe_endpoint
# probe_kind: "http" → curl with --max-time 3
#             "tcp"  → bash /dev/tcp socket open
#
# v0.2.15 maintainer-leak fix: stopped hardcoding `weaviate_claude` /
# `ollama_claude` / `code_embed_claude` — those names only ever existed
# on the maintainer's own pre-VCO machine. Real VCO installs use
# `vco_*`. We now ROW-EXPAND each service across every known historical
# name (canonical → v0.1.x unprefixed → maintainer-era), and the
# container_running check below skips the rows whose container doesn't
# exist. This makes the hook portable across all generations of VCO
# install without removing recovery support for users on legacy names.
#
# Authoritative registry lives in vco_lib/containers.py
# (CANONICAL_CONTAINERS + HISTORICAL_ALIASES). Sync this list when those
# change — the test_pr2_templates_portability tests pin them together.
WATCH=(
    # Weaviate — canonical first
    "vco_weaviate|8081|http|/v1/meta"
    "weaviate|8081|http|/v1/meta"
    "weaviate_claude|8081|http|/v1/meta"
    # Ollama
    "vco_ollama|11435|http|/api/tags"
    "ollama|11435|http|/api/tags"
    "ollama_claude|11435|http|/api/tags"
    # Code-embedding service
    "vco_code_embed|11440|tcp|"
    "vct_code_embed|11440|tcp|"
    "code_embed|11440|tcp|"
    "code_embed_claude|11440|tcp|"
    # NOTE: v0.2.50 audit F2 (2026-06-08) — the `model_router_claude|11436`
    # row that previously lived here was a maintainer-machine leak (same
    # shape as the `_claude` suffix family v0.2.15 already cleaned up
    # for weaviate/ollama/code_embed). There is no canonical
    # `vco_model_router` service in compose or install.py; the model-
    # router runs only on the maintainer's host. Drop the row to stop
    # this hook from polling port 11436 on every install.
)

VERBOSE="${VCT_PORT_WATCHDOG_VERBOSE:-0}"

probe_port() {
    local kind="$1" port="$2" endpoint="${3:-}"
    case "$kind" in
        http)
            curl -sf --max-time 3 -o /dev/null "http://localhost:${port}${endpoint}" 2>/dev/null
            ;;
        tcp)
            ( exec 3<>/dev/tcp/localhost/${port} ) 2>/dev/null
            ;;
        *) return 1 ;;
    esac
}

container_running() {
    local name="$1"
    "$RUNTIME" ps --filter "name=^${name}$" --format '{{.Names}}' 2>/dev/null \
        | grep -qx "$name"
}

# Podman-only: tells real "running" from zombie state-DB entries by
# checking whether the registered PID is alive in /proc. Docker doesn't
# have this failure mode (centralised daemon keeps its state honest)
# AND on Docker Desktop / Windows the container PID lives inside a VM
# we can't /proc-check from the host, so always assume alive there.
container_pid_alive() {
    local name="$1"
    if [ "$RUNTIME" != "podman" ]; then
        return 0
    fi
    local pid
    pid=$("$RUNTIME" inspect "$name" --format '{{.State.Pid}}' 2>/dev/null)
    [ -n "$pid" ] && [ "$pid" != "0" ] && [ -d "/proc/$pid" ]
}

zombies=()
healthy=0
absent=0

for entry in "${WATCH[@]}"; do
    IFS='|' read -r name port kind endpoint <<< "$entry"
    if ! container_running "$name"; then
        absent=$((absent + 1))
        [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $name not running (skip)"
        continue
    fi
    if probe_port "$kind" "$port" "$endpoint"; then
        healthy=$((healthy + 1))
        [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $name :$port OK"
        continue
    fi
    # Probe failed AND container "running" → suspect zombie. Cross-check
    # the actual PID — if alive, the port is just slow to bind; skip.
    if container_pid_alive "$name"; then
        [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $name :$port slow (PID alive, starting up?)"
        continue
    fi
    zombies+=("$name|$port")
done

if [ "${#zombies[@]}" -eq 0 ]; then
    [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $healthy healthy, $absent absent, 0 zombies"
    exit 0
fi

echo "🩺 Container port-binding watchdog: ${#zombies[@]} zombie state(s) detected"
echo "   (container says 'running' but host port is unbound AND container PID is dead)"

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
compose_dir=""
for candidate in "$project_root/claude_mcp_servers" "$project_root/infrastructure" "$project_root"; do
    if [ -f "$candidate/compose.yaml" ] || [ -f "$candidate/compose.yml" ] || [ -f "$candidate/docker-compose.yml" ]; then
        compose_dir="$candidate"
        break
    fi
done

for entry in "${zombies[@]}"; do
    IFS='|' read -r name port <<< "$entry"
    # Derive compose service name from the container name. Compose
    # files use unprefixed service keys (weaviate / ollama / code_embed),
    # but the actual containers ship under various names — strip every
    # known prefix and suffix VCO has ever used. Order matters: longest
    # discriminators first so "vco_code_embed" doesn't collapse to "embed".
    service="$name"
    service="${service#vco_}"
    service="${service#vct_}"
    service="${service%_claude}"
    echo "   → recovering $name (port :$port) via $RUNTIME"
    if [ "$RUNTIME" = "podman" ]; then
        # Podman state-DB desync: force-rm + recreate. `podman restart`
        # would be a no-op because Podman thinks the container is alive.
        if "$RUNTIME" rm -f "$name" >/dev/null 2>&1; then
            if [ -n "$compose_dir" ]; then
                ( cd "$compose_dir" && "${COMPOSE_CMD[@]}" up -d "$service" >/dev/null 2>&1 ) || \
                    echo "     ! ${COMPOSE_CMD[*]} up -d $service failed; manual: cd $compose_dir && ${COMPOSE_CMD[*]} up -d $service"
            else
                echo "     ! could not auto-detect compose dir; manual: ${COMPOSE_CMD[*]} up -d $service"
            fi
        else
            echo "     ! $RUNTIME rm -f $name failed"
        fi
    else
        # Docker silent-crash: state DB is reliable, so this means the
        # app inside the container has wedged. `docker restart` cycles
        # PID 1 and is enough.
        if ! "$RUNTIME" restart "$name" >/dev/null 2>&1; then
            echo "     ! $RUNTIME restart $name failed; manual: $RUNTIME logs $name"
        fi
    fi
done

echo "   recovery complete; first KG/Ollama call may take 20-30s while services warm up"
exit 0
