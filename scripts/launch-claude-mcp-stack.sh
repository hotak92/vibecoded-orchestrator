#!/usr/bin/env bash
# launch-claude-mcp-stack.sh — boot-safe compose-up for the Claude MCP stack.
#
# v0.2.9 (Bug J): the user's `claude-mcp-containers.service` systemd unit
# ran `podman-compose up -d` directly. On NVIDIA hosts, this raced the
# `nvidia-cdi-refresh.service` systemd unit (which writes
# /var/run/cdi/nvidia.yaml at boot). Result: 2026-05-14 — ollama_claude
# and code_embed_claude failed with
#
#   setting up CDI devices: unresolvable CDI devices nvidia.com/gpu=all
#
# and stayed dead until the user manually started them hours later.
#
# Fix: this script wraps the compose invocation with:
#   1. Runtime selection — THE pin rule (vco_lib.containers.runtime_pin):
#      VCT_CONTAINER_RUNTIME, else state/install/runtime.txt, is a PIN; a
#      pinned runtime that is not usable starts NOTHING (one log line naming
#      the pin and the fix). Unpinned: podman first, then docker.
#   2. NVIDIA presence probe (`nvidia-smi -L`).
#   3. CDI-ready wait — poll /var/run/cdi/nvidia.yaml up to 30s, parse-check.
#   4. On success: compose-up with the GPU overlay.
#   5. On timeout: compose-up WITHOUT the GPU overlay (so non-GPU
#      services come up; ollama/code_embed will run CPU-only) AND log a
#      warning. The user's gnome-keyring / launcher restart will not be
#      blocked by an init-time GPU stall.
#   6. On non-Linux or non-NVIDIA: plain compose-up.
#
# Soft-fail throughout: a CDI-wait timeout MUST NOT block the unit, just
# degrade to CPU-only with a log warning.
#
# Tests: a thin Python wrapper at tests/test_launch_claude_mcp_stack_pick.py
# sources this script and exercises `pick_compose_invocation` against a
# matrix of (runtime, gpu_mode) inputs.

set -u
# NOTE: deliberately NOT `set -e` — we want every branch to be able to
# fall through to a graceful default. Each subcommand handles its own
# exit code explicitly.

# ---------------------------------------------------------------------------
# Configuration. Override via env if you need to:
#   - VCT_STACK_WORKING_DIR   — directory containing compose.yaml
#                                 (default: ${VCT_ORCHESTRATOR_ROOT:-<script_dir>/..}/claude_mcp_servers)
#   - VCT_STACK_LOG_FILE      — log path (default: /tmp/claude-mcp-containers.log)
#   - VCT_STACK_CDI_TIMEOUT   — seconds to wait for CDI yaml (default: 30)
#   - VCT_STACK_RUNTIME_FILE  — explicit runtime.txt path (a caller
#                                 override, strict like VCT_CONTAINER_RUNTIME).
#                                 Otherwise the record is THIS wrapper's own
#                                 clone's state/install/runtime.txt — see
#                                 resolve_runtime_file() (R8 G5).
#   - VCT_ORCHESTRATOR_ROOT   — orchestrator install root (the default
#                                 compose home when VCT_STACK_WORKING_DIR is
#                                 unset). NOT a runtime.txt source: another
#                                 clone's record is not this install's.
#   - VCT_STACK_GPU_OVERLAY   — overlay filename for podman path
#                                 (default: infrastructure/podman-compose.gpu.yml)
#   - VCT_STACK_GPU_OVERLAY_DOCKER — overlay for docker path
#                                 (default: infrastructure/docker-compose.gpu.yml)
#   - VCT_STACK_COMPOSE_OVERRIDE  — user-machine compose override
#                                 (default: compose.override.yaml). Resolved
#                                 relative to VCT_STACK_WORKING_DIR; auto-
#                                 applied iff the file exists and is non-
#                                 empty. PR-22 (2026-05-16): podman-compose's
#                                 explicit `-f compose.yaml` bypasses its
#                                 own auto-load, so this script MUST emit
#                                 `-f compose.override.yaml` explicitly when
#                                 the file is present. Without this fix,
#                                 launcher-managed Storage UX overrides
#                                 (PR-10A) were silently ignored at boot.
# ---------------------------------------------------------------------------

# Default working dir: portable fallback. Prefer VCT_ORCHESTRATOR_ROOT
# (set by the launcher / install.py), otherwise derive from this script's
# location (this script lives in <orchestrator>/scripts/, so .. is the
# orchestrator root). systemd units / launchctl jobs always set
# VCT_STACK_WORKING_DIR explicitly so this default rarely fires.
_VCT_DEFAULT_STACK_ROOT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")")/.."
# v0.2.97: the INSTALLER's compose (<root>/infrastructure) is the default when
# it exists. A VCO-managed service composed from the legacy
# claude_mcp_servers/ home gets THAT project's label (and, for Ollama, its
# differently-named volume) — the foreign-owned shape the service_endpoints
# migration exists to undo. The legacy home stays the fallback for a clone
# without infrastructure/.
_VCT_DEFAULT_STACK_BASE="${VCT_ORCHESTRATOR_ROOT:-${_VCT_DEFAULT_STACK_ROOT}}"
if [ -f "$_VCT_DEFAULT_STACK_BASE/infrastructure/docker-compose.yml" ]; then
    _VCT_DEFAULT_STACK_DIR="$_VCT_DEFAULT_STACK_BASE/infrastructure"
else
    _VCT_DEFAULT_STACK_DIR="$_VCT_DEFAULT_STACK_BASE/claude_mcp_servers"
