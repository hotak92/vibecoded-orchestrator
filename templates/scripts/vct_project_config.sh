#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_project_config.sh — bridge between project hooks/scripts and the
# launcher's vct-hub. Reads the resolved project config from
# `GET /api/v1/projects/{id}/config` and prints either the full JSON
# (default) or a single field value (--field NAME).
#
# This is the v0.2.21 sibling of `vct_secrets_resolve.sh`. Same
# discovery (hub.port + hub.token), same auth (Bearer token off argv
# via `curl --header @-`), same exit-code shape with one addition:
# exit 3 means "service misconfigured" (primary KG binding missing
# because the launcher's startup backfill hasn't run / failed). See
# `.claude/context/plans/v0.2.21-resolver-design.md` §3 for the full
# contract.
#
# Usage:
#   vct_project_config.sh <project_folder> [--field NAME]
#       Print the full config JSON for the project at <project_folder>.
#       With --field NAME, print only that field's value (unwrapped).
#   vct_project_config.sh resolve-project <project_folder>
#       Print the project_id (UUID) for <project_folder>.
#
# Exit codes:
#   0  success
#   1  hub unreachable (no hub.token, conn refused, 401 stale token, 5xx)
#   2  project not registered (404 project_not_found)
#   3  service misconfigured (503 — primary KG binding missing)
#   4  field not found (--field NAME, NAME not in config)
#   64 usage error
#
# Hub discovery:
#   1. $VCT_HUB_PORT  / $VCT_HUB_TOKEN env vars (tests / dev launchers)
#   2. ${VCT_STATE_DIR:-$HOME/.vct}/hub.port  + hub.token (written by
#      the launcher on startup; mode 0o600 on Unix).
#   3. Port default: 7700 (matches launcher server.rs::DEFAULT_PORT).
#      Token has no default — missing file maps to exit 1.
#
# Dependencies: curl (always), jq (preferred) OR python3 (fallback).
#
# Stderr policy: on hub unreachable / project missing / misconfigured,
# this script emits ONE diagnostic line to stderr before exiting
# non-zero. Step 17 will add JSONL-based rate-limiting; for now every
# call writes a single line (callers that want quiet failures use
# `2>/dev/null` per design doc §3.6).

set -euo pipefail

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# This file is byte-identical between `templates/scripts/` (shipped to
# user projects) and `.claude/scripts/` (orchestrator's own copy). The
# template-drift gate (`scripts/check_template_drift.py`) enforces that.
# No project-root resolution divergence — the hub owns the lookup, so
# both copies need the same logic.
# VCO-REWIRE-END: orchestrator-root-resolution

err() { printf '[vct-project-config] %s\n' "$*" >&2; }

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

# ── Hub auth-token discovery ────────────────────────────────────────────
hub_token() {
    if [[ -n "${VCT_HUB_TOKEN:-}" ]]; then
        printf '%s' "$VCT_HUB_TOKEN"
        return 0
    fi
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    local token_file="$state_dir/hub.token"
    if [[ -f "$token_file" ]]; then
        tr -d '[:space:]' < "$token_file"
        return 0
    fi
    printf ''
}

# ── HTTP helper ─────────────────────────────────────────────────────────
# `--header @-` reads the Authorization header from stdin so the token
# never appears in argv (`ps`/`/proc/<pid>/cmdline`). Returns:
#   exit 0 + stdout "<status>\t<body>"  on a completed request
#   exit 1                              on curl failure (conn refused, etc.)
#   exit 2                              on missing token (no hub.token)
hub_get() {
    local path="$1"
    local port token
    port=$(hub_port)
    token=$(hub_token)
    if [[ -z "$token" ]]; then
        return 2
    fi
    local url="http://127.0.0.1:${port}/api/v1/${path}"
    local body status
    if ! body=$(curl --silent --show-error --max-time 5 \
                     --header @- \
                     --output - --write-out '\n%{http_code}' "$url" \
                     <<<"Authorization: Bearer ${token}" 2>&1); then
        return 1
    fi
    status="${body##*$'\n'}"
    body="${body%$'\n'*}"
    printf '%s\t%s\n' "$status" "$body"
}

