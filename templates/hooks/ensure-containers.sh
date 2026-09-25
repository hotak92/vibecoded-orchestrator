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
#
# WHICH containers, and what may be done to each (v0.2.97): ONE call,
# `python -m vco_lib.service_lifecycle plan --shell`, reads the launcher.db
# `service_endpoints` rows and hands back the compose service list and a
# per-container policy. A VCO-managed service is started, created by compose
# when missing, and re-created when it is a zombie. An ADOPTED container
# (somebody else's — e.g. a Weaviate another compose project created, whose
# data lives on THAT project's volume) is only ever started BY NAME: never
# `rm`, never composed, because a compose re-create would bring it back on
# the installer's default, EMPTY volume. Every compose call names its
# services explicitly with `--no-deps` — there is no bare `up -d` (plan
# invariant I1). `VCT_REQUIRED_CONTAINERS` still narrows/extends the set.
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
#   one). A VCO-managed container is then `podman rm --force`d and
#   re-created by compose; an adopted one only gets `start` (v0.2.97).
#   Each recovery attempt is appended to
#   ~/.local/state/vct/container-recovery.jsonl for audit.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Source canonical container-name registry. Supplies $VCO_REQUIRED_CONTAINERS
# — the user's VCT_REQUIRED_CONTAINERS override, EMPTY when unset (v0.2.97:
# the default set comes from the service_endpoints plan below) — and exports
# VCO_WEAVIATE_CONTAINER / VCO_OLLAMA_CONTAINER / VCO_CODE_EMBED_CONTAINER.
# shellcheck source=_lib/container-names.sh disable=SC1091
if [ -f "$SCRIPT_DIR/_lib/container-names.sh" ]; then
    . "$SCRIPT_DIR/_lib/container-names.sh"
else
    # Fallback if _lib is missing (very old install pre-PR-2): the same
    # override-only rule.
    VCO_REQUIRED_CONTAINERS=()
    if [ -n "${VCT_REQUIRED_CONTAINERS:-}" ]; then
        # shellcheck disable=SC2206
        read -ra VCO_REQUIRED_CONTAINERS <<<"$VCT_REQUIRED_CONTAINERS"
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

# Container runtime + compose: ONE home — `python -m vco_lib.containers resolve`
# (v0.2.92 PLAN-EXTENSION §3.5 / R13). This hook used to mirror the
# podman/docker + compose-form detection inline (as did two sibling hooks,
# install.py and the launcher), and the four copies had drifted in their
# compose preference order. Class A of the A>B>C rule: one Python
# implementation, called via a ~50 ms subprocess on this session-start path.
# Loud-fail: if the resolver cannot run at all (no interpreter, broken
# install), say so on stderr and skip — never fall back to an inline copy.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"
# shellcheck source=_lib/resolve-vco-venv.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/resolve-vco-venv.sh" ] && . "$SCRIPT_DIR/_lib/resolve-vco-venv.sh"
if command -v resolve_vco_venv_python >/dev/null 2>&1; then
    resolve_vco_venv_python "$SCRIPT_DIR"
fi
RUN_PY="${VCO_VENV_PYTHON:-${PY:-}}"
if [ -z "$RUN_PY" ] || [ ! -x "$RUN_PY" ]; then
    echo "ensure-containers: no Python interpreter for vco_lib.containers (broken VCO install?); skipping"
    exit 0
fi
# v0.2.97 (R7a F10): this hook and verify-container-ports both act on the
# containers at session start, and both are `async` — they ran concurrently,
# and the watchdog could act on the plan before this hook's reconcile had
# corrected it. Now the "reconcile → plan → act" below runs under ONE
# per-user lock (`vco_lib.service_lifecycle with-session-lock`, which holds
# it on a descriptor nothing this hook spawns inherits and re-runs this
# script as its child). Busy past 6 s (the watchdog is recovering a
# container): nothing is done this session — never a second actor.
# R8 G3: that locked re-run is started DETACHED (`run-detached`): the 6 s
# lock wait, the 8 s reconcile and a first-session `compose up` together
# exceed this hook's 15 s timeout, and the timeout's kill must never reach
# the lock holder (it would release the lock while `compose up` runs on).
# This process only relays the re-run's output for the hook's budget
# (`service_lifecycle.SESSION_HOOK_RELAY_BUDGET_S`), then returns; the lock
# is released when the re-run really ends.
if [ -z "${VCO_SESSION_LOCK_HELD:-}" ]; then
    exec "$RUN_PY" -m vco_lib.service_lifecycle run-detached --hook ensure-containers --lock-wait 6 \
        --busy "ensure-containers: verify-container-ports is recovering a container right now; left the containers to it this session (the next session re-checks them)" \
        -- bash "${BASH_SOURCE[0]}" "$@"
