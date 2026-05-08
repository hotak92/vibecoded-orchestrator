#!/usr/bin/env bash
# vct_secrets_resolve.sh — bridge between bundled wrappers/hooks and the
# launcher's hub HTTP API. Reads a single secret value from
# `GET /api/v1/projects/{id}/env?key=NAME` and prints it on stdout.
#
# This is the post-Fix-#3 (0.1.7) replacement for the old
# `cat ~/.vct-secrets/<key>` pattern. The launcher's keychain is the
# authoritative store; the hub adds the per-project active-flag gate
# and (in the future) per-project access matrix. No file-side mirror,
# no hard-coded allowlist — every secret active for the project flows
# through this single path.
#
# Usage:
#   vct_secrets_resolve.sh <project_id_or_folder> <secret_key>
#       Print the value of <secret_key> for the resolved project.
#       Exit codes: 0=ok, 1=hub unreachable, 2=project not registered,
#                   3=key not active for this project, 4=key not found
#                   in the hub's response (treated like 3 for callers).
#
#   vct_secrets_resolve.sh resolve-project <folder>
#       Print the project_id (UUID) registered for <folder>. Same exit
#       codes (1=hub unreachable, 2=project not registered).
#
# Hub discovery:
#   1. $VCT_HUB_PORT env var (set by tests / dev launchers)
#   2. ${VCT_STATE_DIR:-$HOME/.vct}/hub.port (written by the launcher
#      on startup; mirrors `launcher/src-tauri/src/hub/server.rs`)
#   3. Default 7700 (matches `DEFAULT_PORT` in server.rs).
#
# Project ID resolution (when the first arg looks like a path, not a UUID):
#   GET /api/v1/projects/by-path?path=<folder>  → project_id
#
# Dependencies: curl (always), jq (preferred) OR python3 (fallback).
# Both are available on every supported platform; we never silently
# fail because of a missing parser.
#
# Notes for migrating wrappers:
#   - This script writes ONLY to stdout on success. All diagnostics go
#     to stderr so the caller can `value=$(./vct_secrets_resolve.sh ...)`
#     without inheriting log lines.
#   - No caching. The hub itself is the source-of-truth; if the user
#     pauses or rotates a secret, the next call sees the new state.

set -euo pipefail

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# This file is byte-identical between `templates/scripts/` (shipped to
# user projects) and `.claude/scripts/` (orchestrator's own copy). The
# template-drift gate (`scripts/check_template_drift.py`) enforces that.
# No project-root resolution divergence — the hub owns the lookup, so
# both copies need the same logic.
# VCO-REWIRE-END: orchestrator-root-resolution

err() { printf '[vct-secrets-resolve] %s\n' "$*" >&2; }

# ── Hub port discovery ──────────────────────────────────────────────────
hub_port() {
    if [[ -n "${VCT_HUB_PORT:-}" ]]; then
        printf '%s\n' "$VCT_HUB_PORT"
        return 0
    fi
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    local port_file="$state_dir/hub.port"
    if [[ -f "$port_file" ]]; then
        local p
        p=$(tr -d '[:space:]' < "$port_file")
        if [[ -n "$p" ]]; then
            printf '%s\n' "$p"
            return 0
        fi
    fi
    # Default — matches launcher's server.rs::DEFAULT_PORT.
    printf '7700\n'
}

# ── HTTP helpers ────────────────────────────────────────────────────────
#
# We use curl directly so we can capture the HTTP status separately from
# the body. `--fail` would conflate 404 (project not found) and connection
# refused (hub unreachable) into the same exit code, which is exactly the
# distinction the contract needs to preserve.
hub_get() {
    # $1 = path-with-query (no leading slash)
    # echoes "<status>\t<body>" on stdout; exit non-zero only when curl
    # itself fails (e.g. connection refused, not 4xx).
    local path="$1"
    local port
    port=$(hub_port)
    local url="http://127.0.0.1:${port}/api/v1/${path}"
    local body status
    if ! body=$(curl --silent --show-error --max-time 5 --output - --write-out '\n%{http_code}' "$url" 2>&1); then
        return 1
    fi
    # Last line is the status; everything before is the body.
    status="${body##*$'\n'}"
    body="${body%$'\n'*}"
    printf '%s\t%s\n' "$status" "$body"
}

# ── JSON extraction ─────────────────────────────────────────────────────
#
# Try jq first (faster, ubiquitous). Fall back to a one-shot python3
# extractor when jq isn't on PATH — Python 3 is installed on every
# platform we ship the orchestrator on.
json_extract() {
    # $1 = json body, $2 = jq-style path (e.g. ".id" or ".error.code"
    #                                       or '."GITHUB_TOKEN"')
    local body="$1" path="$2"
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$body" | jq -r "$path // empty"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        # Pipe the body via stdin so we don't have to escape it; pass
        # the path as argv. Python's split-on-quoted-segments handles
        # both `.id` and `."GITHUB_TOKEN"` correctly.
        printf '%s' "$body" | VCT_JSON_PATH="$path" python3 -c '
import json, os, sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)

