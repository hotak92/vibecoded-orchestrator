#!/usr/bin/env bash
# Scrub sensitive env vars before any subprocess spawning
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

# V52-AI (v0.2.52): MCP fork-bomb mitigation. If an orchestrator update
# is in progress, skip container startup entirely — the launcher's own
# pre-update hub-stop has already torn the supervisor down, and
# bringing containers back up mid-update races install.py's volume +
# binary writes. The lockfile is at <VCT_STATE_DIR or ~/.vct>/
# .update-in-progress.json; we treat missing-file / parse-error /
# stale (expected_completion_by in the past) as "no active update"
# (proceed normally — same as today's pre-fix behaviour).
__vct_root_dir="${VCT_STATE_DIR:-$HOME/.vct}"
__vct_update_lockfile="$__vct_root_dir/.update-in-progress.json"
if [ -f "$__vct_update_lockfile" ]; then
    # Compare expected_completion_by to now via a one-shot Python invocation
    # (avoids depending on jq + `date -d` which differ across distros).
    __still_fresh=$(python3 -c "
import json, datetime, sys
try:
    with open('$__vct_update_lockfile') as f:
        d = json.load(f)
    deadline = d.get('expected_completion_by', '')
    if deadline.endswith('Z'):
        deadline = deadline[:-1] + '+00:00'
    dt = datetime.datetime.fromisoformat(deadline)
    now = datetime.datetime.now(datetime.timezone.utc)
    print('1' if now < dt else '0')
except Exception:
    print('0')
" 2>/dev/null)
    if [ "$__still_fresh" = "1" ]; then
        echo "[ensure-containers] orchestrator update in progress; skipping container startup until update completes" >&2
        exit 0
    fi
fi
unset __vct_root_dir __vct_update_lockfile __still_fresh

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
#
# Zombie-recovery (PR-13, v0.2.11, 2026-05-16):
#   After OOM events or systemd-oomd kills, podman containers may end up
#   reporting state.Status=running with state.Pid=<dead pid>. The conmon
#   monitor process was killed alongside the container, so nobody triggered
#   the runc delete cleanup; the container exists in podman's DB but its
#   PID does not exist in /proc. `podman restart` then fails with
#   "container with given ID already exists: OCI runtime error".
#   We probe State.Pid against /proc/<pid>; if the PID is dead, run
#   `runc delete --force` against the user's runc root (or the system
#   one), then `podman rm --force`, then re-bring-up via the GPU-safe
#   wrapper or compose. Each recovery attempt is appended to
#   ~/.local/state/vct/container-recovery.jsonl for audit.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

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
        VCO_REQUIRED_CONTAINERS=(vco_weaviate vco_ollama vco_code_embed)
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

# Resolve orchestrator root (used to locate the GPU-safe wrapper script).
# Falls back to REPO_ROOT for the orchestrator clone case.
ORCH_ROOT="${VCT_ORCHESTRATOR_ROOT:-}"
if [ -z "$ORCH_ROOT" ]; then
    if [ -d "$REPO_ROOT/scripts" ] && [ -f "$REPO_ROOT/scripts/launch-claude-mcp-stack.sh" ]; then
        ORCH_ROOT="$REPO_ROOT"
    fi
fi
WRAPPER_SCRIPT=""
if [ -n "$ORCH_ROOT" ] && [ -f "$ORCH_ROOT/scripts/launch-claude-mcp-stack.sh" ]; then
    WRAPPER_SCRIPT="$ORCH_ROOT/scripts/launch-claude-mcp-stack.sh"
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

# ---------------------------------------------------------------------------
# pid_alive :: 0 if PID is a live process, 1 otherwise.
# Linux: check /proc/<pid>. macOS / non-Linux: fallback to `kill -0`.
# Soft-fails to "alive" only on errors talking to /proc to avoid false zombies.
# ---------------------------------------------------------------------------
pid_alive() {
    local pid="$1"
    [ -z "$pid" ] && return 1
    [ "$pid" = "0" ] && return 1
    if [ -d /proc ]; then
        [ -d "/proc/$pid" ]
        return $?
    fi
    # Non-Linux fallback. `kill -0` returns 0 if the process exists and we
    # can signal it; non-zero otherwise.
    kill -0 "$pid" 2>/dev/null
}

# ---------------------------------------------------------------------------
# detect_runc_root :: print the runc root dir most likely to contain orphan
# state for this user's podman. Linux rootless podman uses
# /run/user/<uid>/runc; rootful podman + macOS podman-machine use other
# paths. We probe in a safe order.
# ---------------------------------------------------------------------------
detect_runc_root() {
    local uid
    uid="$(id -u 2>/dev/null || echo 0)"
    local candidates=(
        "${VCT_RUNC_ROOT:-}"
        "/run/user/${uid}/runc"
        "/run/runc"
        "${HOME}/.local/share/containers/storage/runc"
    )
    local c
    for c in "${candidates[@]}"; do
        [ -z "$c" ] && continue
        if [ -d "$c" ]; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# log_recovery :: append a JSON line to ~/.local/state/vct/container-recovery.jsonl.
# Args: container, action, reason. Best-effort; never errors.
# ---------------------------------------------------------------------------
log_recovery() {
    local container="$1"
    local action="$2"
    local reason="$3"
    local state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/vct"
    local log_file="$state_dir/container-recovery.jsonl"
    mkdir -p "$state_dir" 2>/dev/null || return 0
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Manually escape only the fields we control. Container/action/reason
    # come from a known controlled set — no quotes, no backslashes — so a
    # naive sprintf is safe here.
    printf '{"timestamp":"%s","container":"%s","action":"%s","reason":"%s"}\n' \
        "$ts" "$container" "$action" "$reason" >> "$log_file" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# is_gpu_container :: 0 if the container name should use the GPU-safe
# wrapper for cold-start (ollama, code_embed). 1 otherwise.
# ---------------------------------------------------------------------------
is_gpu_container() {
    case "$1" in
        *ollama*|*code_embed*) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# bring_up_via_wrapper :: invoke the CDI-wait wrapper if available, else
# fall back to direct compose-up. Honoured for GPU services and for the
# generic "missing containers" path.
# ---------------------------------------------------------------------------
bring_up_via_wrapper() {
    local reason="$1"
    if [ -n "$WRAPPER_SCRIPT" ] && [ -x "$WRAPPER_SCRIPT" ]; then
        # The wrapper handles its own working_dir resolution via
        # VCT_STACK_WORKING_DIR; if the caller set it, honour it; else
        # point it at our COMPOSE_DIR so the wrapper finds compose.yaml.
        if [ -z "${VCT_STACK_WORKING_DIR:-}" ] && [ -n "$COMPOSE_DIR" ]; then
            VCT_STACK_WORKING_DIR="$COMPOSE_DIR" "$WRAPPER_SCRIPT"
        else
            "$WRAPPER_SCRIPT"
        fi
        echo "Ran launch-claude-mcp-stack.sh wrapper ($reason)"
        return 0
    fi
    if [ -n "$COMPOSE_CMD" ] && [ -n "$COMPOSE_DIR" ] && [ -d "$COMPOSE_DIR" ]; then
        (cd "$COMPOSE_DIR" && $COMPOSE_CMD up -d)
        echo "Ran '$COMPOSE_CMD up -d' in $COMPOSE_DIR ($reason)"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# recover_zombie :: tear down a zombie container's runc state and recreate.
# Args: container_name. Returns 0 on best-effort recovery, 1 on hard failure.
# ---------------------------------------------------------------------------
recover_zombie() {
    local name="$1"

    # v0.2.50 audit F6 (2026-06-08): the zombie-state-DB-desync failure
    # mode is Podman-specific (rootless conmon vanishes without writing
    # the exit event). On Docker the centralised daemon manages State.*
    # honestly; the PID-alive /proc check at the caller can fire
    # spuriously (Docker PIDs live in a VM on macOS/Windows; even on
    # Linux Docker the host-side State.Pid is semantically different
    # from podman's). Running runc delete + `docker rm --force` on a
    # healthy Docker container produces noisy unnecessary recreate
    # cycles. Mirror the guard in verify-container-ports.sh:130.
    if [ "${RUNTIME:-podman}" != "podman" ]; then
        return 0
    fi

    local container_id
    container_id="$($RUNTIME inspect "$name" --format '{{.Id}}' 2>/dev/null || echo "")"

    # 1. Try runc delete --force (cleans /run/user/<uid>/runc/<id>/).
    local runc_root
    if command -v runc >/dev/null 2>&1; then
        runc_root="$(detect_runc_root || echo "")"
        if [ -n "$runc_root" ] && [ -n "$container_id" ]; then
            runc --root "$runc_root" delete --force "$container_id" 2>/dev/null || true
        fi
    fi

    # 2. podman rm --force (cleans the state DB row even if the OCI bundle
    # is gone). This is the load-bearing step on Linux rootless podman.
    if ! $RUNTIME rm --force "$name" >/dev/null 2>&1; then
        log_recovery "$name" "failed" "podman rm --force failed"
        echo "ensure-containers: failed to remove zombie '$name' — manual cleanup required" >&2
        return 1
    fi

    # 3. Recreate via the GPU-safe wrapper (preferred for ollama/code_embed)
    # or via direct compose-up.
    if is_gpu_container "$name"; then
        if ! bring_up_via_wrapper "recreating zombie GPU container $name"; then
            log_recovery "$name" "failed" "no wrapper or compose available for recreate"
            return 1
        fi
    else
        if ! bring_up_via_wrapper "recreating zombie container $name"; then
            log_recovery "$name" "failed" "no wrapper or compose available for recreate"
            return 1
        fi
    fi

    log_recovery "$name" "recovered" "zombie pid; runc+rm+recreate"
    echo "ensure-containers: recovered zombie container '$name'"
    return 0
}

started=0
recovered=0
needs_compose=false
needs_gpu_wrapper=false
# v0.2.50 audit F6 (2026-06-08): zombie detection (running status with
# dead PID per /proc) is Podman-specific. On Docker the State.Pid value
# carries different host-side semantics (containerd PID, VM PID on
# macOS/Windows), and `/proc/$pid` is unreliable. Skip the PID-alive
# cross-check for non-podman runtimes; trust Docker's State.Status.
ZOMBIE_DETECTION_ENABLED=true
if [ "${RUNTIME:-podman}" != "podman" ]; then
    ZOMBIE_DETECTION_ENABLED=false
fi
for container in "${VCO_REQUIRED_CONTAINERS[@]}"; do
    status=$($RUNTIME inspect "$container" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
    if [ "$status" = "running" ]; then
        if [ "$ZOMBIE_DETECTION_ENABLED" = "false" ]; then
            # Docker / rootful runtime: trust State.Status=running.
            continue
        fi
        # Liveness probe — guard against zombie state where podman thinks
        # the container is up but the PID is dead (post-OOM, conmon-killed).
        pid=$($RUNTIME inspect "$container" --format '{{.State.Pid}}' 2>/dev/null || echo "0")
        if pid_alive "$pid"; then
            continue
        fi
        # Zombie detected. Recover.
        log_recovery "$container" "detected" "running status with dead pid=$pid"
        if recover_zombie "$container"; then
            recovered=$((recovered + 1))
        fi
        continue
    elif [ "$status" = "stopping" ]; then
        if [ "$ZOMBIE_DETECTION_ENABLED" = "false" ]; then
            # Docker / rootful runtime: trust State.Status=stopping; let
            # the runtime finish its own teardown.
            continue
        fi
        # `stopping` with a dead conmon is the other zombie shape. Treat
        # the same as the running-but-dead-pid case.
        pid=$($RUNTIME inspect "$container" --format '{{.State.Pid}}' 2>/dev/null || echo "0")
        if ! pid_alive "$pid"; then
            log_recovery "$container" "detected" "stopping status with dead pid=$pid"
            if recover_zombie "$container"; then
                recovered=$((recovered + 1))
            fi
            continue
        fi
        # Genuinely still stopping — let the runtime finish; nothing to do.
        continue
    elif [ "$status" = "missing" ]; then
        # Container doesn't exist — needs compose to create it
        needs_compose=true
        if is_gpu_container "$container"; then
            needs_gpu_wrapper=true
        fi
    else
        # Container exists but stopped — try starting it
        $RUNTIME start "$container" 2>/dev/null && started=$((started + 1))
    fi
done

# If any container was missing entirely, bring up the full compose stack.
# Prefer the CDI-wait wrapper for GPU services (Bug J: nvidia-cdi-refresh
# race kills ollama/code_embed on cold start). Fall back to direct compose.
if [ "$needs_compose" = true ]; then
    if [ "$needs_gpu_wrapper" = true ] && [ -n "$WRAPPER_SCRIPT" ] && [ -x "$WRAPPER_SCRIPT" ]; then
        bring_up_via_wrapper "missing GPU container(s)" || \
            echo "ensure-containers: wrapper invocation failed" >&2
    elif [ -n "$COMPOSE_CMD" ] && [ -n "$COMPOSE_DIR" ] && [ -d "$COMPOSE_DIR" ]; then
        # Don't redirect stderr — surface failures so users can see what went wrong.
        (cd "$COMPOSE_DIR" && $COMPOSE_CMD up -d)
        echo "Ran '$COMPOSE_CMD up -d' in $COMPOSE_DIR (missing containers detected)"
    elif [ -z "$COMPOSE_CMD" ]; then
        echo "ensure-containers: $RUNTIME has no compose available (tried '$RUNTIME compose' and standalone) — install $RUNTIME-compose or the compose plugin" >&2
    elif [ -z "$COMPOSE_DIR" ]; then
        echo "ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT/infrastructure, $REPO_ROOT/infrastructure, $REPO_ROOT/claude_mcp_servers) — set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude/env" >&2
    fi
fi

if [ "$started" -gt 0 ]; then
    echo "Started $started container(s) via $RUNTIME"
fi
if [ "$recovered" -gt 0 ]; then
    echo "Recovered $recovered zombie container(s) via $RUNTIME"
fi

exit 0
