#!/usr/bin/env bash
# Ensure the code embedding service (CodeSage-Large-v2) container is running.
# Uses flock to prevent race conditions when multiple sessions start simultaneously.
# Called by SessionStart hook (background, non-blocking).
#
# Optional service: whether compose may create the code_embed container is
# decided by the launcher.db `service_endpoints` plan (v0.2.97) — the
# launcher's Services page or `python -m vco_lib.service_endpoints show` /
# `plan --json` — never by editing a compose file (it ships active in
# infrastructure/docker-compose.yml). When the plan does not list code_embed
# as a VCO-managed, enabled service, this hook silently no-ops (a CPU host
# falls back to Ollama for code embeddings). The probe PORT likewise comes
# from the plan (`VCO_CODE_EMBED_PORT`), never from env `CODE_EMBED_PORT` —
# a retired input; env `*_PORT` values are projected outputs only.

# Scrub sensitive env vars before any subprocess
unset SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY 2>/dev/null
[ -n "${VCT_DISABLE_HOOKS:-}" ] && exit 0

set -euo pipefail

. "$(dirname "${BASH_SOURCE[0]}")/_lib/stderr-cap.sh"

CONTAINER_NAME="${VCT_CODE_EMBED_CONTAINER:-code_embed}"
LOCKFILE="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/code_embed_service.lock"

# Resolve compose dir relative to the hook's own location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${VCT_COMPOSE_DIR:-$(cd "$SCRIPT_DIR/../../claude_mcp_servers" && pwd)}"

# Resolve a Python interpreter portably (python3 → python → py).
# Used for the cross-platform TCP port probe below; see audit findings F3 + F6.
# shellcheck source=_lib/find-python.sh disable=SC1091
[ -f "$SCRIPT_DIR/_lib/find-python.sh" ] && . "$SCRIPT_DIR/_lib/find-python.sh"

# Portable TCP port-open probe. /dev/tcp is bash-only and missing in dash,
# zsh and several MSYS shells (audit F3). Falls back to a 1-second connect
# attempt; returns 0 if open, 1 otherwise. Silent no-op if no Python.
_port_open() {
    local port="$1"
    local timeout_s="${2:-2}"
    if [ -z "${PY:-}" ]; then
        # Last resort — try /dev/tcp anyway. Bash on every supported OS
        # has it; only relevant if Python is genuinely missing.
        timeout "$timeout_s" bash -c "echo > /dev/tcp/localhost/${port}" 2>/dev/null
        return $?
    fi
    "$PY" -c "import socket,sys
s = socket.socket()
s.settimeout(float(sys.argv[2]))
try:
    sys.exit(0 if s.connect_ex(('localhost', int(sys.argv[1]))) == 0 else 1)
finally:
    s.close()" "$port" "$timeout_s" 2>/dev/null
}

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
    echo "ensure-code-embed-service: no Python interpreter for vco_lib.containers (broken VCO install?); skipping"
    exit 0
fi

# v0.2.97: the probe PORT comes from the launcher.db service_endpoints plan
# (`VCO_CODE_EMBED_PORT`), never from env `CODE_EMBED_PORT` — a retired
# input; env `*_PORT` values are projected outputs only and `vco doctor`
# treats retired inputs as retired. ONE plan read serves both the port and
# the compose gate in code_embed_up_args below. When the plan cannot be
# read, fall back to the compiled default 11440 — never to env.
unset VCO_CODE_EMBED_PORT
__vco_se_plan="$("$RUN_PY" -m vco_lib.service_lifecycle plan --shell 2>/dev/null)" || __vco_se_plan=""
PORT=11440
if [ -n "$__vco_se_plan" ]; then
    eval "$__vco_se_plan"
    [ -n "${VCO_CODE_EMBED_PORT:-}" ] && PORT="$VCO_CODE_EMBED_PORT"
fi
__vco_rt_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-containers-resolve.$$"
__vco_rt_out="$("$RUN_PY" -m vco_lib.containers resolve --shell 2>"$__vco_rt_err")" ; __vco_rt_rc=$?
case "$__vco_rt_rc" in
    0|3|4) eval "$__vco_rt_out" ;;
    *)
        echo "ensure-code-embed-service: vco_lib.containers resolve failed (rc=$__vco_rt_rc): $(tail -n 3 "$__vco_rt_err" 2>/dev/null | tr '\n' ' ')"
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
    echo "ensure-code-embed-service: $VCO_RUNTIME_REASON; skipping"
    exit 0
fi
RUNTIME="$VCO_RUNTIME"
COMPOSE_CMD="${VCT_COMPOSE_CMD:-$VCO_COMPOSE_CMD}"