fi
VCT_STACK_WORKING_DIR="${VCT_STACK_WORKING_DIR:-$_VCT_DEFAULT_STACK_DIR}"
VCT_STACK_LOG_FILE="${VCT_STACK_LOG_FILE:-/tmp/claude-mcp-containers.log}"
VCT_STACK_CDI_TIMEOUT="${VCT_STACK_CDI_TIMEOUT:-30}"
# NOTE (PR-12 Bug C): VCT_STACK_RUNTIME_FILE is not eagerly defaulted to
# ${VCT_STACK_WORKING_DIR}/state/install/runtime.txt — that single path was
# too narrow when systemd's WorkingDirectory pointed at a stale install
# location. resolve_runtime_file() probes several candidate paths and the
# first that EXISTS with a podman/docker token is the pin. (PR-12 Bug B's
# "skip a runtime.txt whose runtime is down" is superseded by the pin rule,
# v0.2.97: see detect_runtime.)
# v0.2.97: remember which file knobs the CALLER set, so main() can adapt the
# defaults to the working dir's layout (infrastructure/ holds
# docker-compose.yml + its overlays side by side; the legacy
# claude_mcp_servers/ holds compose.yaml with the overlays one level down).
_VCT_STACK_COMPOSE_FILE_SET="${VCT_STACK_COMPOSE_FILE+1}"
_VCT_STACK_GPU_OVERLAY_SET="${VCT_STACK_GPU_OVERLAY+1}"
_VCT_STACK_GPU_OVERLAY_DOCKER_SET="${VCT_STACK_GPU_OVERLAY_DOCKER+1}"
VCT_STACK_GPU_OVERLAY="${VCT_STACK_GPU_OVERLAY:-infrastructure/podman-compose.gpu.yml}"
VCT_STACK_GPU_OVERLAY_DOCKER="${VCT_STACK_GPU_OVERLAY_DOCKER:-infrastructure/docker-compose.gpu.yml}"
VCT_STACK_COMPOSE_FILE="${VCT_STACK_COMPOSE_FILE:-compose.yaml}"
VCT_STACK_COMPOSE_OVERRIDE="${VCT_STACK_COMPOSE_OVERRIDE:-compose.override.yaml}"
# v0.2.97 — WHICH services this script composes (plan invariant I1: never a
# bare whole-stack `up -d`). Order of precedence:
#   1. service names on the command line (`launch-claude-mcp-stack.sh
#      [up|start|restart] <service>...` — the verb is optional and means
#      "bring up", as it always did);
#   2. VCO_COMPOSE_SERVICES in the environment (space-separated; set but
#      EMPTY means "nothing" → no compose call);
#   3. the launcher.db service_endpoints plan (`python -m
#      vco_lib.service_lifecycle plan`) — the boot unit's case, which ALSO
#      starts adopted containers BY NAME.
# 1 and 2 are intersected with the plan's VCO-managed list: an adopted
# service is never composed, whoever asks. No readable plan → nothing runs.
#   - VCT_STACK_BUILD=1       — add `--build` (code_embed's image is built
#                                 from the checkout; the session hook sets it
#                                 when it is creating that container).
_VCT_CALLER_SERVICES_SET="${VCO_COMPOSE_SERVICES+1}"
_VCT_CALLER_SERVICES="${VCO_COMPOSE_SERVICES-}"

# Resolve the directory that contains THIS script — used as one fallback
# root for runtime.txt resolution. Works whether the script is sourced or
# executed directly.
_VCT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || _VCT_SCRIPT_DIR=""
# The clone this wrapper belongs to — the install it serves (R8 G5): its
# runtime.txt is THE record, its infrastructure/ the fallback compose home,
# its ledger where an exit 3 is recorded (G6). Same answer as Python's
# resolve_install_root() and Rust's orchestrator_install_root().
_VCT_OWN_ROOT=""
[ -n "$_VCT_SCRIPT_DIR" ] && _VCT_OWN_ROOT="$(cd "$_VCT_SCRIPT_DIR/.." 2>/dev/null && pwd)"

# ---------------------------------------------------------------------------
# log :: append a timestamped line to the log file. Best-effort; never errors.
# ---------------------------------------------------------------------------
log() {
    local ts
    ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s [launch-claude-mcp-stack] %s\n' "$ts" "$*" >> "$VCT_STACK_LOG_FILE" 2>/dev/null || true
    printf '%s [launch-claude-mcp-stack] %s\n' "$ts" "$*"
}

# ---------------------------------------------------------------------------
# resolve_runtime_file :: prints the path of THE runtime.txt record — the
# runtime.txt pin. Empty string when there is none (unpinned).
#
# Candidates (first that records podman/docker wins):
#   1. ${VCT_STACK_RUNTIME_FILE} if explicitly set (caller override).
#   2. <own clone>/state/install/runtime.txt — the install this wrapper
#      belongs to and serves (R8 G5). The same record Python
#      (resolve_install_root) and Rust (orchestrator_install_root) read.
# VCT_STACK_WORKING_DIR (a compose dir, possibly a stale unit
# WorkingDirectory — PR-12 Bug C) and VCT_ORCHESTRATOR_ROOT are NOT
# candidates: before v0.2.97 R8 the FIRST of them that held a record won,
# so another clone's record could pin this install's stack onto empty
# volumes. A differing record there is logged, never used.
#
# A candidate that is missing, empty or names no runtime is skipped. One
# whose runtime is DOWN is NOT skipped: it is the pin, and detect_runtime
# refuses it (or, for the own clone's record, reconciles it read-only).
# ---------------------------------------------------------------------------
resolve_runtime_file() {
    local candidates=()
    if [ -n "${VCT_STACK_RUNTIME_FILE:-}" ]; then
        candidates+=("$VCT_STACK_RUNTIME_FILE")
    fi
    if [ -n "${_VCT_OWN_ROOT:-}" ]; then
        candidates+=("${_VCT_OWN_ROOT}/state/install/runtime.txt")
    fi

    local seen_path=""
    local cand
    for cand in "${candidates[@]}"; do
        # De-dup adjacent identical candidates (common when env vars
        # collapse to the same path on default installs).
        [ "$cand" = "$seen_path" ] && continue
        seen_path="$cand"
        [ -r "$cand" ] || continue
        local token
        token="$(head -n 1 "$cand" 2>/dev/null | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
        case "$token" in
            podman|docker)
                _log_foreign_record "$cand" "$token"
                printf '%s\n' "$cand"
                return 0
                ;;
            '') ;;
            *) log "runtime.txt at $cand names '$token', not podman or docker — ignoring it" >&2 ;;
        esac
    done
    printf ''
}

