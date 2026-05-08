#!/usr/bin/env bash
# verify-container-ports.sh — host-side container-port watchdog (2026-05-08).
#
# Wired as a SessionStart:startup hook. Detects the Podman state-DB desync
# pattern observed 2026-05-07/08: `podman ps` says a container is "Up X
# hours (running)", but the host port is unbound and `curl` fails. The
# container's main PID is dead (and conmon vanished without writing the
# exit event) — Podman's view of the world is wrong. Only a force-rm +
# recreate clears it; `podman restart` is a no-op because Podman thinks
# the container is already running.
#
# Detection: host-side TCP probe of each declared port, NOT podman ps
# (which lies). If a port is listed in the table below AND the
# corresponding container is running per `podman ps` AND the port can't
# be reached from the host, that's the zombie state.
#
# Recovery: podman rm -f <container> && podman-compose up -d <service>.
#
# Bypass: VCT_SKIP_PORT_WATCHDOG=1
# Verbose:  VCT_PORT_WATCHDOG_VERBOSE=1 (default: only prints when it
#           actually finds drift)

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

set -uo pipefail

[ "${VCT_SKIP_PORT_WATCHDOG:-0}" = "1" ] && exit 0

command -v podman >/dev/null 2>&1 || exit 0

# Container | host_port | probe_kind | probe_endpoint
# probe_kind: "http" → curl with --max-time 3
#             "tcp"  → bash /dev/tcp socket open
WATCH=(
    "weaviate_claude|8081|http|/v1/meta"
    "ollama_claude|11435|http|/api/tags"
    "code_embed_claude|11440|tcp|"
    "model_router_claude|11436|tcp|"
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
    podman ps --filter "name=^${name}$" --format '{{.Names}}' 2>/dev/null \
        | grep -qx "$name"
}

container_pid_alive() {
    local name="$1"
    local pid
    pid=$(podman inspect "$name" --format '{{.State.Pid}}' 2>/dev/null)
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
    service="${name%_claude}"
    echo "   → recovering $name (port :$port)"
    if podman rm -f "$name" >/dev/null 2>&1; then
        if [ -n "$compose_dir" ]; then
            ( cd "$compose_dir" && podman-compose up -d "$service" >/dev/null 2>&1 ) || \
                echo "     ! podman-compose up -d $service failed; manual: cd $compose_dir && podman-compose up -d $service"
        else
            echo "     ! could not auto-detect compose dir; manual: podman-compose up -d $service"
        fi
    else
        echo "     ! podman rm -f $name failed"
    fi
done

echo "   recovery complete; first KG/Ollama call may take 20-30s while services warm up"
exit 0
