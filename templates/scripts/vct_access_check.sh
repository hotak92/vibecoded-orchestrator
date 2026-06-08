#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_access_check.sh — KG access matrix gate client (v0.2.49 Phase 8).
#
# Queries the launcher's vct-hub for the access level a project has on
# a collection. Used by hooks + scripts that WRITE to Weaviate to
# enforce the access matrix at write time (the matrix has been a
# read-gate only until v0.2.49; this client closes the symmetry).
#
# Hub contract:
#   GET http://127.0.0.1:${VCT_HUB_PORT}/api/v1/projects/{project_id}/access/{collection_name}
#   Headers: Authorization: Bearer <hub.token>
#   Response 200: {"level": "read" | "write" | "none"}
#   Response 404: project_id not found OR collection has no access row → fail-open default "write"
#   Response 401/5xx OR timeout → fail-open default "write" + WARNING to stderr
#
# Fail-open contract: the script ALWAYS prints a valid level + exits 0.
# Hub unreachable / authentication failed / project missing → print "write"
# + emit ONE stderr warning + log a dropped-write-metric row. This is
# DELIBERATE: a closed-circuit policy would brick all KG writes during a
# launcher restart, which is unacceptable UX. The warning surfaces the
# degraded state so the user notices.
#
# Usage:
#   vct_access_check.sh <project_id> <collection_name>
#     Prints "read" | "write" | "none" on stdout. Exit 0 always.
#
# Hub discovery + auth: mirrors vct_project_config.sh (port file, token
# file, env-var overrides for tests).
#
# Stderr policy: one WARNING per (PID, hub-unreachable-kind) per 5-min
# window — rate-limited via $VCT_STATE_DIR/cache/access_check_warn.jsonl.
# Set VCO_HOOK_DEBUG=1 to bypass rate limit.
#
# Dropped-write metric: every fail-open emission appends a row to
# $VCT_STATE_DIR/cache/dropped_writes.jsonl with timestamp + project_id
# + collection + reason. Caller can ingest this for observability.

set -euo pipefail

readonly RESOLVER_PROTOCOL_VERSION=1

