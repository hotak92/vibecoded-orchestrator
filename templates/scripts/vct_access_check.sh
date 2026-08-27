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
# ── Rate-limit scope (cross-client documentation) ──────────────────────
#
# This script keys its rate-limit on "$$:$reason" (PID-scoped). Every
# hook invocation is a fresh bash process, so PID-scoped means EACH
# hook firing emits at least one WARNING per failure reason. This is
# INTENTIONAL — for ephemeral hook callers we want the user to SEE the
# degraded state every time it occurs, not have it silently suppressed
# by a long-lived rate-limit window. vct_access_check.ps1 mirrors this
# PID-scoped behaviour exactly.
#
# Contrast: vco_lib/access_resolver.py (consumed by the long-running
# MCP server claude_mcp_servers/weaviate_mcp/server.py) uses a
# process-scoped rate-limit (just "$reason", no PID prefix) so a single
# MCP process doesn't spam WARNINGs every 5 minutes for the same
# persistent failure.
#
# The divergence is by design: hook invocations are episodic and
# user-visible; MCP processes are long-lived and emit through Python
# logging. If you find yourself thinking "should I align these?" the
# answer is no — read this paragraph again.
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

# ── Stale-env hub-token fallback (v0.2.91, WP-D item 4) ─────────────────
#
# MUST MATCH the SSOT `vco_lib/project_config.py::_stale_env_token_fallback`
# and the mirrors in `vct_secrets_resolve.{sh,ps1}`,
# `vct_project_config.{sh,ps1}`, `claude_mcp_servers/wrappers/_base.py`,
# `launcher/tools/vct-cli/src/main.rs` and `tools/vct-secrets/vct`.
# Locked by tests/test_stale_env_token_parity_v0291.py.
#
# WHY here: `$VCT_HUB_TOKEN` wins over the file, and the hub regenerates
# `hub.token` on every start — so a shell that exported the token before
# an update presents a dead credential, the hub 401s, and this client
# FAILS OPEN to "write" on every single call. The gate then silently
# degrades to permissive for the whole life of that shell while the
# on-disk token sitting next to it would have answered correctly.
# After a PROVABLE refusal (401/403) we retry ONCE with the on-disk token
# and print one definitive line.
#
# THE FAIL-OPEN CONTRACT IS UNCHANGED (deliberate availability choice):
# a genuine auth failure, an unreachable hub, a missing token, or a retry
# that is ALSO refused still fails open to "write" with the SAME reason
# string, the SAME rate-limited warning and the SAME dropped-write metric
# row. This fix only makes the fail-open reached LESS OFTEN.
#
# GLOBAL TOKEN ONLY: `/projects/{id}/access/{collection}` is NOT a
# per-project-token route (the hub gates only `/env` + `/config` that
# way — see `auth.rs::per_project_token_route`), so presenting a scoped
# `hub.token.<id>` here would itself 401. Unlike the resolver quadruplet,
# this mirror deliberately has no scoped branch.

# The ONE definitive line. Byte-identical across every mirror.
_VCT_STALE_ENV_TOKEN_MESSAGE="stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — run \`unset VCT_HUB_TOKEN\` or open a new shell"

warn_stale_env_token() {
    # NOT a fail-open event: no dropped-write metric row, no
    # `hub unreachable` phrasing. The gate WORKED — we just had to reach
    # past a dead env pin to make it work. One line, always emitted (this
    # script is one-shot and its stderr policy is deliberately per-PID).
    printf '[vct-access-check] WARNING: %s\n' "$_VCT_STALE_ENV_TOKEN_MESSAGE" >&2
}

hub_token_on_disk() {
    # The ON-DISK global token, IGNORING $VCT_HUB_TOKEN. Empty when
    # nothing readable exists.
    local token_file="$state_dir/hub.token"
    if [[ -f "$token_file" && -r "$token_file" ]]; then
        tr -d '[:space:]' < "$token_file" 2>/dev/null || printf ''
        return 0
    fi
    printf ''
}

