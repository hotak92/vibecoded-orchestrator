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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The compose dir: ONE home, _lib/compose-dir.sh (shared with ensure-containers
# and verify-container-ports; v0.2.100) — the orchestrator's infrastructure/
# first, and a directory whose parent is not the orchestrator clone (no
# vct-module.json with id "orchestrator") is REFUSED (review L1-F17).
# shellcheck source=_lib/compose-dir.sh disable=SC1091
. "$SCRIPT_DIR/_lib/compose-dir.sh"
vco_resolve_compose_dir "$(cd "$SCRIPT_DIR/../.." && pwd)" || true

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
# the compose gate in code_embed_up_args below. R9 H3: when the plan CANNOT
# be read, this hook takes NO state-changing action — a guess-port probe that
# finds 11440 closed on a container that was deliberately moved to another
# port used to RESTART a healthy service every session the plan was
# unreadable. One line, exit 0. 11440 stays only the default for a plan that
# READS but carries no code_embed row — never to env.
unset VCO_CODE_EMBED_PORT
PORT=11440
if __vco_se_plan="$("$RUN_PY" -m vco_lib.service_lifecycle plan --shell 2>/dev/null)"; then
    eval "$__vco_se_plan"
    [ -n "${VCO_CODE_EMBED_PORT:-}" ] && PORT="$VCO_CODE_EMBED_PORT"
else
    echo "[code_embed] service_endpoints plan unreadable; nothing probed, started or restarted this session"
    exit 0
fi
__vco_rt_err="${TMPDIR:-${XDG_RUNTIME_DIR:-/tmp}}/vco-containers-resolve.$$"
# R10 J1: the resolver's NON-ZERO answers (3 = absent — a refused pin, a
# down daemon; 4 = unknown) are EXPECTED branches in the case below, but
# this hook runs `set -euo pipefail`: a bare `out="$(cmd)" ; rc=$?` ABORTS
# the shell at the assignment, before `rc` is ever read — so every session
# with a refused pin died as a failed hook (exit 3, empty stdout, this
# stderr capture file leaked) and the `case 0|3|4)` report never ran. The
# `rc=0; … || rc=$?` shape makes every exit code a VALUE, not a trap. The
# siblings without `set -e` (ensure-containers, verify-container-ports)
# keep the plain shape — there it is harmless.
# The trap removes the capture file on EVERY exit path from here on
# (including a later `set -e` abort), so nothing leaks per session.
trap 'rm -f "$__vco_rt_err" 2>/dev/null' EXIT
__vco_rt_rc=0
__vco_rt_out="$("$RUN_PY" -m vco_lib.containers resolve --shell 2>"$__vco_rt_err")" || __vco_rt_rc=$?
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
# service (VCO_COMPOSE_SERVICES, from the plan eval'd above — one read, two
# consumers). v0.2.100 (AD-3/AD-4, F-W1-13): the compose call itself is the
# ONE guarded verb, `python -m vco_lib.service_lifecycle up` — the data-identity
# guard first, `--no-deps` and the gpu profile from `compose_up_args`, and the
# one retry/heal home: `--build` is dropped ONLY on positive evidence that
# this compose rejects the flag (then it says the image was NOT rebuilt), and
# a real build failure is reported, never retried silently. This hook no
# longer retries or prints compose hints itself.
case " ${VCO_COMPOSE_SERVICES:-} " in
    *" code_embed "*) ;;
    *)
        echo "[code_embed] not created: launcher.db service_endpoints does not list code_embed as a VCO-managed, enabled service (see \`python -m vco_lib.service_endpoints show\`)"
        exit 0
        ;;
esac
if [ -n "$COMPOSE_DIR_REFUSAL" ]; then
    echo "[code_embed] $COMPOSE_DIR_REFUSAL"
    exit 0
fi
if [ -n "$COMPOSE_CMD" ] && [ -n "$COMPOSE_DIR" ]; then
    echo "[code_embed] Starting code embedding service via $COMPOSE_CMD..."
    # v0.2.92 BLOCKER-1: `--build` here. We are CREATING this container, so a
    # build is already on the critical path when no image exists; the flag
    # only adds cost when an image exists but was built from older source.
    # `|| true`: this hook runs under `set -euo pipefail`; the runtime is
    # asked below whether the container now exists.
    { "$RUN_PY" -m vco_lib.service_lifecycle up --services code_embed --build \
        --compose-dir "$COMPOSE_DIR" --compose-cmd "$COMPOSE_CMD" --runtime "$RUNTIME" 2>&1 \
        | tail -12; } || true
    if $RUNTIME container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        echo "[code_embed] Started container ${CONTAINER_NAME} on port ${PORT}"
    else
        echo "[code_embed] ${CONTAINER_NAME} was not created (see above); \`python install.py --update\` from the orchestrator root retries it"
    fi
fi