# v0.2.92 BLOCKER-1: this service is the ONLY one whose image is BUILT from
# the checkout, and `compose up` builds only when the image is MISSING — so it
# can serve months-old code through every update, silently truncating
# over-window input at HTTP 200 instead of refusing it. Report it: one line,
# only when the running service's self-reported source digest does not match
# this checkout's. `--quiet-unless-stale` prints NOTHING otherwise, so a
# current (or absent) service costs the session no noise. STDOUT, not stderr:
# a SessionStart hook's stderr is discarded when it exits 0.
report_code_embed_staleness() {
    [ -n "${RUN_PY:-}" ] && [ -x "$RUN_PY" ] || return 0
    "$RUN_PY" -m vco_lib.code_embed_image --quiet-unless-stale 2>/dev/null || true
}

# Use flock to serialize startup attempts across concurrent sessions.
exec 200>"$LOCKFILE"
if ! flock -n 200; then
    echo "[code_embed] Another session is starting the service, skipping"
    exit 0
fi

# Check if container is already running
if $RUNTIME container inspect "$CONTAINER_NAME" --format '{{.State.Status}}' 2>/dev/null | grep -q "running"; then
    if _port_open "${PORT}" 3; then
        echo "[code_embed] Already running on port ${PORT}"
        report_code_embed_staleness
        exit 0
    fi
    echo "[code_embed] Container running but not responding, restarting..."
    $RUNTIME restart "$CONTAINER_NAME" >/dev/null 2>&1
    exit 0
fi

# Check if port is in use by something else (e.g., bare-metal process)
if _port_open "${PORT}" 2; then
    echo "[code_embed] Port ${PORT} already in use (external process)"
    report_code_embed_staleness
    exit 0
fi

# Container doesn't exist or is stopped — try start, then compose up
if $RUNTIME container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
    echo "[code_embed] Starting stopped container..."
    $RUNTIME start "$CONTAINER_NAME" >/dev/null 2>&1
    echo "[code_embed] Started container ${CONTAINER_NAME}"
    exit 0
fi

# v0.2.97 (plan invariant I1): compose may create code_embed only while the
# launcher.db service_endpoints plan lists it as a VCO-managed, enabled
# service, and only with the argv `vco_lib.service_lifecycle compose-args`
# builds — `--no-deps` (code_embed's `depends_on: ollama` must never create an
# Ollama next to an ADOPTED one) and the gpu profile the service lives in.
# Prints the shell-quoted args, or nothing when compose must not run.
# The plan itself was already eval'd above (one read, two consumers);
# VCO_COMPOSE_SERVICES from that eval is the gate.
code_embed_up_args() {
    case " ${VCO_COMPOSE_SERVICES:-} " in
        *" code_embed "*) ;;
        *) return 0 ;;
    esac
    "$RUN_PY" -m vco_lib.service_lifecycle compose-args --shell --services code_embed "$@"
}

if [ -n "$COMPOSE_CMD" ] && [ -d "$COMPOSE_DIR" ]; then
    __ce_up=()
    __ce_args="$(code_embed_up_args --build)" || __ce_args=""
    if [ -z "$__ce_args" ]; then
        echo "[code_embed] not created: launcher.db service_endpoints does not list code_embed as a VCO-managed, enabled service (see \`python -m vco_lib.service_endpoints show\`)"
        exit 0
    fi
    eval "__ce_up=($__ce_args)"
    echo "[code_embed] Starting code embedding service via $COMPOSE_CMD..."
    # v0.2.92 BLOCKER-1: `--build` here. We are CREATING this container, so a
    # build is already on the critical path when no image exists; the flag only
    # adds cost in the one case that must not be skipped — an image that exists
    # but was built from older source (the field state: image 2026-05-16,
    # container recreated 2026-07-12, service still truncating silently).
    # Scoped to `code_embed` alone, so no other service is rebuilt or recreated.
    # `|| true`: this hook runs under `set -euo pipefail`, so a compose that
    # rejects `--build` would otherwise ABORT the script here and the retry
    # below would be unreachable code. (Found by the hook-driving test, not by
    # reading — which is the point of driving it.)
    # shellcheck disable=SC2086  # COMPOSE_CMD is a command + its subcommand
    { (cd "$COMPOSE_DIR" && $COMPOSE_CMD "${__ce_up[@]}") 2>&1 | tail -3; } || true
    # ASK THE RUNTIME whether the container now exists: under pipefail the
    # pipeline status is unusable as a success signal, and `tail`'s status
    # cannot fail at all.
    if ! $RUNTIME container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        # Older compose implementations reject `--build` on `up`. Retry without
        # it rather than leaving the service down; the image then stays stale,
        # which `report_code_embed_staleness` and `vco doctor` both surface.
        echo "[code_embed] compose up --build did not create the container — retrying without --build"
        __ce_args="$(code_embed_up_args)" || __ce_args=""
        eval "__ce_up=($__ce_args)"
        # shellcheck disable=SC2086
        [ -n "$__ce_args" ] && { (cd "$COMPOSE_DIR" && $COMPOSE_CMD "${__ce_up[@]}") 2>&1 | tail -3; } || true
        echo "[code_embed] NOTE: the image was NOT rebuilt from source; run 'python install.py --update' from the orchestrator root to refresh it."
    fi
    echo "[code_embed] Started container ${CONTAINER_NAME} on port ${PORT}"
fi