path = os.environ.get("VCT_JSON_PATH", "")
keys = []
i = 0
n = len(path)
while i < n:
    if path[i] == ".":
        i += 1
        if i < n and path[i] == "\"":
            j = path.find("\"", i + 1)
            if j == -1:
                sys.exit(0)
            keys.append(path[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and path[j] not in (".",):
                j += 1
            if j > i:
                keys.append(path[i:j])
            i = j
    else:
        sys.exit(0)

out = data
for k in keys:
    if isinstance(out, dict) and k in out:
        out = out[k]
    else:
        sys.exit(0)
if isinstance(out, str):
    sys.stdout.write(out)
else:
    sys.stdout.write(json.dumps(out))
'
        return 0
    fi
    err "neither jq nor python3 found on PATH; cannot parse hub response"
    return 1
}

# ── Project ID resolution ───────────────────────────────────────────────
#
# Heuristic: if the arg starts with `/` or `./` or `../` or contains a
# path separator → treat as a folder. Otherwise → treat as a project_id
# (UUID hex). Cheap to be wrong here: a misclassified path will 404 the
# UUID lookup; a misclassified id (rare) will 404 the by-path lookup.
looks_like_path() {
    case "$1" in
        /*|./*|../*) return 0 ;;
        *) [[ "$1" == */* ]] && return 0 ;;
    esac
    return 1
}

resolve_project_id() {
    # $1 = arg (path or id). Echoes the project_id on success.
    local arg="$1"
    if ! looks_like_path "$arg"; then
        # Already a project_id; trust it. (The hub will 404 on the env
        # call if it's bogus — same exit-code mapping.)
        printf '%s' "$arg"
        return 0
    fi
    # Folder lookup via /projects/by-path?path=<arg>.
    local result status body
    if ! result=$(hub_get "projects/by-path?path=$(url_encode "$arg")"); then
        err "hub unreachable; is the launcher running?"
        return 1
    fi
    status="${result%%$'\t'*}"
    body="${result#*$'\t'}"
    case "$status" in
        200)
            local id
            id=$(json_extract "$body" '.id')
            if [[ -z "$id" ]]; then
                err "hub returned 200 but no .id field; body=$body"
                return 2
            fi
            printf '%s' "$id"
            ;;
        404)
            err "no project registered at path: $arg"
            return 2
            ;;
        400)
            err "hub rejected path query: $body"
            return 2
            ;;
        *)
            err "hub returned status $status for by-path lookup; body=$body"
            return 1
            ;;
    esac
}

# ── URL-encode (POSIX-y, no Python dep here) ────────────────────────────
url_encode() {
    local s="$1" out="" c
    local i len=${#s}
    for ((i = 0; i < len; i++)); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9._~/-]) out+="$c" ;;
            *) out+=$(printf '%%%02X' "'$c") ;;
        esac
    done
    printf '%s' "$out"
}

# ── Main subcommand: read a single key ──────────────────────────────────
read_key() {
    # $1 = project_id_or_folder, $2 = key
    local pid_arg="$1" key="$2"
    local pid
    if ! pid=$(resolve_project_id "$pid_arg"); then
        # resolve_project_id already mapped the exit code (1 or 2).
        return $?
    fi
    local result status body
    if ! result=$(hub_get "projects/$(url_encode "$pid")/env?key=$(url_encode "$key")"); then
        err "hub unreachable; is the launcher running?"
        return 1
    fi
    status="${result%%$'\t'*}"
    body="${result#*$'\t'}"
    case "$status" in
        200)
            local val
            val=$(json_extract "$body" ".\"$key\"")
            if [[ -z "$val" ]]; then
                err "hub returned 200 but no $key field; body=$body"
                return 4
            fi
            printf '%s' "$val"
            ;;
        404)
            local code
            code=$(json_extract "$body" '.error.code')
            case "$code" in
                project_not_found)
                    err "project $pid not found in launcher.db"
                    return 2
                    ;;
                key_not_active)
                    err "key $key not active for project $pid (paused, or not declared by any installed module)"
                    return 3
                    ;;
                *)
                    err "hub 404 with unknown code $code; body=$body"
                    return 4
                    ;;
            esac
            ;;
        400)
            err "hub rejected request: $body"
            return 4
            ;;
        *)
            err "hub returned status $status; body=$body"
            return 1
            ;;
    esac
}

# ── Entry point ─────────────────────────────────────────────────────────
main() {
    if [[ $# -lt 2 ]]; then
        cat >&2 <<EOF
Usage:
  $0 <project_id_or_folder> <secret_key>
  $0 resolve-project <folder>

Exit codes:
  0  success
  1  hub unreachable
  2  project not registered
  3  key not active for this project
  4  key not found in hub response
EOF
        exit 64
    fi
    case "$1" in
        resolve-project)
            resolve_project_id "$2"
            ;;
        *)
            read_key "$1" "$2"
            ;;
    esac
}

main "$@"
