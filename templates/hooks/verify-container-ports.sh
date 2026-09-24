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
#
# Log: every run appends ONE JSON line to
# <project>/.claude/logs/container_port_check.jsonl (<project> =
# $CLAUDE_PROJECT_DIR, else the project this hook is installed in):
# timestamp, runtime, each service's result (healthy | slow | zombie |
# absent, with the container and port) and the action taken (none | skipped
# + reason | waiting_for_session_lock | recovered + what was done to each
# zombie). A run that finds a zombie re-runs itself under the session lock,
# so it writes the detection line and the re-run writes the recovery line.
# Soft-fail: a log that cannot be written never changes what the hook does.
# MUST MATCH the record verify-container-ports.ps1 writes.

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

set -uo pipefail

[ "${VCT_SKIP_PORT_WATCHDOG:-0}" = "1" ] && exit 0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── The run log (see the header) ─────────────────────────────────────────
PORT_CHECK_LOG="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd)}/.claude/logs/container_port_check.jsonl"
LOG_RUNTIME=""
LOG_SERVICES=""   # "service|container|port|result" lines, one per watched container
LOG_RECOVERY=""   # "container|service|action|detail" lines
json_str() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/}"
    s="${s//$'\t'/\\t}"
    printf '"%s"' "$s"
}
# The per-service summary: the most telling state of the service's watched
# containers (zombie > slow > healthy); absent when none of them runs.
log_services_json() {
    local out="" svc best bc bp rank line s c p r rk
    for svc in weaviate ollama code_embed; do
        best="absent"; bc=""; bp=""; rank=0
        while IFS='|' read -r s c p r; do
            [ "$s" = "$svc" ] || continue
            case "$r" in zombie) rk=3 ;; slow) rk=2 ;; healthy) rk=1 ;; *) rk=0 ;; esac
            if [ "$rk" -gt "$rank" ]; then rank=$rk; best="$r"; bc="$c"; bp="$p"; fi
        done <<< "$LOG_SERVICES"
        line="$(json_str "$svc"):{\"result\":$(json_str "$best")"
        [ -n "$bc" ] && line="$line,\"container\":$(json_str "$bc")"
        case "$bp" in ''|*[!0-9]*) ;; *) line="$line,\"port\":$bp" ;; esac
        out="${out:+$out,}$line}"
    done
    printf '{%s}' "$out"
}
log_recovery_json() {
    local out="" c s a d
    while IFS='|' read -r c s a d; do
        [ -n "$c" ] || continue
        out="${out:+$out,}{\"container\":$(json_str "$c"),\"service\":$(json_str "$s"),\"action\":$(json_str "$a"),\"detail\":$(json_str "$d")}"
    done <<< "$LOG_RECOVERY"
    printf '[%s]' "$out"
}
log_run() {  # $1 = action, $2 = reason ("" = none)
    local ts line
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null)"
    line="{\"timestamp\":$(json_str "$ts"),\"hook\":\"verify-container-ports\",\"runtime\":$(json_str "$LOG_RUNTIME"),\"services\":$(log_services_json),\"action\":$(json_str "$1")"
    [ -n "${2:-}" ] && line="$line,\"reason\":$(json_str "$2")"
    [ -n "$LOG_RECOVERY" ] && line="$line,\"recovery\":$(log_recovery_json)"
    line="$line}"
    { mkdir -p "$(dirname "$PORT_CHECK_LOG")" && printf '%s\n' "$line" >> "$PORT_CHECK_LOG"; } 2>/dev/null || true
}
service_of() {
    case "$1" in
        *weaviate*) echo weaviate ;;
        *ollama*) echo ollama ;;
        *code_embed*) echo code_embed ;;
        *) echo "" ;;
    esac
}
log_container() { LOG_SERVICES="${LOG_SERVICES}$(service_of "$1")|$1|$2|$3"$'\n'; }
log_recovered() { LOG_RECOVERY="${LOG_RECOVERY}$1|$(service_of "$1")|$2|$3"$'\n'; }

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
    echo "verify-container-ports: no Python interpreter for vco_lib.containers (broken VCO install?); skipping"
    log_run skipped "no Python interpreter for vco_lib (broken VCO install?)"
    exit 0