fi
__vco_rt_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-containers-resolve.$$"
__vco_rt_out="$("$RUN_PY" -m vco_lib.containers resolve --shell 2>"$__vco_rt_err")" ; __vco_rt_rc=$?
case "$__vco_rt_rc" in
    0|3|4) eval "$__vco_rt_out" ;;
    *)
        echo "ensure-containers: vco_lib.containers resolve failed (rc=$__vco_rt_rc): $(tail -n 3 "$__vco_rt_err" 2>/dev/null | tr '\n' ' ')"
        rm -f "$__vco_rt_err"
        exit 0
        ;;
esac
rm -f "$__vco_rt_err"
# v0.2.92 BLOCKER-4 + MAJOR-6: the resolver REFUSES a pinned-but-unusable
# runtime (podman and docker have per-runtime named volumes, so driving the
# one the user did not pin brings the stack up EMPTY) and hands back the
# refusal as its reason. Report it on STDOUT, not stderr: a SessionStart
# hook's stderr is not surfaced to the user when the hook exits 0 -- only
# stdout is injected as session context, and an unread report is not a report.
if [ "$VCO_RUNTIME_STATE" != "resolved" ]; then
    # `absent` is a true fact (nothing installed / daemon down / a refused
    # pin); `unknown` means a probe could not run. Both are skips, both said.
    echo "ensure-containers: $VCO_RUNTIME_REASON; skipping"
    exit 0
fi
RUNTIME="$VCO_RUNTIME"
# v0.2.97 (R8 follow-up): the resolver answered the OTHER runtime because the
# install's record names one that is not installed and the other holds VCO's
# data (case (a) of the read-only record reconcile — the resolver says so via
# requested_via + record_reconciled, and the reason carries the story). One
# stdout line so the user sees what happened; nothing was written, the next
# update re-records it.
if [ "$VCO_RUNTIME_RECONCILED" = "1" ]; then
    echo "ensure-containers: $VCO_RUNTIME_REASON"
fi
# User can override the compose invocation via VCT_COMPOSE_CMD.
COMPOSE_CMD="${VCT_COMPOSE_CMD:-$VCO_COMPOSE_CMD}"

# Session reconcile FIRST (v0.2.97): `python -m vco_lib.service_endpoints
# reconcile --phase session --json`, run by `service_lifecycle
# session-reconcile` as a child with a hard time bound (8 s), soft-failing
# to one stdout line. It runs in the detached re-run (see the lock above), so
# it does not count against this hook's 15 s timeout. It is the emit site of
# `service_endpoint_unreachable` (an adopted container a row names is gone),
# and it corrects the rows to what is running — an adopted container that
# moved port, or a "VCO-managed" row whose container turns out to belong to
# another compose project (then adopted). BEFORE the plan, because the plan
# must act on the corrected rows: read first, it could zombie-`rm` and
# re-create a container that is not VCO's. Nothing this hook does can change
# the reconcile's verdict (it never creates adopted containers), so running
# it after would only act on stale rows. Its own start-by-name of a stopped
# adopted container makes this hook's start of it a no-op.
# `--if-stale 60`: the reconcile runs once per minute across both container
# hooks and every session — whichever holds the lock first (R7a F10).
"$RUN_PY" -m vco_lib.service_lifecycle session-reconcile --if-stale 60 2>/dev/null || true

# The lifecycle plan: which containers, and what may be done to each
# (v0.2.97 — see the header). Loud-fail like the runtime resolver above: a
# plan that cannot be read means NOTHING is started, never a fallback to the
# old whole-stack `up -d` (which would compose-create adopted services).
__vco_lc_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-service-lifecycle.$$"
if [ "${#VCO_REQUIRED_CONTAINERS[@]}" -gt 0 ]; then
    __vco_lc_out="$("$RUN_PY" -m vco_lib.service_lifecycle plan --shell --required "${VCO_REQUIRED_CONTAINERS[*]}" 2>"$__vco_lc_err")"; __vco_lc_rc=$?