# ── Args ──────────────────────────────────────────────────────────────
if [[ $# -ne 2 ]]; then
    printf 'usage: vct_access_check.sh <project_id> <collection_name>\n' >&2
    exit 64
fi
project_id="$1"
collection="$2"

# ── Hub discovery (mirrors vct_project_config.sh contract) ─────────────
state_dir="${VCT_STATE_DIR:-$HOME/.vct}"

hub_port=""
if [[ -n "${VCT_HUB_PORT:-}" ]]; then
    hub_port="$VCT_HUB_PORT"
elif [[ -f "$state_dir/hub.port" ]]; then
    hub_port=$(tr -d '[:space:]' < "$state_dir/hub.port" 2>/dev/null || true)
fi
[[ -z "$hub_port" ]] && hub_port=7700

hub_token=""
if [[ -n "${VCT_HUB_TOKEN:-}" ]]; then
    hub_token="$VCT_HUB_TOKEN"
elif [[ -f "$state_dir/hub.token" ]]; then
    hub_token=$(tr -d '[:space:]' < "$state_dir/hub.token" 2>/dev/null || true)
fi

# ── Fail-open path: print "write", emit metric, log warning ─────────────
emit_metric() {
    local reason="$1"
    local cache_dir="$state_dir/cache"
    local jsonl="$cache_dir/dropped_writes.jsonl"
    mkdir -p "$cache_dir" 2>/dev/null || return 0
    local ts
    ts=$(date +%s 2>/dev/null) || ts=0
    printf '{"ts":%d,"project_id":"%s","collection":"%s","reason":"%s","fail_open":true}\n' \
        "$ts" "$project_id" "$collection" "$reason" \
        >> "$jsonl" 2>/dev/null || true
}

emit_warning() {
    local reason="$1"
    local now
    now=$(date +%s 2>/dev/null) || now=0
    local cache_dir="$state_dir/cache"
    local jsonl="$cache_dir/access_check_warn.jsonl"
    local key="$$:$reason"

    # Rate-limit: skip if same key fired in the past 5 min, unless VCO_HOOK_DEBUG=1.
    if [[ "${VCO_HOOK_DEBUG:-}" != "1" && -f "$jsonl" ]]; then
        local cutoff=$((now - 300))
        # awk-grep for a row with this key + ts >= cutoff
        local found
        found=$(awk -F'"key":"' -v cutoff="$cutoff" '
            /"ts":[0-9]+/ {
                # extract ts
                match($0, /"ts":[0-9]+/)
                ts = substr($0, RSTART+5, RLENGTH-5) + 0
                if (NF >= 2 && ts >= cutoff) {
                    # extract key (between first " and next ")
                    split($2, parts, "\"")
                    if (parts[1] == k) { print "found"; exit }
                }
            }
        ' k="$key" "$jsonl" 2>/dev/null) || true
        if [[ "$found" == "found" ]]; then
            return 0
        fi
    fi

    mkdir -p "$cache_dir" 2>/dev/null || true
    printf '{"ts":%d,"pid":%d,"key":"%s","reason":"%s"}\n' \
        "$now" "$$" "$key" "$reason" \
        >> "$jsonl" 2>/dev/null || true
    printf '[vct-access-check] WARNING: hub unreachable (%s); failing open to write level (rate-limited)\n' \
        "$reason" >&2
}

fail_open() {
    local reason="$1"
    emit_metric "$reason"
    emit_warning "$reason"
    printf 'write\n'
    exit 0
}

# ── Token required → fail-open if missing ──────────────────────────────
if [[ -z "$hub_token" ]]; then
    fail_open "no_hub_token"
fi

# ── HTTP call with 5s timeout ───────────────────────────────────────────
url="http://127.0.0.1:${hub_port}/api/v1/projects/${project_id}/access/${collection}"
response=""
http_status=""

# Write response body to a temp file so we can capture both body + status.
tmpfile=$(mktemp 2>/dev/null) || fail_open "mktemp_failed"
trap 'rm -f "$tmpfile"' EXIT

if ! http_status=$(curl --silent --show-error --max-time 5 \
        --output "$tmpfile" --write-out "%{http_code}" \
        --header "Authorization: Bearer $hub_token" \
        "$url" 2>/dev/null); then
    fail_open "curl_failed"
fi

if [[ "$http_status" -ge 500 ]]; then
    fail_open "hub_5xx_${http_status}"
elif [[ "$http_status" -eq 401 ]]; then
    fail_open "hub_auth_${http_status}"
elif [[ "$http_status" -eq 404 ]]; then
    # 404 = project_id not registered OR no access row for this collection.
    # Per the hub contract, the default (no row) IS "none" — but per the
    # fail-open spirit (we'd rather over-grant on degraded state than block
    # legitimate writes), 404 yields "write" with a metric emission so the
    # user can investigate WHY their project isn't registered.
    fail_open "hub_404_no_row"
elif [[ "$http_status" -ne 200 ]]; then
    fail_open "hub_unexpected_${http_status}"
fi

# Parse {"level": "..."} from response body.
response=$(cat "$tmpfile" 2>/dev/null || true)
level=""
if command -v jq >/dev/null 2>&1; then
    level=$(printf '%s' "$response" | jq -r '.level // empty' 2>/dev/null || true)
elif command -v python3 >/dev/null 2>&1; then
    level=$(printf '%s' "$response" | python3 -c \
        'import sys, json; d=json.load(sys.stdin); print(d.get("level",""))' 2>/dev/null || true)
fi

if [[ -z "$level" ]] || ! [[ "$level" =~ ^(read|write|none)$ ]]; then
    fail_open "hub_malformed_response"
fi

printf '%s\n' "$level"
exit 0