fi
__vco_rt_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-containers-resolve.$$"
__vco_rt_out="$("$RUN_PY" -m vco_lib.containers resolve --shell 2>"$__vco_rt_err")" ; __vco_rt_rc=$?
case "$__vco_rt_rc" in
    0|3|4) eval "$__vco_rt_out" ;;
    *)
        echo "verify-container-ports: vco_lib.containers resolve failed (rc=$__vco_rt_rc): $(tail -n 3 "$__vco_rt_err" 2>/dev/null | tr '\n' ' ')"
        rm -f "$__vco_rt_err"
        log_run skipped "vco_lib.containers resolve failed (rc=$__vco_rt_rc)"
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
    # A probe-only watchdog: nothing to verify without a usable runtime, so it
    # stays quiet on a plain "no runtime" host -- ensure-containers already
    # said that. A REFUSED PIN is different: it is a user action
    # (start the pinned runtime, or repin), so it is reported.
    if [ -n "${VCO_RUNTIME_REQUESTED:-}" ]; then
        echo "verify-container-ports: $VCO_RUNTIME_REASON; skipping"
    fi
    log_run skipped "${VCO_RUNTIME_REASON:-no usable container runtime}"
    exit 0
fi
RUNTIME="$VCO_RUNTIME"
LOG_RUNTIME="$RUNTIME"
# Compose driver as an argv array (the resolver quotes each token).
if [ "${#VCO_COMPOSE_ARGV[@]}" -gt 0 ]; then COMPOSE_CMD=("${VCO_COMPOSE_ARGV[@]}"); else COMPOSE_CMD=("$RUNTIME" "compose"); fi
case "$RUNTIME" in
    podman|docker)
        ;;
    *)
        log_run skipped "unsupported container runtime: $RUNTIME"
        exit 0
        ;;
esac

# v0.2.97 (R7a F10): this watchdog and ensure-containers both act on the
# containers at session start, concurrently (`async` hooks). Recovery here now
# happens only under the per-user session lock ensure-containers holds for its
# own "reconcile → plan → act" (`vco_lib.service_lifecycle with-session-lock`),
# and only after that reconcile — so a zombie is judged against CORRECTED rows,
# never removed on a stale one, and never recovered by both hooks at once.
# Detection runs unlocked; when it finds a zombie, the script re-runs itself
# under the lock (detection repeats there: ensure-containers may have
# recovered it meanwhile).
LOCK_HELD="${VCO_SESSION_LOCK_HELD:-}"
ROWS_CHECKED=false
if [ -n "$LOCK_HELD" ]; then
    # Reconcile first (once per minute across both hooks); exit 5 = the rows
    # are not known to be current, so nothing is removed on them below.
    "$RUN_PY" -m vco_lib.service_lifecycle session-reconcile --if-stale 60 --require-fresh 2>/dev/null
    [ $? -eq 0 ] && ROWS_CHECKED=true
fi