# _log_foreign_record :: say so when VCT_ORCHESTRATOR_ROOT's clone records a
# DIFFERENT runtime than the one used ($1 path, $2 token) — never use it.
_log_foreign_record() {
    local foreign="${VCT_ORCHESTRATOR_ROOT:-}/state/install/runtime.txt" ftok
    [ -n "${VCT_ORCHESTRATOR_ROOT:-}" ] && [ -r "$foreign" ] || return 0
    [ "$foreign" = "$1" ] && return 0
    ftok="$(head -n 1 "$foreign" 2>/dev/null | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    case "$ftok" in
        podman|docker)
            [ "$ftok" = "$2" ] || log "note: $foreign records $ftok, but this wrapper serves the install whose record is $1 ($2) — ignoring the other clone's record" >&2 ;;
    esac
}

# ---------------------------------------------------------------------------
# reconcile_record :: the READ-ONLY form of install.py's runtime-record
# reconcile (python -m vco_lib.runtime_reconcile boot — the one home of the
# decision; this wrapper never rewrites state). Prints the runtime to use
# for THIS boot when VCO's own record is stale AND there is positive evidence
# of where the data is — its runtime is not installed (not on PATH nor in the
# usual install locations) and the other one holds VCO's containers/volumes,
# or it holds none of them while the other does — and nothing otherwise (then
# the pin is refused as before). Under a bind-mounted data folder (R10 J2,
# R11 L2/L3) only VCO containers RUNNING under the other runtime — or every
# service's data being a folder — switch; a leftover volume there never does.
# "No VCO data anywhere" is never switched
# here (R9 H1: install.py decides that one), nor is a runtime the user
# confirmed with `install.py --container` (R9 H2). The next install/update
# re-records it.
# ---------------------------------------------------------------------------
reconcile_record() {
    local py="${STACK_PY:-}" out
    [ -n "$py" ] || py="$(resolve_stack_python)"
    [ -n "$py" ] && [ -n "$_VCT_OWN_ROOT" ] || return 0
    out="$(PYTHONPATH="${_VCT_OWN_ROOT}${PYTHONPATH:+:$PYTHONPATH}" "$py" -m vco_lib.runtime_reconcile boot --root "$_VCT_OWN_ROOT" 2>/dev/null)" || return 0
    local VCO_RECONCILE_OUTCOME="" VCO_RECONCILE_RUNTIME="" VCO_RECONCILE_DETAIL=""
    eval "$out"
    if [ "$VCO_RECONCILE_OUTCOME" = "rewritten" ] && [ -n "$VCO_RECONCILE_RUNTIME" ]; then
        log "$VCO_RECONCILE_DETAIL" >&2
        printf '%s\n' "$VCO_RECONCILE_RUNTIME"
    fi
}

# ---------------------------------------------------------------------------
# record_boot_refusal :: R8 G6 — an exit 3 must not be a dead end. Records
# `container_runtime_unusable` in the own clone's ledger through the one
# Python emitter (python -m vco_lib.runtime_reconcile record-boot-refusal),
# which session start and the launcher show; it clears once the runtime
# answers. Best effort, bounded, never blocks boot. $1 = the reason.
#
# R9 H7: the CLI bounds ITSELF — no coreutils `timeout` (absent on macOS):
# it waits at most BOOT_LEDGER_LOCK_TIMEOUT_S for the ledger lock an in-flight
# update may hold (non-blocking retries, then it skips with a log line), and
# exits by RECORD_REFUSAL_DEADLINE_S whatever else is in flight.
# ---------------------------------------------------------------------------
record_boot_refusal() {
    local py="${STACK_PY:-}"
    [ -n "$py" ] || py="$(resolve_stack_python)"
    [ -n "$py" ] && [ -n "$_VCT_OWN_ROOT" ] || return 0
    PYTHONPATH="${_VCT_OWN_ROOT}${PYTHONPATH:+:$PYTHONPATH}" "$py" -m vco_lib.runtime_reconcile \
        record-boot-refusal --root "$_VCT_OWN_ROOT" --reason "$1" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# _bounded :: run "$@" for at most $1 seconds — coreutils `timeout` when it
# exists, else a portable background watchdog (macOS ships no `timeout`, and
# before R9 H7 every bounded probe here simply FAILED there: `timeout 5 docker
# info` is "command not found", so every runtime read as unusable at boot).
# Returns the command's exit code (non-zero when it was cut off).
# ---------------------------------------------------------------------------
_bounded() {
    local secs="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
        return $?
    fi
    "$@" &
    local pid=$! watcher rc
    ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null; sleep 2; kill -KILL "$pid" 2>/dev/null ) >/dev/null 2>&1 &
    watcher=$!
    wait "$pid"
    rc=$?
    kill "$watcher" 2>/dev/null
    wait "$watcher" 2>/dev/null
    return "$rc"
}

# ---------------------------------------------------------------------------
# augment_tool_path :: R9 H1(b)/H5 — a boot unit's PATH (systemd --user,
# launchd) lacks ~/bin, ~/.local/bin, /opt/homebrew/bin, /usr/local/bin, ...
# where podman/docker (or their compose front-ends) are often installed, so
# `command -v` called an installed runtime "not installed" — and a runtime
# judged not installed is one the record reconcile may switch away from.
# Asks the ONE table (vco_lib/tool_search_dirs.toml, through
# `python -m vco_lib.tool_search_dirs search-path`) and adds the directory of
# every such tool found outside PATH, per the table's placement (a
# graphical-launch dir such as Homebrew ahead of PATH, as a login shell has it;
# a runtime location such as ~/bin after it) — the order the launcher, the hub
# and every Python surface use. PATH itself is kept as it is. Soft: no Python
# or no answer leaves PATH as it is.
# ---------------------------------------------------------------------------
augment_tool_path() {
    local p
    [ -n "${STACK_PY:-}" ] || return 0
    p="$(stack_py vco_lib.tool_search_dirs search-path 2>/dev/null)" || return 0
    [ -n "$p" ] || return 0
    if [ "$p" != "$PATH" ]; then
        PATH="$p"
        export PATH
        log "container runtime tools found outside this service's PATH; PATH is now: $PATH"
    fi
}