else
    __vco_lc_out="$("$RUN_PY" -m vco_lib.service_lifecycle plan --shell 2>"$__vco_lc_err")"; __vco_lc_rc=$?
fi
if [ "$__vco_lc_rc" -ne 0 ]; then
    echo "ensure-containers: vco_lib.service_lifecycle plan failed (rc=$__vco_lc_rc): $(tail -n 3 "$__vco_lc_err" 2>/dev/null | tr '\n' ' '); skipping"
    rm -f "$__vco_lc_err"
    exit 0
fi
rm -f "$__vco_lc_err"
eval "$__vco_lc_out"
unset __vco_lc_err __vco_lc_out __vco_lc_rc

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
# is_gpu_service :: 0 for the compose services the GPU-safe wrapper should
# bring up (ollama, code_embed — the CDI-wait matters for them). 1 otherwise.
# ---------------------------------------------------------------------------
is_gpu_service() {
    case "$1" in
        ollama|code_embed) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# compose_up_services :: bring up EXACTLY the named compose services
# (`--no-deps`, never a bare `up -d`). Args: "build"|"" then service names.
# Prefers the CDI-wait wrapper when a GPU service is among them; the argv
# comes from `vco_lib.service_lifecycle compose-args` (the one home of the
# rule), so the hook never spells a compose command itself.
# ---------------------------------------------------------------------------
compose_up_services() {
    local build="$1"
    shift
    [ "$#" -gt 0 ] || return 0
    local wants_gpu=false s
    for s in "$@"; do is_gpu_service "$s" && wants_gpu=true; done
    if [ "$wants_gpu" = true ] && [ -n "$WRAPPER_SCRIPT" ] && [ -x "$WRAPPER_SCRIPT" ]; then
        # The wrapper resolves its compose file from VCT_STACK_WORKING_DIR;
        # point it at our COMPOSE_DIR unless the caller already did.
        if VCT_STACK_WORKING_DIR="${VCT_STACK_WORKING_DIR:-$COMPOSE_DIR}" \
            VCT_STACK_BUILD="$([ "$build" = build ] && echo 1)" \
            "$WRAPPER_SCRIPT" up "$@"; then
            echo "Ran launch-claude-mcp-stack.sh wrapper for: $*"
            return 0
        fi
        echo "ensure-containers: wrapper invocation failed for: $*" >&2
        return 1
    fi
    if [ -z "$COMPOSE_CMD" ] || [ -z "$COMPOSE_DIR" ] || [ ! -d "$COMPOSE_DIR" ]; then
        return 2
    fi
    local services="$*" args
    local -a up_args=() build_flag=()
    [ "$build" = build ] && build_flag=(--build)
    if ! args="$("$RUN_PY" -m vco_lib.service_lifecycle compose-args --shell --services "$services" \
            "${build_flag[@]}")"; then
        echo "ensure-containers: vco_lib.service_lifecycle compose-args failed for: $services" >&2
        return 1
    fi
    [ -n "$args" ] || return 0
    # `args` is shlex-quoted by the Python side; this only splits it.
    eval "up_args=($args)"
    # Don't redirect stderr — surface failures so users can see what went wrong.
    # shellcheck disable=SC2086  # COMPOSE_CMD is a command + its subcommand
    if (cd "$COMPOSE_DIR" && $COMPOSE_CMD "${up_args[@]}"); then
        echo "Ran '$COMPOSE_CMD $args' in $COMPOSE_DIR"
        return 0
    fi
    if [ "$build" = build ]; then
        # Report which invocation ACTUALLY ran: claiming "--build" after
        # falling back to a plain up would be a promise the run did not keep.
        args="$("$RUN_PY" -m vco_lib.service_lifecycle compose-args --shell --services "$services")" || return 1
        eval "up_args=($args)"
        # shellcheck disable=SC2086
        (cd "$COMPOSE_DIR" && $COMPOSE_CMD "${up_args[@]}")
        echo "Ran '$COMPOSE_CMD $args' in $COMPOSE_DIR ('--build' was rejected, so the code-embedding image was NOT refreshed — run 'python install.py --update' from the orchestrator root)"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# clear_zombie_state :: `runc delete --force` the container's orphan OCI
# state (/run/user/<uid>/runc/<id>/). Touches no container record and no
# data. Args: container_name.
# ---------------------------------------------------------------------------
clear_zombie_state() {
    local name="$1" container_id runc_root
    container_id="$($RUNTIME inspect "$name" --format '{{.Id}}' 2>/dev/null || echo "")"
    if command -v runc >/dev/null 2>&1; then
        runc_root="$(detect_runc_root || echo "")"
        if [ -n "$runc_root" ] && [ -n "$container_id" ]; then
            runc --root "$runc_root" delete --force "$container_id" 2>/dev/null || true
        fi
    fi
}

# ---------------------------------------------------------------------------
# handle_zombie :: act on a zombie container per its lifecycle policy.
# Args: container_name, service, on_zombie (recreate|start|ignore).
# recreate (VCO-managed) → runc cleanup + `rm --force`, and the service is
#   queued for the ONE compose call below.
# start (adopted / unlisted) → runc cleanup + `start` BY NAME. Never `rm`:
#   a compose re-create would bring it back on the installer's EMPTY volume.
# ---------------------------------------------------------------------------
handle_zombie() {
    local name="$1" service="$2" action="$3"

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
    case "$action" in
        recreate)
            clear_zombie_state "$name"
            # podman rm --force cleans the state DB row even if the OCI
            # bundle is gone — the load-bearing step on rootless podman.
            if ! $RUNTIME rm --force "$name" >/dev/null 2>&1; then
                log_recovery "$name" "failed" "podman rm --force failed"
                echo "ensure-containers: failed to remove zombie '$name' — manual cleanup required" >&2
                return 1
            fi
            compose_list+=("$service")
            zombie_recreated+=("$name")
            ;;
        start)
            clear_zombie_state "$name"
            if $RUNTIME start "$name" >/dev/null 2>&1; then
                log_recovery "$name" "recovered" "zombie pid; runc cleanup+start (not VCO-managed: never removed)"
                echo "ensure-containers: restarted zombie container '$name' (not VCO-managed: cleaned its runtime state and started it, never removed it)"
                recovered=$((recovered + 1))
            else
                log_recovery "$name" "failed" "zombie pid; start after runc cleanup failed (not VCO-managed: never removed)"
                echo "ensure-containers: '$name' is in a zombie state and could not be started; VCO does not remove a container it does not manage — check it with its owner ($RUNTIME start $name)"
            fi
            ;;
        *)
            log_recovery "$name" "skipped" "zombie pid; lifecycle policy leaves it alone"
            ;;
    esac
    return 0
}

