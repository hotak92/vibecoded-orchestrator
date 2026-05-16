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
#   1. Runtime detection (docker preferred, else podman-compose, else podman compose).
#      An optional state/install/runtime.txt is the authoritative source.
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
#                                 (default: ${HOME}/Desktop/PROGETTI/Claude/claude_mcp_servers)
#   - VCT_STACK_LOG_FILE      — log path (default: /tmp/claude-mcp-containers.log)
#   - VCT_STACK_CDI_TIMEOUT   — seconds to wait for CDI yaml (default: 30)
#   - VCT_STACK_RUNTIME_FILE  — explicit runtime.txt path. When set, this
#                                 wins over all candidate-path search (PR-12
#                                 Bug B). When unset, candidates probed in
#                                 order — see resolve_runtime_file().
#   - VCT_ORCHESTRATOR_ROOT   — orchestrator install root (used as one of
#                                 the runtime.txt candidate-path roots).
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

VCT_STACK_WORKING_DIR="${VCT_STACK_WORKING_DIR:-${HOME}/Desktop/PROGETTI/Claude/claude_mcp_servers}"
VCT_STACK_LOG_FILE="${VCT_STACK_LOG_FILE:-/tmp/claude-mcp-containers.log}"
VCT_STACK_CDI_TIMEOUT="${VCT_STACK_CDI_TIMEOUT:-30}"
# NOTE (PR-12 Bug B): VCT_STACK_RUNTIME_FILE is no longer eagerly defaulted
# to ${VCT_STACK_WORKING_DIR}/state/install/runtime.txt — that single path
# was too narrow when systemd's WorkingDirectory pointed at a stale install
# location (Bug C). resolve_runtime_file() now probes multiple candidates
# and picks the first one that contains a USABLE runtime token.
VCT_STACK_GPU_OVERLAY="${VCT_STACK_GPU_OVERLAY:-infrastructure/podman-compose.gpu.yml}"
VCT_STACK_GPU_OVERLAY_DOCKER="${VCT_STACK_GPU_OVERLAY_DOCKER:-infrastructure/docker-compose.gpu.yml}"
VCT_STACK_COMPOSE_FILE="${VCT_STACK_COMPOSE_FILE:-compose.yaml}"
VCT_STACK_COMPOSE_OVERRIDE="${VCT_STACK_COMPOSE_OVERRIDE:-compose.override.yaml}"