# ---------------------------------------------------------------------------
# _runtime_usable :: given a runtime token (lowercase: "docker" or "podman"),
# return 0 iff the runtime is actually usable on this host (binary exists
# AND its daemon / rootless setup is reachable). PR-12 Bug A.
#
# This guards the real-world failure mode where Docker Desktop is installed
# (binary on PATH) but the user is not in the `docker` group → the systemd
# unit picks runtime=docker, then fails at boot with
# "permission denied while trying to connect to the Docker daemon socket".
#
# Detection rules:
#   docker → `docker info` exit 0 AND output contains a "Server:" section.
#            The Client section appears even without daemon access; the
#            Server section requires a reachable daemon.
#   podman → `podman info` exit 0 (rootless setup includes its own probes;
#            podman info exits non-zero if the user namespace / storage
#            backend isn't initialized).
#   anything else → not usable.
#
# Both probes carry a 5s bound (_bounded — portable, R9 H7) — a hung
# daemon socket must NOT block boot indefinitely.
# ---------------------------------------------------------------------------
_runtime_usable() {
    local token="$1"
    case "$token" in
        docker)
            command -v docker >/dev/null 2>&1 || return 1
            local info_out
            if ! info_out="$(_bounded 5 docker info 2>&1)"; then
                return 1
            fi
            # Server: section presence is the daemon-access proxy.
            printf '%s\n' "$info_out" | grep -qE '^(Server:|Server Version:)' || return 1
            return 0
            ;;
        podman)
            command -v podman >/dev/null 2>&1 || return 1
            _bounded 5 podman info >/dev/null 2>&1 || return 1
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# refuse_pin :: log THE one line for a pinned runtime that cannot be used —
# naming the pin's source and the fix — on stderr and in the log file (never
# on stdout: detect_runtime's stdout is its answer).
#   $1 = pinned runtime, $2 = pin source (VCT_CONTAINER_RUNTIME or the
#   runtime.txt path), $3 = what is wrong with it.
# MUST MATCH Write-RuntimePinRefusal in launch-claude-mcp-stack.ps1.
# ---------------------------------------------------------------------------
refuse_pin() {
    local pinned="$1" source="$2" why="$3" other="docker" change
    [ "$pinned" = "docker" ] && other="podman"
    if [ "$source" = "VCT_CONTAINER_RUNTIME" ]; then
        change="unset VCT_CONTAINER_RUNTIME (or set it to $other) if the data is not in $pinned"
    else
        change="set VCT_CONTAINER_RUNTIME=$other if the data is not in $pinned (the install recorded $pinned in $source)"
    fi
    log "FATAL: the container runtime is pinned to $pinned by $source, but $why — starting nothing (the stack's data is in $pinned's volumes; $other would start it on empty ones). Fix: start $pinned, or $change." >&2
}

# ---------------------------------------------------------------------------
# detect_runtime :: prints one of "docker", "podman-compose", "podman compose", or ""
#
# THE pin rule (vco_lib.containers.runtime_pin, v0.2.97 — the same rule as
# the session hooks, install.py and the launcher):
#   1. VCT_CONTAINER_RUNTIME=podman|docker is a PIN ("auto"/unset: none).
#   2. Else the first runtime.txt resolve_runtime_file finds is a PIN.
#   A pinned runtime is the ONLY candidate. When it is not usable (binary
#   missing, daemon / machine down, or podman without a compose front-end)
#   refuse_pin logs one line and this returns 4 with empty output: starting
#   the stack under the OTHER runtime would create its containers on that
#   runtime's empty volumes next to the real data (plan invariants I1/I2).
#   This supersedes PR-12 Bug B's fall-through to the other runtime.
#   3. Unpinned: podman first (preferred default, no group-permission
#      gotcha), then docker; podman without a compose front-end falls
#      through to docker.
#   4. Empty (no usable runtime), return 0.
#
# A "usable" docker means `docker info` reaches the daemon (Server section
# present); a "usable" podman means `podman info` succeeds (_runtime_usable).
# ---------------------------------------------------------------------------
detect_runtime() {
    local pref pin="" pin_source=""
    pref="$(printf '%s' "${VCT_CONTAINER_RUNTIME:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    case "$pref" in
        podman|docker)
            pin="$pref"
            pin_source="VCT_CONTAINER_RUNTIME"
            ;;
        ''|auto)
            : # no env pin
            ;;
        *)
            log "VCT_CONTAINER_RUNTIME=${pref} unrecognized (expected 'podman'/'docker'/'auto') — ignoring" >&2
            ;;
    esac
    if [ -z "$pin" ]; then
        local runtime_file
        runtime_file="$(resolve_runtime_file)"
        if [ -n "$runtime_file" ]; then
            pin="$(head -n 1 "$runtime_file" 2>/dev/null | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
            pin_source="$runtime_file"
        fi
    fi

    # R8 G5: VCO's OWN record (never the user's VCT_CONTAINER_RUNTIME, never an
    # explicit VCT_STACK_RUNTIME_FILE) is reconciled read-only when its runtime
    # is not usable, so a stale record does not strand the stack at boot.
    if [ -n "$pin" ] && [ "$pin_source" = "${_VCT_OWN_ROOT}/state/install/runtime.txt" ] \
        && ! _runtime_usable "$pin"; then
        local reconciled
        reconciled="$(reconcile_record)"
        if [ -n "$reconciled" ] && [ "$reconciled" != "$pin" ]; then
            pin="$reconciled"
            pin_source="$pin_source (stale; reconciled to $reconciled for this boot)"
        fi
    fi

    if [ -n "$pin" ]; then
        if ! _runtime_usable "$pin"; then
            refuse_pin "$pin" "$pin_source" "$pin is not usable (not installed, or \`$pin info\` fails: daemon / machine not running)"
            return 4
        fi
        case "$pin" in
            docker)
                printf 'docker\n'
                return 0
                ;;
            podman)
                if command -v podman-compose >/dev/null 2>&1; then
                    printf 'podman-compose\n'
                    return 0
                fi
                if podman compose --help >/dev/null 2>&1; then
                    printf 'podman compose\n'
                    return 0
                fi
                refuse_pin podman "$pin_source" "neither podman-compose nor \`podman compose\` is available"
                return 4
                ;;
        esac
    fi

    # Unpinned: podman first — preferred default, no group-perm gotcha.
    if _runtime_usable podman; then
        if command -v podman-compose >/dev/null 2>&1; then
            printf 'podman-compose\n'
            return 0
        fi
        if podman compose --help >/dev/null 2>&1; then
            printf 'podman compose\n'
            return 0
        fi
        # podman daemon usable but no compose front-end — log and try docker.
        log "podman daemon is reachable but neither 'podman-compose' nor 'podman compose' is available — falling through to docker" >&2
    fi

    # Then docker (only if its daemon is actually reachable).
    if _runtime_usable docker; then
        printf 'docker\n'
        return 0
    fi

    # No usable runtime.
    printf ''
}