started=0
recovered=0
# The compose services to bring up, in ONE explicit-list call at the end:
# VCO-managed services whose container is missing, and VCO-managed zombies
# just removed. Adopted containers never land here.
compose_list=()
zombie_recreated=()
# v0.2.92 BLOCKER-1: code_embed is the ONE compose service BUILT from the
# checkout, and `up -d` builds an image only when it is MISSING — so a stale
# image survives every update and keeps silently truncating over-window code
# at HTTP 200. When THIS container is among the missing ones we are creating
# it anyway, so `--build` costs a cache hit when the image is current and
# rebuilds it when it is not. Deliberately NOT set when only weaviate/ollama
# are missing: an unconditional `--build` on a session-start hook would
# rebuild (CUDA: 6 GB base) every time any container went away.
needs_code_embed_build=false
# v0.2.50 audit F6 (2026-06-08): zombie detection (running status with
# dead PID per /proc) is Podman-specific. On Docker the State.Pid value
# carries different host-side semantics (containerd PID, VM PID on
# macOS/Windows), and `/proc/$pid` is unreliable. Skip the PID-alive
# cross-check for non-podman runtimes; trust Docker's State.Status.
ZOMBIE_DETECTION_ENABLED=true
if [ "${RUNTIME:-podman}" != "podman" ]; then
    ZOMBIE_DETECTION_ENABLED=false