# Resolve the directory that contains THIS script — used as one fallback
# root for runtime.txt resolution. Works whether the script is sourced or
# executed directly.
_VCT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || _VCT_SCRIPT_DIR=""

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
# resolve_runtime_file :: prints the path to the FIRST runtime.txt candidate
# that exists on disk and contains a usable runtime token. Empty string if
# none usable (PR-12 Bug B).
#
# Probe order (first hit wins):
#   1. ${VCT_STACK_RUNTIME_FILE} if explicitly set (caller override).
#   2. ${VCT_STACK_WORKING_DIR}/state/install/runtime.txt
#   3. ${VCT_ORCHESTRATOR_ROOT}/state/install/runtime.txt
#   4. <script_dir>/../state/install/runtime.txt   (script lives in
#      <orchestrator>/scripts/, so .. is the orchestrator root).
#
# A candidate is "usable" iff:
#   - the file exists + is readable + non-empty, AND
#   - the token it contains corresponds to a runtime whose daemon access
#     check passes (_runtime_usable).
#
# We log every candidate that exists-but-is-not-usable so a stale unit
# WorkingDirectory pointing at a dead install doesn't fail silently.
# ---------------------------------------------------------------------------
resolve_runtime_file() {
    local candidates=()
    if [ -n "${VCT_STACK_RUNTIME_FILE:-}" ]; then
        candidates+=("$VCT_STACK_RUNTIME_FILE")
    fi
    if [ -n "${VCT_STACK_WORKING_DIR:-}" ]; then
        candidates+=("${VCT_STACK_WORKING_DIR}/state/install/runtime.txt")
    fi
    if [ -n "${VCT_ORCHESTRATOR_ROOT:-}" ]; then
        candidates+=("${VCT_ORCHESTRATOR_ROOT}/state/install/runtime.txt")
    fi
    if [ -n "${_VCT_SCRIPT_DIR:-}" ]; then
        candidates+=("${_VCT_SCRIPT_DIR}/../state/install/runtime.txt")
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
        [ -z "$token" ] && continue
        if _runtime_usable "$token"; then
            printf '%s\n' "$cand"
            return 0
        else
            log "runtime.txt at $cand names '$token' but its daemon is not reachable — falling through to live probe"
        fi
    done
    printf ''
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
# Both probes carry a 5s timeout — a hung daemon socket must NOT block
# boot indefinitely.
# ---------------------------------------------------------------------------
_runtime_usable() {
    local token="$1"
    case "$token" in
        docker)
            command -v docker >/dev/null 2>&1 || return 1
            local info_out
            if ! info_out="$(timeout 5 docker info 2>&1)"; then
                return 1
            fi
            # Server: section presence is the daemon-access proxy.
            printf '%s\n' "$info_out" | grep -qE '^(Server:|Server Version:)' || return 1
            return 0
            ;;
        podman)
            command -v podman >/dev/null 2>&1 || return 1
            timeout 5 podman info >/dev/null 2>&1 || return 1
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# ---------------------------------------------------------------------------
# detect_runtime :: prints one of "docker", "podman-compose", "podman compose", or ""
#
# Order (PR-12 Bug A — every candidate validated via _runtime_usable):
#   1. resolve_runtime_file → token from runtime.txt → expand to compose
#      invocation IFF the runtime is usable. Otherwise log + fall through.
#   2. Probe podman first (preferred default — it's the VCO-recommended
#      runtime, has no group-permission gotcha).
#   3. Probe docker.
#   4. Empty (no usable runtime).
#
# A "usable" docker means `docker info` reaches the daemon (Server section
# present); a "usable" podman means `podman info` succeeds. This prevents
# the boot-time "permission denied" failure when Docker Desktop is present
# but the user is not in the `docker` group.
# ---------------------------------------------------------------------------
detect_runtime() {
    # 1. runtime.txt — only honored if its named runtime is actually usable.
    local runtime_file
    runtime_file="$(resolve_runtime_file)"
    if [ -n "$runtime_file" ]; then
        local persisted
        persisted="$(head -n 1 "$runtime_file" 2>/dev/null | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
        case "$persisted" in
            docker)
                # _runtime_usable already validated docker daemon access in
                # resolve_runtime_file; we trust that result here.
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
                # podman is usable but no compose front-end available —
                # fall through to live probe (which will also fail, but at
                # least surfaces the right diagnostic).
                ;;
        esac
    fi

    # 2. Probe podman first — preferred default, no group-perm gotcha.
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
        log "podman daemon is reachable but neither 'podman-compose' nor 'podman compose' is available — falling through to docker"
    fi

    # 3. Probe docker (only if its daemon is actually reachable).
    if _runtime_usable docker; then
        printf 'docker\n'
        return 0
    fi

    # 4. No usable runtime.
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
# universal. The user's actual Claude orchestrator stack at
# ~/Desktop/PROGETTI/Claude/claude_mcp_servers/compose.yaml has its GPU
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
# main :: orchestrate the boot-safe compose-up.
# ---------------------------------------------------------------------------
main() {
    log "starting (working_dir=$VCT_STACK_WORKING_DIR cdi_timeout=$VCT_STACK_CDI_TIMEOUT)"

    if [ ! -d "$VCT_STACK_WORKING_DIR" ]; then
        log "FATAL: working directory does not exist: $VCT_STACK_WORKING_DIR"
        exit 2
    fi
    cd "$VCT_STACK_WORKING_DIR" || { log "FATAL: cd $VCT_STACK_WORKING_DIR failed"; exit 2; }

    local runtime
    runtime="$(detect_runtime)"
    if [ -z "$runtime" ]; then
        log "FATAL: no container runtime found (tried runtime.txt, docker, podman-compose, podman compose)"
        exit 3
    fi
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
    log "exec: $argv up -d"

    # shellcheck disable=SC2086
    # Intentional word splitting — `argv` is a space-separated string
    # built from a controlled set of values inside `pick_compose_invocation`.
    $argv up -d
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