# ── URL-encode ──────────────────────────────────────────────────────────
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

# ── JSON extraction (jq → python3 fallback) ─────────────────────────────
json_extract() {
    local body="$1" path="$2"
    if command -v jq >/dev/null 2>&1; then
        printf '%s' "$body" | jq -r "$path // empty"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
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
looks_like_path() {
    case "$1" in
        /*|./*|../*) return 0 ;;
        *) [[ "$1" == */* ]] && return 0 ;;
    esac
    return 1
}

resolve_project_id() {
    local arg="$1"
    if ! looks_like_path "$arg"; then
        printf '%s' "$arg"
        return 0
    fi
    local result status body rc
    set +e
    result=$(hub_get "projects/by-path?path=$(url_encode "$arg")")
    rc=$?
    set -e
    if [[ $rc -eq 2 ]]; then
        err "hub.token missing; is the launcher running?"
        return 1
    fi
    if [[ $rc -ne 0 ]]; then
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
        401)
            err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again."
            return 1
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

# ── Main: fetch config ──────────────────────────────────────────────────
fetch_config() {
    local pid="$1" field="${2:-}"
    local path="projects/$(url_encode "$pid")/config"
    if [[ -n "$field" ]]; then
        path="${path}?key=$(url_encode "$field")"
    fi

    local result status body rc
    set +e
    result=$(hub_get "$path")
    rc=$?
    set -e
    if [[ $rc -eq 2 ]]; then
        err "hub.token missing; is the launcher running?"
        return 1
    fi
    if [[ $rc -ne 0 ]]; then
        err "hub unreachable; is the launcher running?"
        return 1
    fi

    status="${result%%$'\t'*}"
    body="${result#*$'\t'}"

    case "$status" in
        200)
            if [[ -n "$field" ]]; then
                # Single-field envelope: {"<field>": <value>}. Unwrap.
                local val
                val=$(json_extract "$body" ".\"$field\"")
                if [[ -z "$val" ]]; then
                    err "field $field decoded empty from hub response"
                    return 4
                fi
                printf '%s' "$val"
            else
                printf '%s' "$body"
            fi
            ;;
        401)
            err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again."
            return 1
            ;;
        404)
            local code
            code=$(json_extract "$body" '.error.code')
            case "$code" in
                project_not_found)
                    err "project $pid not registered in launcher.db"
                    return 2
                    ;;
                field_not_found)
                    err "field $field not in config for project $pid"
                    return 4
                    ;;
                *)
                    err "hub 404 with unknown code $code; body=$body"
                    return 4
                    ;;
            esac
            ;;
        503)
            err "hub returned 503 service_misconfigured for project $pid (primary KG binding missing — fix in launcher GUI)"
            return 3
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
usage() {
    cat >&2 <<EOF
Usage:
  $0 <project_folder> [--field NAME]
  $0 resolve-project <project_folder>

Exit codes:
  0  success
  1  hub unreachable
  2  project not registered
  3  service misconfigured (primary KG binding missing)
  4  field not found
  64 usage error
EOF
}

main() {
    if [[ $# -lt 1 ]]; then
        usage
        exit 64
    fi

    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        resolve-project)
            if [[ $# -lt 2 ]]; then
                err "resolve-project requires <folder>"
                exit 64
            fi
            resolve_project_id "$2"
            ;;
        *)
            local project_arg="$1" field=""
            shift
            while [[ $# -gt 0 ]]; do
                case "$1" in
                    --field)
                        if [[ $# -lt 2 || -z "${2:-}" ]]; then
                            err "--field requires NAME"
                            exit 64
                        fi
                        field="$2"
                        shift 2
                        ;;
                    *)
                        err "unknown option: $1"
                        exit 64
                        ;;
                esac
            done
            local pid
            pid=$(resolve_project_id "$project_arg") || exit $?
            fetch_config "$pid" "$field"
            ;;
    esac
}

main "$@"