stale_env_fallback_token() {
    # Prints the on-disk token to retry with and returns 0, or returns 1
    # (no output). Rules, in order (identical in every mirror):
    #   1. VCT_HUB_TOKEN_STRICT=1        → 1 (the pin is authoritative)
    #   2. VCT_HUB_TOKEN unset/empty     → 1 (nothing was pinned)
    #   3. no readable on-disk token     → 1 (nothing better to try)
    #   4. on-disk == env (whitespace-normalised) → 1 (pin is not stale)
    # Trimmed comparison to the literal 1 — the SSOT's spelling, so a
    # `VCT_HUB_TOKEN_STRICT=1` written with a trailing newline/CR means the
    # same thing in bash, PowerShell, Rust and Python.
    [[ "$(printf '%s' "${VCT_HUB_TOKEN_STRICT:-}" | tr -d '[:space:]')" == "1" ]] \
        && return 1
    local env_tok
    env_tok=$(printf '%s' "${VCT_HUB_TOKEN:-}" | tr -d '[:space:]')
    [[ -n "$env_tok" ]] || return 1
    local disk_tok
    disk_tok=$(hub_token_on_disk)
    [[ -n "$disk_tok" ]] || return 1
    [[ "$disk_tok" == "$env_tok" ]] && return 1
    printf '%s' "$disk_tok"
    return 0
}

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

do_request() {
    # $1 = bearer token. Echoes the HTTP status; body lands in $tmpfile.
    # Returns non-zero only when curl itself fails (conn refused, DNS,
    # timeout) — the caller maps that to the fail-open path.
    local token="$1"
    curl --silent --show-error --max-time 5 \
        --output "$tmpfile" --write-out "%{http_code}" \
        --header "Authorization: Bearer $token" \
        "$url" 2>/dev/null
}

if ! http_status=$(do_request "$hub_token"); then
    fail_open "curl_failed"
fi

# v0.2.91 (WP-D item 4): a PROVABLE credential refusal is the ONLY
# trigger for the one-shot on-disk-token retry. A strict pin, an absent
# env token, an identical on-disk token, a retry that is ALSO refused, a
# retry whose curl fails, or a retry whose answer does not PROVE the
# fallback credential was accepted all fall through with the ORIGINAL
# status — so every fail-open reason string, warning and metric row below
# is byte-identical to pre-v0.2.91.
#
# ADOPT only 2xx or 404: the hub answers 404 ("project not registered / no
# access row") strictly AFTER its auth middleware accepted the bearer, so
# it is a post-auth answer like a 200. A 5xx is NOT — before the wave-3
# MINOR-1 fix it was adopted, turning a truthful `hub_auth_401` metric row
# into `hub_5xx_503` and printing the definitive line on no evidence.
case "$http_status" in
    401|403)
        if fallback_token=$(stale_env_fallback_token); then
            if retry_status=$(do_request "$fallback_token"); then
                case "$retry_status" in
                    2??|404)
                        warn_stale_env_token
                        http_status="$retry_status"
                        ;;
                    *) : ;;   # not proof — keep the original refusal
                esac
            else
                # The retry's curl failed; re-run nothing. The original
                # 401/403 body was overwritten in $tmpfile, but the 401
                # arm below never reads the body — it fails open on the
                # status alone.
                :
            fi
        fi
        ;;
esac

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
#
# v0.2.49 Step F MF8 (L4-S3): split the "malformed" reason into two
# distinct values to match the py/ps1 siblings + enable cross-client
# metric aggregation. Pre-fix bash emitted a single `hub_malformed_response`
# value that conflated JSON-parse failures and unrecognized-level
# values. The siblings (vco_lib/access_resolver.py + vct_access_check.ps1)
# emit `hub_malformed_json` vs `hub_malformed_level` separately, so
# aggregating `dropped_writes.jsonl` across clients saw 3 keys for
# 2 failure modes.
response=$(cat "$tmpfile" 2>/dev/null || true)
level=""
parser_used=""
if command -v jq >/dev/null 2>&1; then
    level=$(printf '%s' "$response" | jq -r '.level // empty' 2>/dev/null || true)
    parser_used="jq"
elif command -v python3 >/dev/null 2>&1; then
    level=$(printf '%s' "$response" | python3 -c \
        'import sys, json; d=json.load(sys.stdin); print(d.get("level",""))' 2>/dev/null || true)
    parser_used="python3"
fi

# If parser emitted empty AND there was a non-empty response body,
# parse failed → hub_malformed_json. If parser emitted a value that
# isn't in {read, write, none} → hub_malformed_level.
if [[ -z "$level" ]]; then
    # Non-empty body but parser produced nothing → JSON parse failed.
    if [[ -n "$response" ]]; then
        fail_open "hub_malformed_json"
    else
        fail_open "hub_malformed_json"
    fi
fi
if ! [[ "$level" =~ ^(read|write|none)$ ]]; then
    fail_open "hub_malformed_level"
fi

printf '%s\n' "$level"
exit 0