# The lifecycle plan: which container is which service's, its port (the
# row's, not a literal), and what may be done to it. Read-only.
unset VCO_WEAVIATE_PORT VCO_OLLAMA_PORT VCO_CODE_EMBED_PORT
__vcp_plan="$("$RUN_PY" -m vco_lib.service_lifecycle plan --shell 2>/dev/null)" || __vcp_plan=""
[ -n "$__vcp_plan" ] && eval "$__vcp_plan"
# Where each service answers: the row's port (VCO_<SERVICE>_PORT from the
# plan), else the compiled default when no plan could be read.
WEAVIATE_PROBE_PORT="${VCO_WEAVIATE_PORT:-8081}"
OLLAMA_PROBE_PORT="${VCO_OLLAMA_PORT:-11435}"
CODE_EMBED_PROBE_PORT="${VCO_CODE_EMBED_PORT:-11440}"

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
    "vco_weaviate|$WEAVIATE_PROBE_PORT|http|/v1/meta"
    "weaviate|$WEAVIATE_PROBE_PORT|http|/v1/meta"
    "weaviate_claude|$WEAVIATE_PROBE_PORT|http|/v1/meta"
    # Ollama
    "vco_ollama|$OLLAMA_PROBE_PORT|http|/api/tags"
    "ollama|$OLLAMA_PROBE_PORT|http|/api/tags"
    "ollama_claude|$OLLAMA_PROBE_PORT|http|/api/tags"
    # Code-embedding service
    "vco_code_embed|$CODE_EMBED_PROBE_PORT|tcp|"
    "vct_code_embed|$CODE_EMBED_PROBE_PORT|tcp|"
    "code_embed|$CODE_EMBED_PROBE_PORT|tcp|"
    "code_embed_claude|$CODE_EMBED_PROBE_PORT|tcp|"
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
    # "Running" with a DEAD main PID is a zombie whatever its port says —
    # nothing in the container can be serving it — so the PID is checked
    # first and a dead container's port is never probed (v0.2.97). A live
    # (or uncheckable) PID: probe the port; failing → slow to bind, skip.
    if container_pid_alive "$name"; then
        if probe_port "$kind" "$port" "$endpoint"; then
            healthy=$((healthy + 1))
            log_container "$name" "$port" healthy
            [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $name :$port OK"
        else
            log_container "$name" "$port" slow
            [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $name :$port slow (PID alive, starting up?)"
        fi
        continue
    fi
    log_container "$name" "$port" zombie
    zombies+=("$name|$port")
done

if [ "${#zombies[@]}" -eq 0 ]; then
    [ "$VERBOSE" = "1" ] && echo "verify-container-ports: $healthy healthy, $absent absent, 0 zombies"
    log_run none
    exit 0
fi
if [ -z "$LOCK_HELD" ]; then
    # The re-run under the lock appends the recovery line.
    log_run waiting_for_session_lock
    # Recover only under the session lock, after the reconcile (see above).
    exec "$RUN_PY" -m vco_lib.service_lifecycle with-session-lock --wait 20 \
        --busy "verify-container-ports: ${#zombies[@]} zombie container(s) seen, but ensure-containers still holds the session lock; not recovered here (the next session re-checks)" \
        -- bash "${BASH_SOURCE[0]}" "$@"
fi

echo "🩺 Container port-binding watchdog: ${#zombies[@]} zombie state(s) detected"
echo "   (container says 'running' but host port is unbound AND container PID is dead)"

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# v0.2.97: the installer's compose (infrastructure/) FIRST — a VCO-managed
# service is re-created under the project that owns it, never under the
# legacy claude_mcp_servers/ home (whose project label is what made
# containers foreign-owned in the first place). Same tiers as
# ensure-containers.
compose_dir=""
for candidate in "${VCT_COMPOSE_DIR:-}" "${VCT_INFRASTRUCTURE_DIR:-}" \
        "${VCT_ORCHESTRATOR_ROOT:+$VCT_ORCHESTRATOR_ROOT/infrastructure}" \
        "$project_root/infrastructure" "$project_root/claude_mcp_servers" "$project_root"; do
    [ -n "$candidate" ] || continue
    if [ -f "$candidate/compose.yaml" ] || [ -f "$candidate/compose.yml" ] || [ -f "$candidate/docker-compose.yml" ]; then
        compose_dir="$candidate"
        break
    fi
done

# v0.2.97 (plan invariants I1 + the zombie gate): what may be done to each
# container comes from the launcher.db service_endpoints plan. Only a
# VCO-managed service is `rm -f`'d and re-created — by compose, naming that
# ONE service with `--no-deps`. An adopted container is never removed: a
# re-create would put it on the installer's default, EMPTY volume. No
# readable plan → nothing is re-created.
up_args=()
zombie_policy() {  # container → "<service|-> <on_zombie>"
    local i
    for i in "${!VCO_LC_CONTAINER[@]}"; do
        if [ "${VCO_LC_CONTAINER[$i]}" = "$1" ]; then
            printf '%s %s\n' "${VCO_LC_SERVICE[$i]}" "${VCO_LC_ON_ZOMBIE[$i]}"
            return 0
        fi
    done
    printf -- '- ignore\n'
}

for entry in "${zombies[@]}"; do
    IFS='|' read -r name port <<< "$entry"
    echo "   → recovering $name (port :$port) via $RUNTIME"
    if [ "$RUNTIME" = "podman" ]; then
        if [ -z "$__vcp_plan" ]; then
            echo "     ! the service_endpoints plan could not be read — $name left as is (manual: $RUNTIME start $name)"
            log_recovered "$name" left_as_is "the service_endpoints plan could not be read"
            continue
        fi
        read -r service on_zombie <<< "$(zombie_policy "$name")"
        if [ "$on_zombie" != "recreate" ]; then
            # Not VCO-managed (adopted / unlisted / no row yet — ownership
            # unknown): ensure-containers cleans its orphan runtime state and
            # starts it BY NAME; never removed.
            echo "     ! $name is not VCO-managed — never removed or re-created here (manual: $RUNTIME start $name)"
            log_recovered "$name" left_as_is "not VCO-managed"
            continue
        fi
        if [ "$ROWS_CHECKED" != true ]; then
            # The session reconcile did not complete: the row may be stale (a
            # container another compose project now owns), and a re-create
            # would put the service on the installer's default volume.
            echo "     ! the service_endpoints rows could not be re-checked this session — $name left as is (manual: $RUNTIME start $name)"
            log_recovered "$name" left_as_is "the service_endpoints rows could not be re-checked"
            continue
        fi
        # Podman state-DB desync: force-rm + recreate. `podman restart`
        # would be a no-op because Podman thinks the container is alive.
        up_line="$("$RUN_PY" -m vco_lib.service_lifecycle compose-args --shell --services "$service" 2>/dev/null)" || up_line=""
        if [ -z "$up_line" ]; then
            echo "     ! no compose argv for $service — $name left as is"
            log_recovered "$name" left_as_is "no compose argv"
            continue
        fi
        eval "up_args=($up_line)"
        if "$RUNTIME" rm -f "$name" >/dev/null 2>&1; then
            if [ -n "$compose_dir" ]; then
                if ( cd "$compose_dir" && "${COMPOSE_CMD[@]}" "${up_args[@]}" >/dev/null 2>&1 ); then
                    log_recovered "$name" recreated "$up_line"
                else
                    echo "     ! ${COMPOSE_CMD[*]} $up_line failed; manual: cd $compose_dir && ${COMPOSE_CMD[*]} $up_line"
                    log_recovered "$name" failed "removed; compose up failed"
                fi
            else
                echo "     ! could not auto-detect compose dir; manual: ${COMPOSE_CMD[*]} $up_line"
                log_recovered "$name" failed "removed; no compose directory found"
            fi
        else
            echo "     ! $RUNTIME rm -f $name failed"
            log_recovered "$name" failed "$RUNTIME rm -f failed"
        fi
    else
        # Docker silent-crash: state DB is reliable, so this means the
        # app inside the container has wedged. `docker restart` cycles
        # PID 1 and is enough.
        if "$RUNTIME" restart "$name" >/dev/null 2>&1; then
            log_recovered "$name" restarted ""
        else
            echo "     ! $RUNTIME restart $name failed; manual: $RUNTIME logs $name"
            log_recovered "$name" failed "$RUNTIME restart failed"
        fi
    fi
done
log_run recovered

echo "   recovery complete; first KG/Ollama call may take 20-30s while services warm up"
exit 0
