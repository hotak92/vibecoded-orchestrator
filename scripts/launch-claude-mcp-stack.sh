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
#   - VCT_STACK_RUNTIME_FILE  — runtime.txt path (default: ${VCT_STACK_WORKING_DIR}/state/install/runtime.txt)
#   - VCT_STACK_GPU_OVERLAY   — overlay filename for podman path
#                                 (default: infrastructure/podman-compose.gpu.yml)
#   - VCT_STACK_GPU_OVERLAY_DOCKER — overlay for docker path
#                                 (default: infrastructure/docker-compose.gpu.yml)
# ---------------------------------------------------------------------------

VCT_STACK_WORKING_DIR="${VCT_STACK_WORKING_DIR:-${HOME}/Desktop/PROGETTI/Claude/claude_mcp_servers}"
VCT_STACK_LOG_FILE="${VCT_STACK_LOG_FILE:-/tmp/claude-mcp-containers.log}"
VCT_STACK_CDI_TIMEOUT="${VCT_STACK_CDI_TIMEOUT:-30}"
VCT_STACK_RUNTIME_FILE="${VCT_STACK_RUNTIME_FILE:-${VCT_STACK_WORKING_DIR}/state/install/runtime.txt}"
VCT_STACK_GPU_OVERLAY="${VCT_STACK_GPU_OVERLAY:-infrastructure/podman-compose.gpu.yml}"
VCT_STACK_GPU_OVERLAY_DOCKER="${VCT_STACK_GPU_OVERLAY_DOCKER:-infrastructure/docker-compose.gpu.yml}"
VCT_STACK_COMPOSE_FILE="${VCT_STACK_COMPOSE_FILE:-compose.yaml}"

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
# detect_runtime :: prints one of "docker", "podman-compose", "podman compose", or ""
#
# Order:
#   1. $VCT_STACK_RUNTIME_FILE (state/install/runtime.txt) if present + non-empty.
#      Normalised: "docker" → "docker"; "podman" → "podman-compose" if
#      `podman-compose` exists else "podman compose".
#   2. `which docker`        → "docker"
#   3. `which podman-compose` → "podman-compose"
#   4. `command -v podman` AND `podman compose --help` works → "podman compose"
#   5. empty (no runtime found)
#
# Pure helper — no side effects beyond stdout.
# ---------------------------------------------------------------------------
detect_runtime() {
    # 1. runtime.txt
    if [ -r "$VCT_STACK_RUNTIME_FILE" ]; then
        local persisted
        persisted="$(head -n 1 "$VCT_STACK_RUNTIME_FILE" 2>/dev/null | tr -d '[:space:]')"
        case "$persisted" in
            docker)
                if command -v docker >/dev/null 2>&1; then
                    printf 'docker\n'
                    return 0
                fi
                ;;
            podman)
                if command -v podman-compose >/dev/null 2>&1; then
                    printf 'podman-compose\n'
                    return 0
                fi
                if command -v podman >/dev/null 2>&1 && podman compose --help >/dev/null 2>&1; then
                    printf 'podman compose\n'
                    return 0
                fi
                ;;
        esac
    fi

    # 2-4. Probe in preference order.
    if command -v docker >/dev/null 2>&1; then
        printf 'docker\n'
        return 0
    fi
    if command -v podman-compose >/dev/null 2>&1; then
        printf 'podman-compose\n'
        return 0
    fi
    if command -v podman >/dev/null 2>&1 && podman compose --help >/dev/null 2>&1; then
        printf 'podman compose\n'
        return 0
    fi
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
#   ...
#
# When gpu_mode=gpu but the overlay file is missing, the invocation is
# emitted WITHOUT `-f overlay` (inline-GPU compose path) AND the global
# `OVERLAY_MISSING_WARNED=1` flag is set so callers / tests can detect
# the fall-through.
#
# Pure-ish function — only env reads are VCT_STACK_*_OVERLAY env vars
# (which act as constants from the caller's POV) and VCT_STACK_COMPOSE_FILE.
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

    case "$runtime" in
        docker)
            if [ "$use_overlay" = "1" ]; then
                printf 'docker compose -f %s -f %s\n' \
                    "$VCT_STACK_COMPOSE_FILE" "$overlay"
            else
                printf 'docker compose -f %s\n' "$VCT_STACK_COMPOSE_FILE"
            fi
            ;;
        podman-compose)
            if [ "$use_overlay" = "1" ]; then
                printf 'podman-compose -f %s -f %s\n' \
                    "$VCT_STACK_COMPOSE_FILE" "$overlay"
            else
                printf 'podman-compose -f %s\n' "$VCT_STACK_COMPOSE_FILE"
            fi
            ;;
        "podman compose")
            if [ "$use_overlay" = "1" ]; then
                printf 'podman compose -f %s -f %s\n' \
                    "$VCT_STACK_COMPOSE_FILE" "$overlay"
            else
                printf 'podman compose -f %s\n' "$VCT_STACK_COMPOSE_FILE"
            fi
            ;;
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