# ---------------------------------------------------------------------------
# has_nvidia :: return 0 iff nvidia-smi reports at least one GPU.
# Soft-fails (returns 1) when nvidia-smi is absent or hangs.
# ---------------------------------------------------------------------------
has_nvidia() {
    command -v nvidia-smi >/dev/null 2>&1 || return 1
    # `timeout 2` guards against driver-bug hangs (observed on flaky
    # systems after a partial Xorg restart).
    if timeout 2 nvidia-smi -L 2>/dev/null | grep -qE '^GPU [0-9]+:'; then
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# wait_for_cdi :: poll /var/run/cdi/nvidia.yaml until it exists AND parses,
# up to $VCT_STACK_CDI_TIMEOUT seconds. Returns 0 on ready, 1 on timeout.
#
# Parse-check chain:
#   1. `yq` (preferred — handles real YAML semantics)
#   2. `python3 -c 'import yaml; yaml.safe_load(open(...))'`
#   3. Last resort: file exists + non-empty. Better than treating an
#      empty placeholder as ready.
# ---------------------------------------------------------------------------
wait_for_cdi() {
    local path="/var/run/cdi/nvidia.yaml"
    local deadline=$((SECONDS + VCT_STACK_CDI_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if [ -s "$path" ]; then
            if command -v yq >/dev/null 2>&1; then
                if yq eval . "$path" >/dev/null 2>&1; then
                    return 0
                fi
            elif command -v python3 >/dev/null 2>&1; then
                if python3 -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' "$path" 2>/dev/null; then
                    return 0
                fi
            else
                # No parser available — accept "file exists + non-empty"
                # as ready. The CDI spec is well-formed by construction
                # at this size; an empty file would mean nvidia-ctk
                # hasn't generated yet.
                return 0
            fi
        fi
        sleep 1
    done
    return 1
}

# ---------------------------------------------------------------------------
# overlay_exists :: pure helper, returns 0 iff the argument is a non-empty
# existing regular file. Path is resolved relative to $PWD (callers must
# `cd` into VCT_STACK_WORKING_DIR before invoking, OR pass an absolute
# path). Kept argument-driven (no env reads) so unit tests can pass any
# path they like.
#
# v0.2.10 (Bug L1): the original wrapper unconditionally emitted
# `-f <overlay>` whenever gpu_mode=gpu, on the assumption that VCO's own
# overlay-file pattern (`infrastructure/podman-compose.gpu.yml`) was
# universal. Some orchestrator stacks (notably those whose
# `claude_mcp_servers/compose.yaml` declares GPU devices INLINE) have GPU
# devices declared INLINE in the ollama / code_embed service blocks
# (`devices: - nvidia.com/gpu=all`) — no overlay file exists. The
# previous behaviour broke on that layout because podman-compose would
# bail out with "no such file" before any container even started. This
# helper drives the overlay-vs-inline branch in pick_compose_invocation.
# ---------------------------------------------------------------------------
overlay_exists() {
    local path="$1"
    [ -n "$path" ] && [ -f "$path" ] && [ -s "$path" ]
}

# ---------------------------------------------------------------------------
# pick_compose_invocation :: given (runtime, gpu_mode, [working_dir]), print
# the argv that should be invoked (compose binary + flags, NOT including
# `up -d`).
#
#   runtime     ∈ "docker" | "podman-compose" | "podman compose"
#   gpu_mode    ∈ "gpu" | "cpu"
#   working_dir : optional 3rd arg — directory to resolve overlay path
#                 against. Defaults to $PWD. Tests pass an explicit dir
#                 so they don't depend on cwd.
#
# Output (one line, space-separated):
#   docker compose -f compose.yaml -f infrastructure/docker-compose.gpu.yml
#   podman-compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml
#   podman compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml
#   docker compose -f compose.yaml
#   docker compose -f compose.yaml -f compose.override.yaml
#   podman-compose -f compose.yaml -f infrastructure/podman-compose.gpu.yml -f compose.override.yaml
#   ...
#
# When gpu_mode=gpu but the overlay file is missing, the invocation is
# emitted WITHOUT `-f overlay` (inline-GPU compose path) AND the global
# `OVERLAY_MISSING_WARNED=1` flag is set so callers / tests can detect
# the fall-through.
#
# When VCT_STACK_COMPOSE_OVERRIDE points at an existing non-empty file
# (resolved relative to working_dir if not absolute), the override is
# emitted as the LAST `-f` flag so it wins on conflicts (compose
# precedence rule: later files override earlier ones). PR-22
# (2026-05-16): without this explicit `-f`, podman-compose's
# auto-load is bypassed by the explicit `-f compose.yaml` and the
# launcher-managed Storage UX override (PR-10A) is silently ignored.
#
# Pure-ish function — only env reads are VCT_STACK_*_OVERLAY env vars,
# VCT_STACK_COMPOSE_FILE, and VCT_STACK_COMPOSE_OVERRIDE
# (all of which act as constants from the caller's POV).
# Tested via tests/test_launch_claude_mcp_stack_pick.py.
# ---------------------------------------------------------------------------
pick_compose_invocation() {
    local runtime="$1"
    local gpu_mode="$2"
    local working_dir="${3:-$PWD}"

    # Pick the right overlay filename per runtime. podman-compose and the
    # podman compose subcommand both use the podman overlay; docker compose
    # uses the docker overlay.
    local overlay=""
    case "$runtime" in
        docker)              overlay="$VCT_STACK_GPU_OVERLAY_DOCKER" ;;
        podman-compose)      overlay="$VCT_STACK_GPU_OVERLAY" ;;
        "podman compose")    overlay="$VCT_STACK_GPU_OVERLAY" ;;
    esac

    # Resolve overlay path against working_dir for existence-check, but
    # emit the path EXACTLY as configured in the env var (so consumers
    # that already chdir'd to working_dir keep relative paths in the
    # argv — important for log clarity and matching test expectations).
    local resolved_overlay=""
    if [ -n "$overlay" ]; then
        case "$overlay" in
            /*) resolved_overlay="$overlay" ;;
            *)  resolved_overlay="${working_dir}/${overlay}" ;;
        esac
    fi

    # The "use the overlay flag" decision is the AND of:
    #   - gpu_mode is "gpu"
    #   - an overlay path was configured for this runtime
    #   - that overlay actually exists on disk
    local use_overlay=0
    if [ "$gpu_mode" = "gpu" ] && [ -n "$overlay" ]; then
        if overlay_exists "$resolved_overlay"; then
            use_overlay=1
        else
            # Surface the inline-GPU fall-through to the caller via a
            # global flag. Tests assert on this; main() logs a warning.
            OVERLAY_MISSING_WARNED=1
        fi
    fi

    # PR-22 (2026-05-16): user-machine compose override (bind mounts,
    # alternate ports, extra services). Resolved relative to working_dir
    # when not absolute. Auto-applied iff the file exists and is non-empty.
    # podman-compose ordinarily auto-loads `compose.override.yaml`, but
    # the explicit `-f compose.yaml` below bypasses that auto-load — so
    # the override flag MUST be emitted here. Silent fall-through (no
    # `-f`) is expected when the file is absent.
    local use_override=0
    local resolved_override=""
    if [ -n "${VCT_STACK_COMPOSE_OVERRIDE:-}" ]; then
        case "$VCT_STACK_COMPOSE_OVERRIDE" in
            /*) resolved_override="$VCT_STACK_COMPOSE_OVERRIDE" ;;
            *)  resolved_override="${working_dir}/${VCT_STACK_COMPOSE_OVERRIDE}" ;;
        esac
        if [ -f "$resolved_override" ] && [ -s "$resolved_override" ]; then
            use_override=1
        fi
    fi

    # Per-runtime emission via a helper so all three compose front-ends
    # share the same flag-ordering logic. Order matters:
    #   1. `-f compose.yaml`          (base)
    #   2. `-f <gpu-overlay>`         (GPU additions, when applicable)
    #   3. `-f <user-override>`       (LAST so it wins on conflicts)
    _emit_compose_args() {
        local cmd="$1"
        local args="-f $VCT_STACK_COMPOSE_FILE"
        [ "$use_overlay" = "1" ] && args="$args -f $overlay"
        [ "$use_override" = "1" ] && args="$args -f $VCT_STACK_COMPOSE_OVERRIDE"
        printf '%s %s\n' "$cmd" "$args"
    }

    case "$runtime" in
        docker)            _emit_compose_args "docker compose" ;;
        podman-compose)    _emit_compose_args "podman-compose" ;;
        "podman compose")  _emit_compose_args "podman compose" ;;
        "")
            # No runtime — caller handles this case before invoking.
            return 1
            ;;
        *)
            # Unknown runtime — return non-zero so caller logs + bails.
            return 2
            ;;
    esac
}

# ---------------------------------------------------------------------------
# adapt_file_defaults :: fit the DEFAULT compose-file / overlay names to the
# working dir's layout (v0.2.97). Caller-set knobs are never touched.
#   - compose file: `compose.yaml` when present, else `docker-compose.yml`
#     (infrastructure/ — the installer's compose; before this the session
#     hook and the hub watchdog pointed the wrapper at infrastructure/ and it
#     asked for a compose.yaml that does not exist there).
#   - GPU overlays: `infrastructure/<overlay>` when present, else the same
#     name directly in the working dir (infrastructure/ again).
# Arg: working dir.
# ---------------------------------------------------------------------------
adapt_file_defaults() {
    local dir="$1"
    if [ -z "$_VCT_STACK_COMPOSE_FILE_SET" ] && [ ! -f "$dir/compose.yaml" ] \
        && [ -f "$dir/docker-compose.yml" ]; then
        VCT_STACK_COMPOSE_FILE="docker-compose.yml"
    fi
    if [ -z "$_VCT_STACK_GPU_OVERLAY_SET" ] && [ ! -f "$dir/$VCT_STACK_GPU_OVERLAY" ] \
        && [ -f "$dir/$(basename "$VCT_STACK_GPU_OVERLAY")" ]; then
        VCT_STACK_GPU_OVERLAY="$(basename "$VCT_STACK_GPU_OVERLAY")"
    fi
    if [ -z "$_VCT_STACK_GPU_OVERLAY_DOCKER_SET" ] && [ ! -f "$dir/$VCT_STACK_GPU_OVERLAY_DOCKER" ] \
        && [ -f "$dir/$(basename "$VCT_STACK_GPU_OVERLAY_DOCKER")" ]; then
        VCT_STACK_GPU_OVERLAY_DOCKER="$(basename "$VCT_STACK_GPU_OVERLAY_DOCKER")"
    fi
}

# ---------------------------------------------------------------------------
# is_stack_dir :: 0 iff $1 is a compose home (a directory holding the
# installer's docker-compose.yml or the legacy compose.yaml).
# own_stack_dir :: THIS wrapper's clone's compose home (infrastructure/, else
# the legacy claude_mcp_servers/), or empty.
# ---------------------------------------------------------------------------
is_stack_dir() {
    [ -n "$1" ] && [ -d "$1" ] || return 1
    [ -f "$1/docker-compose.yml" ] || [ -f "$1/compose.yaml" ] || [ -f "$1/$VCT_STACK_COMPOSE_FILE" ]
}

own_stack_dir() {
    [ -n "$_VCT_OWN_ROOT" ] || return 0
    if is_stack_dir "$_VCT_OWN_ROOT/infrastructure"; then
        printf '%s\n' "$_VCT_OWN_ROOT/infrastructure"
    elif is_stack_dir "$_VCT_OWN_ROOT/claude_mcp_servers"; then
        printf '%s\n' "$_VCT_OWN_ROOT/claude_mcp_servers"
    fi
}

# resolve_working_dir :: R8 G5 — a VCT_STACK_WORKING_DIR that is not a compose
# home (a moved / re-cloned install's old unit WorkingDirectory — PR-12 Bug C)
# is logged and replaced by THIS wrapper's own clone's, whose compose names the
# same volumes — never an empty stack elsewhere. Mirrors Resolve-WorkingDir.
resolve_working_dir() {
    is_stack_dir "$VCT_STACK_WORKING_DIR" && return 0
    local own_dir
    own_dir="$(own_stack_dir)"
    if [ -n "$own_dir" ] && [ "$own_dir" != "$VCT_STACK_WORKING_DIR" ]; then
        log "VCT_STACK_WORKING_DIR=$VCT_STACK_WORKING_DIR is not a VCO compose directory (stale?) — using this wrapper's own clone: $own_dir"
        VCT_STACK_WORKING_DIR="$own_dir"
    fi
}

# ---------------------------------------------------------------------------
# resolve_stack_python :: print an interpreter that can import vco_lib from
# THIS checkout (the wrapper lives in <root>/scripts). VCO_VENV_PYTHON wins;
# then the hooks' shared venv resolver; then <root>/.venv; then python3.
# Empty when none — the caller then refuses to compose anything.
# ---------------------------------------------------------------------------
resolve_stack_python() {
    local root="${_VCT_SCRIPT_DIR:+$_VCT_SCRIPT_DIR/..}"
    if [ -n "${VCO_VENV_PYTHON:-}" ] && [ -f "$VCO_VENV_PYTHON" ] && [ -x "$VCO_VENV_PYTHON" ]; then
        printf '%s\n' "$VCO_VENV_PYTHON"
        return 0
    fi
    if [ -n "$root" ] && [ -f "$root/templates/hooks/_lib/resolve-vco-venv.sh" ]; then
        # shellcheck source=/dev/null
        . "$root/templates/hooks/_lib/resolve-vco-venv.sh"
        VCO_VENV_PYTHON=""
        resolve_vco_venv_python "$root/templates/hooks"
        if [ -n "$VCO_VENV_PYTHON" ]; then
            printf '%s\n' "$VCO_VENV_PYTHON"
            return 0
        fi
    fi
    if [ -n "$root" ] && [ -x "$root/.venv/bin/python" ]; then
        printf '%s\n' "$root/.venv/bin/python"
        return 0
    fi
    command -v python3 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# stack_py :: run `python -m <args>` with THIS checkout first on PYTHONPATH.
# ---------------------------------------------------------------------------
stack_py() {
    local root="${_VCT_SCRIPT_DIR:+$_VCT_SCRIPT_DIR/..}"
    PYTHONPATH="${root}${PYTHONPATH:+:$PYTHONPATH}" "$STACK_PY" -m "$@"
}

# ---------------------------------------------------------------------------
# select_services :: decide the compose service list (see the header).
# Args: the service names from the command line (verb already stripped).
# Sets SELECTED_SERVICES (space-separated) and FROM_PLAN (1 when the plan
# supplied the list — the boot case that also starts adopted containers).
# Requires the plan variables (VCO_COMPOSE_SERVICES = VCO-managed list).
# ---------------------------------------------------------------------------
select_services() {
    local managed=" ${VCO_COMPOSE_SERVICES:-} " requested s
    FROM_PLAN=0
    if [ "$#" -gt 0 ]; then
        requested="$*"
    elif [ -n "$_VCT_CALLER_SERVICES_SET" ]; then
        requested="$_VCT_CALLER_SERVICES"
    else
        requested="${VCO_COMPOSE_SERVICES:-}"
        FROM_PLAN=1
    fi
    SELECTED_SERVICES=""
    for s in $requested; do
        case "$managed" in
            *" $s "*) SELECTED_SERVICES="${SELECTED_SERVICES:+$SELECTED_SERVICES }$s" ;;
            *) log "skipping '$s': not a VCO-managed service in launcher.db service_endpoints (an adopted service is never composed)" ;;
        esac
    done
}

# ---------------------------------------------------------------------------
# main :: orchestrate the boot-safe compose-up.
# ---------------------------------------------------------------------------
main() {
    log "starting (working_dir=$VCT_STACK_WORKING_DIR cdi_timeout=$VCT_STACK_CDI_TIMEOUT)"

    # Optional leading verb: the launcher passes `start` / `restart`; both
    # have always meant "bring the services up".
    case "${1:-}" in
        up|start|restart) shift ;;
    esac

    resolve_working_dir
    if [ ! -d "$VCT_STACK_WORKING_DIR" ]; then
        log "FATAL: working directory does not exist: $VCT_STACK_WORKING_DIR"
        exit 2
    fi
    cd "$VCT_STACK_WORKING_DIR" || { log "FATAL: cd $VCT_STACK_WORKING_DIR failed"; exit 2; }
    adapt_file_defaults "$VCT_STACK_WORKING_DIR"

    # The service_endpoints plan (VCO-managed list + adopted containers).
    STACK_PY="$(resolve_stack_python)"
    if [ -z "$STACK_PY" ]; then
        log "FATAL: no Python interpreter to read the service_endpoints plan (broken VCO install?) — nothing composed"
        exit 5
    fi
    # Before any runtime probe: a runtime installed outside this service's
    # PATH is found (and driven) instead of read as absent (R9 H1(b)/H5).
    augment_tool_path
    local plan_out plan_err
    plan_err="${TMPDIR:-/tmp}/vco-stack-plan.$$"
    if ! plan_out="$(stack_py vco_lib.service_lifecycle plan --shell 2>"$plan_err")"; then
        log "FATAL: vco_lib.service_lifecycle plan failed — nothing composed: $(tail -n 3 "$plan_err" 2>/dev/null | tr '\n' ' ')"
        rm -f "$plan_err"
        exit 5
    fi
    rm -f "$plan_err"
    eval "$plan_out"
    select_services "$@"
    if [ -z "$SELECTED_SERVICES" ] && { [ "$FROM_PLAN" != "1" ] || [ -z "${VCO_ADOPTED_CONTAINERS:-}" ]; }; then
        log "nothing to compose: no VCO-managed service selected"
        exit 0
    fi

    local runtime runtime_rc reason rt_err="${TMPDIR:-/tmp}/vco-stack-runtime.$$"
    runtime="$(detect_runtime 2>"$rt_err")"
    runtime_rc=$?
    cat "$rt_err" >&2 2>/dev/null
    if [ -z "$runtime" ]; then
        # rc 4: a pinned runtime is not usable — refuse_pin already logged
        # the one line that names the pin and the fix.
        if [ "$runtime_rc" -eq 4 ]; then
            reason="$(grep 'FATAL:' "$rt_err" 2>/dev/null | tail -n 1 | sed 's/^.*FATAL: //')"
        else
            reason="no container runtime found (tried runtime.txt, docker, podman-compose, podman compose)"
            log "FATAL: $reason"
        fi
        rm -f "$rt_err"
        # R8 G6: exit 3 is recorded where session start and the launcher look.
        record_boot_refusal "$reason"
        exit 3
    fi
    rm -f "$rt_err"
    log "runtime=$runtime"

    local gpu_mode="cpu"
    case "$(uname -s)" in
        Linux)
            if has_nvidia; then
                log "nvidia detected; checking CDI readiness"
                # Docker uses its own runtime hook for GPU — no CDI yaml
                # required. Only podman blocks on /var/run/cdi/nvidia.yaml.
                if [ "$runtime" = "docker" ]; then
                    log "docker runtime: skipping CDI wait (docker uses runtime hook)"
                    gpu_mode="gpu"
                else
                    if wait_for_cdi; then
                        log "CDI ready (/var/run/cdi/nvidia.yaml parseable)"
                        gpu_mode="gpu"
                    else
                        log "WARNING: CDI yaml not ready after ${VCT_STACK_CDI_TIMEOUT}s — degrading to CPU-only compose"
                        gpu_mode="cpu"
                    fi
                fi
            else
                log "no NVIDIA GPU detected (nvidia-smi absent or empty) — CPU-only compose"
                gpu_mode="cpu"
            fi
            ;;
        *)
            # Non-Linux: this wrapper is intended for systemd / Linux only.
            # Other OSes don't have systemd; the unit template install is a
            # no-op on macOS/Windows. If somehow invoked, default to CPU.
            log "non-Linux ($(uname -s)) — defaulting to CPU compose"
            gpu_mode="cpu"
            ;;
    esac

    # OVERLAY_MISSING_WARNED is set by pick_compose_invocation when
    # gpu_mode=gpu but the configured overlay file doesn't exist. Reset
    # it here so a previous invocation's state can't leak in.
    OVERLAY_MISSING_WARNED=0
    local argv
    if ! argv="$(pick_compose_invocation "$runtime" "$gpu_mode" "$VCT_STACK_WORKING_DIR")"; then
        log "FATAL: pick_compose_invocation rejected runtime=$runtime gpu_mode=$gpu_mode"
        exit 4
    fi
    if [ "${OVERLAY_MISSING_WARNED:-0}" = "1" ]; then
        local missing_overlay
        case "$runtime" in
            docker)            missing_overlay="$VCT_STACK_GPU_OVERLAY_DOCKER" ;;
            podman-compose|"podman compose") missing_overlay="$VCT_STACK_GPU_OVERLAY" ;;
            *)                 missing_overlay="(unknown)" ;;
        esac
        log "WARNING: inline-GPU compose assumed — overlay file '${VCT_STACK_WORKING_DIR}/${missing_overlay}' not found, proceeding without overlay"
    fi

    # Adopted containers (somebody else's, started BY NAME, never composed)
    # come up on the boot path only — an explicit list is a caller asking
    # for those services and nothing else.
    local name
    if [ "$FROM_PLAN" = "1" ]; then
        local rt_bin="podman"
        [ "$runtime" = "docker" ] && rt_bin="docker"
        for name in ${VCO_ADOPTED_CONTAINERS:-}; do
            if "$rt_bin" start "$name" >/dev/null 2>&1; then
                log "started adopted container $name (by name — never re-created)"
            else
                log "WARNING: could not start adopted container $name"
            fi
        done
    fi

    # The `up` argv for EXACTLY the selected services — from the one home of
    # the rule (`--no-deps`; code_embed only with the gpu profile, and not at
    # all in CPU mode). An empty list is NO compose call, never a bare up.
    local up_line
    local -a up_args=() build_flag=()
    [ "${VCT_STACK_BUILD:-}" = "1" ] && build_flag=(--build)
    if ! up_line="$(stack_py vco_lib.service_lifecycle compose-args --shell \
            --services "$SELECTED_SERVICES" --gpu-mode "$gpu_mode" \
            "${build_flag[@]}")"; then
        log "FATAL: vco_lib.service_lifecycle compose-args failed for '$SELECTED_SERVICES' — nothing composed"
        exit 5
    fi
    if [ -z "$up_line" ]; then
        log "nothing to compose (selected: '${SELECTED_SERVICES}', gpu_mode=$gpu_mode)"
        exit 0
    fi
    # `up_line` is shlex-quoted by the Python side; this only splits it.
    eval "up_args=($up_line)"
    log "exec: $argv $up_line"

    # shellcheck disable=SC2086
    # Intentional word splitting — `argv` is a space-separated string
    # built from a controlled set of values inside `pick_compose_invocation`.
    $argv "${up_args[@]}"
    local rc=$?
    log "compose exited rc=$rc"
    # Exit 125 from podman-compose means "one or more containers failed
    # to start" — we tolerate that at the unit level (other containers'
    # restart policy recovers them).
    case "$rc" in
        0|125) exit 0 ;;
        *)     exit "$rc" ;;
    esac
}

# Only run main when executed directly, NOT when sourced for tests.
# Idiom: `BASH_SOURCE[0]` is this file; `$0` is the invocation entry.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