fi
for i in "${!VCO_LC_CONTAINER[@]}"; do
    container="${VCO_LC_CONTAINER[$i]}"
    service="${VCO_LC_SERVICE[$i]}"
    [ "$service" = "-" ] && service=""
    on_missing="${VCO_LC_ON_MISSING[$i]}"
    on_zombie="${VCO_LC_ON_ZOMBIE[$i]}"
    on_stopped="${VCO_LC_ON_STOPPED[$i]}"
    if [ "$on_missing" = ignore ] && [ "$on_zombie" = ignore ] && [ "$on_stopped" = ignore ]; then
        continue  # disabled / not autostarted: VCO leaves it alone
    fi
    status=$($RUNTIME inspect "$container" --format '{{.State.Status}}' 2>/dev/null || echo "missing")
    if [ "$status" = "running" ] || [ "$status" = "stopping" ]; then
        if [ "$ZOMBIE_DETECTION_ENABLED" = "false" ]; then
            # Docker / rootful runtime: trust State.Status; let the runtime
            # finish its own teardown of a `stopping` container.
            continue
        fi
        # Liveness probe — guard against zombie state where podman thinks
        # the container is up (or stopping) but the PID is dead (post-OOM,
        # conmon-killed). A live `stopping` container is left to finish.
        pid=$($RUNTIME inspect "$container" --format '{{.State.Pid}}' 2>/dev/null || echo "0")
        if pid_alive "$pid"; then
            continue
        fi
        log_recovery "$container" "detected" "$status status with dead pid=$pid"
        handle_zombie "$container" "$service" "$on_zombie"
        continue
    elif [ "$status" = "missing" ]; then
        case "$on_missing" in
            compose)
                compose_list+=("$service")
                # VCO_CODE_EMBED_CONTAINER comes from the plan (the row's name).
                [ "$container" = "${VCO_CODE_EMBED_CONTAINER:-}" ] && needs_code_embed_build=true
                ;;
            report)
                echo "ensure-containers: container '$container' does not exist; it is not VCO-managed, so VCO does not create it (see \`python -m vco_lib.service_endpoints show\`)"
                ;;
        esac
    elif [ "$on_stopped" = start ]; then
        # Container exists but stopped — start it BY NAME (managed or adopted).
        $RUNTIME start "$container" 2>/dev/null && started=$((started + 1))
    fi
done

# Bring up EXACTLY the queued services — prefer the CDI-wait wrapper for GPU
# services (Bug J: nvidia-cdi-refresh race kills ollama/code_embed on cold
# start), else direct compose. Never the whole stack.
#
# NOTE (not a parity directive — the hook-os-parity gate only reads the first
# few lines of a file and is already satisfied here by the co-modified .ps1
# sibling existing): the v0.2.64 Windows reserved-port-range warning
# (`Test-VcoReservedPorts` in ensure-containers.ps1) has NO behavioural Bash
# counterpart by design. The bug it guards against is Windows-only: WinNAT /
# Hyper-V reserve a dynamic TCP range that refuses host binds on 11435 / 11440.
# There is no `netsh` and no equivalent reservation mechanism on Linux/macOS, so
# this Bash path is the correct no-op. Native-Windows users (no WSL) run the .ps1.
if [ "${#compose_list[@]}" -gt 0 ]; then
    compose_up_services "$([ "$needs_code_embed_build" = true ] && echo build)" "${compose_list[@]}"
    __vco_up_rc=$?
    if [ "$__vco_up_rc" -eq 2 ]; then
        if [ -z "$COMPOSE_CMD" ]; then
            echo "ensure-containers: $RUNTIME has no compose available (tried '$RUNTIME compose' and standalone) — install $RUNTIME-compose or the compose plugin" >&2
        else
            echo "ensure-containers: no compose directory found (tried VCT_COMPOSE_DIR, VCT_INFRASTRUCTURE_DIR, VCT_ORCHESTRATOR_ROOT/infrastructure, $REPO_ROOT/infrastructure, $REPO_ROOT/claude_mcp_servers) — set VCT_INFRASTRUCTURE_DIR or VCT_ORCHESTRATOR_ROOT in .claude/env" >&2
        fi
    fi
    for name in "${zombie_recreated[@]}"; do
        if [ "$__vco_up_rc" -eq 0 ]; then
            log_recovery "$name" "recovered" "zombie pid; runc+rm+recreate"
            echo "ensure-containers: recovered zombie container '$name'"
            recovered=$((recovered + 1))
        else
            log_recovery "$name" "failed" "no wrapper or compose available for recreate"
        fi
    done
fi

if [ "$started" -gt 0 ]; then
    echo "Started $started container(s) via $RUNTIME"
fi
if [ "$recovered" -gt 0 ]; then
    echo "Recovered $recovered zombie container(s) via $RUNTIME"
fi

exit 0
