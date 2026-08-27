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
#   5  forbidden (403 — a scoped-credential boundary refusal; NOT a
#      transient/unreachable condition, so callers must NOT env-fallback.
#      Post-flip the hub returns 403 when a resolver presents the coarse
#      global hub.token on a per-project /env|/config route while the
#      compat window is closed, or presents a per-project token minted
#      for a DIFFERENT project. Fix: present the scoped hub.token.<id>
#      (this resolver already prefers it) or set
#      VCT_HUB_LEGACY_GLOBAL_ENV=1 on the hub to reopen the compat window.)
#   64 usage error
#
# Hub discovery:
#   1. $VCT_HUB_PORT  / $VCT_HUB_TOKEN env vars (tests / dev launchers)
#   2. ${VCT_STATE_DIR:-$HOME/.vct}/hub.port  + hub.token (written by
#      the launcher on startup; mode 0o600 on Unix).
#   3. Port default: 7700 (matches launcher server.rs::DEFAULT_PORT).
#      Token has no default — missing file maps to exit 1.
#
#   v0.2.91 — STALE-ENV FALLBACK: the env token pin wins on the FIRST
#   attempt, always. On a PROVABLE refusal (401/403), when
#   $VCT_HUB_TOKEN is set AND differs from the on-disk token, the request
#   is retried ONCE with the on-disk token (the always-fresh 0600 SSOT
#   the hub regenerates on every start) and ONE definitive stderr line is
#   emitted. A failed retry changes nothing — the exit codes above are a
#   contract. Set VCT_HUB_TOKEN_STRICT=1 to disable the fallback
#   (harnesses that pin a bad token and assert the 401 path).
#
# Dependencies: curl (always), jq (preferred) OR python3 (fallback).
#
# Stderr policy: on hub unreachable / project missing / misconfigured /
# field missing, this script emits ONE rate-limited diagnostic line to
# stderr via _emit_warning() before exiting non-zero. Default suppression
# key is (pid, error_kind); callers may pass an explicit override (e.g.
# the schema-version drift warning keys on the hub-reported version so
# that ALL hooks across ALL PIDs share a single 5-min window). Max one
# emission per key per 5-min window; VCO_HOOK_DEBUG=1 bypasses the limit.
# State persisted at
#   ${VCT_STATE_DIR:-$HOME/.vct}/cache/resolver_warn.jsonl
# Hard usage errors / "neither jq nor python3" remain ALWAYS-emit via
# err() — those are programmer-fix-required, not env-fallback paths.

set -euo pipefail

# Resolver protocol version this client understands. MUST stay in
# lock-step with `RESOLVER_PROTOCOL_VERSION` in vco_lib/project_config.py
# and the ps1 sibling. When the hub reports a HIGHER value, we emit one
# best-effort stderr warning (forward-compat safety net) and continue —
# the hub's response shape is additive across versions.
readonly RESOLVER_PROTOCOL_VERSION=1

# VCO-REWIRE-BEGIN: orchestrator-root-resolution
# This file is byte-identical between `templates/scripts/` (shipped to
# user projects) and `.claude/scripts/` (orchestrator's own copy). The
# template-drift gate (`scripts/check_template_drift.py`) enforces that.
# No project-root resolution divergence — the hub owns the lookup, so
# both copies need the same logic.
# VCO-REWIRE-END: orchestrator-root-resolution

err() { printf '[vct-project-config] %s\n' "$*" >&2; }

# ── Rate-limited fall-through warning emission ─────────────────────────
#
# When the script falls through to its env-fallback path (hub
# unreachable, project not registered, field missing, hub
# misconfigured), the consumer wants ONE stderr line — not a flood when
# the same hook fires hundreds of times per session.
#
# Policy (mirrors vco_lib/resolver_warn.py):
#   - Key:    "<pid>:<error_kind>"  (per-PID; parallel hooks each get one).
#             Callers MAY override via the optional 3rd arg, in which
#             case the override becomes the suppression key verbatim
#             (used e.g. by schema-version drift, which keys on the
#             hub-reported version so ALL hooks across ALL PIDs share
#             a single 5-min window).
#   - Window: 5 minutes per key.
#   - Bypass: VCO_HOOK_DEBUG=1 → emit every occurrence.
#   - State:  $VCT_STATE_DIR/cache/resolver_warn.jsonl
#             (atomic append via flock on a sidecar lockfile).
#   - Rotation: when JSONL exceeds 1 MiB, truncate to most-recent 100 rows.
#
# Usage:  _emit_warning <error_kind> [<detail>] [<suppress_key_override>] [<stderr_line_override>]
#   The 4th arg, when non-empty, replaces the default
#   "[vct] project_config: ..." stderr line entirely. The JSONL row
#   shape is unchanged.
_RW_CACHE_DIR_INIT=0
_rw_cache_dir() {
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    printf '%s\n' "$state_dir/cache"
}

_rw_jsonl_path() { printf '%s/resolver_warn.jsonl\n' "$(_rw_cache_dir)"; }
_rw_lockfile_path() { printf '%s/resolver_warn.jsonl.lock\n' "$(_rw_cache_dir)"; }

_rw_ensure_dir() {
    if (( _RW_CACHE_DIR_INIT == 0 )); then
        mkdir -p "$(_rw_cache_dir)" 2>/dev/null || return 1
        _RW_CACHE_DIR_INIT=1
    fi
    return 0
}

# Return 0 if a matching row exists within the suppression window, 1 otherwise.
_rw_should_suppress() {
    local key="$1" now="$2" jsonl
    jsonl=$(_rw_jsonl_path)
    [[ -f "$jsonl" ]] || return 1
    # awk: find the most-recent ts for the matching "key":"...". Bail
    # if last_ts is non-empty AND within window.
    local last_ts
    last_ts=$(awk -v k="\"key\":\"$key\"" '
        index($0, k) { last = $0 }
        END {
            if (last == "") exit 0
            n = match(last, /"ts":[0-9]+/)
            if (n == 0) exit 0
            ts = substr(last, RSTART + 5, RLENGTH - 5)
            print ts
        }
    ' "$jsonl" 2>/dev/null) || return 1
    [[ -n "$last_ts" ]] || return 1
    local delta=$(( now - last_ts ))
    if (( delta >= 0 && delta < 300 )); then
        return 0  # suppress
    fi
    return 1
}

# Opportunistic rotation: when JSONL > 1MiB, keep only the last 100 lines.
_rw_maybe_rotate() {
    local jsonl="$1"
    [[ -f "$jsonl" ]] || return 0
    local size
    # stat -c on Linux, -f on BSD/macOS. Fall back gracefully.
    if size=$(stat -c '%s' "$jsonl" 2>/dev/null); then
        :
    elif size=$(stat -f '%z' "$jsonl" 2>/dev/null); then
        :
    else
        return 0
    fi
    if (( size > 1048576 )); then
        local tmp="$jsonl.rot.tmp"
        if tail -n 100 "$jsonl" > "$tmp" 2>/dev/null; then
            mv -f "$tmp" "$jsonl" 2>/dev/null || rm -f "$tmp" 2>/dev/null
        else
            rm -f "$tmp" 2>/dev/null || true
        fi
    fi
}

# JSON-escape a string for embedding in a JSON value. Handles backslash,
# double-quote, newline, tab, carriage return; drops other control bytes.
_rw_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/ }"
    s="${s//$'\r'/ }"
    s="${s//$'\t'/ }"
    printf '%s' "$s"
}

_emit_warning() {
    local error_kind="$1"
    local detail="${2:-}"
    local key_override="${3:-}"
    local stderr_override="${4:-}"
    local pid=$$
    local consumer="${BASH_SOURCE[0]##*/}"
    local key
    if [[ -n "$key_override" ]]; then
        key="$key_override"
    else
        key="${pid}:${error_kind}"
    fi
    local now
    now=$(date +%s 2>/dev/null) || now=0

    _rw_ensure_dir || true
    local jsonl lockfile
    jsonl=$(_rw_jsonl_path)
    lockfile=$(_rw_lockfile_path)

    # Rate-limit check (skipped when VCO_HOOK_DEBUG=1).
    if [[ "${VCO_HOOK_DEBUG:-}" != "1" ]]; then
        if _rw_should_suppress "$key" "$now"; then
            return 0
        fi
    fi

    # Stderr line: default fixed shape mirrors ps1 + python siblings.
    # Callers may pass a custom line (4th arg) to override — used by
    # schema-version drift so the legacy "[vct_project_config] WARNING: ..."
    # format stays stable across the rate-limit refactor.
    if [[ -n "$stderr_override" ]]; then
        printf '%s\n' "$stderr_override" >&2
    else
        printf '[vct] project_config: %s: %s. Falling back to env. (rate-limited; set VCO_HOOK_DEBUG=1 to see every occurrence)\n' \
            "$error_kind" "$detail" >&2
    fi

    # Cap detail at 200 bytes (parameter expansion is byte-oriented for
    # ASCII; for multibyte it may split a codepoint, acceptable for a
    # diagnostic JSONL row).
    local detail_clipped="${detail:0:200}"
    local user_name="${USER:-${USERNAME:-unknown}}"

    local row
    row=$(printf '{"ts":%d,"pid":%d,"consumer":"%s","consumer_pid":%d,"error_kind":"%s","key":"%s","detail":"%s","user":"%s"}' \
          "$now" "$pid" \
          "$(_rw_json_escape "$consumer")" \
          "$pid" \
          "$(_rw_json_escape "$error_kind")" \
          "$(_rw_json_escape "$key")" \
          "$(_rw_json_escape "$detail_clipped")" \
          "$(_rw_json_escape "$user_name")")

    # Atomic append via flock on a sidecar lockfile (so the awk scan
    # above doesn't race against the writer). flock missing → fall back
    # to plain append; POSIX O_APPEND is atomic for short writes.
    if command -v flock >/dev/null 2>&1; then
        (
            flock -x 200
            printf '%s\n' "$row" >> "$jsonl"
        ) 200>"$lockfile" 2>/dev/null || \
            printf '%s\n' "$row" >> "$jsonl" 2>/dev/null || true
    else
        printf '%s\n' "$row" >> "$jsonl" 2>/dev/null || true
    fi

    _rw_maybe_rotate "$jsonl"
    return 0
}

# ── Hub port discovery ──────────────────────────────────────────────────
# CORRUPT-INPUT CONTRACT (F-8) — MUST MATCH the ps1 sibling
# `vct_project_config.ps1::Get-HubPort` and the python sibling
# `vco_lib/project_config.py::_discover_hub` (port branch):
#   * env `VCT_HUB_PORT` or `hub.port` file that is non-numeric / garbage
#     → emit ONE rate-limited stderr warning (kind `hub_port_invalid`) and
#     fall through to the default port. NEVER print a garbage value (that
#     would build a malformed URL that curl then fails on, mis-classed as
#     `hub_unreachable`).
#   * `hub.port` present but UNREADABLE (perm-denied) → warn
#     (`hub_port_unreadable`) + default.
# A valid numeric port matches `^[0-9]+$`. This is the ONE conservative
# contract: invalid content → warn + default, never crash, never emit a
# partial/garbage resolution.
hub_port() {
    if [[ -n "${VCT_HUB_PORT:-}" ]]; then
        if [[ "$VCT_HUB_PORT" =~ ^[0-9]+$ ]]; then
            printf '%s\n' "$VCT_HUB_PORT"
            return 0
        fi
        _emit_warning "hub_port_invalid" \
            "VCT_HUB_PORT is not a positive integer; using default 7700"
        printf '7700\n'
        return 0
    fi
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    local port_file="$state_dir/hub.port"
    if [[ -f "$port_file" ]]; then
        local p
        # A read failure (perm-denied) leaves $p empty and `tr` writes to
        # stderr; suppress that and detect the failure via the readability
        # test so we can emit our own single warning instead.
        if [[ ! -r "$port_file" ]]; then
            _emit_warning "hub_port_unreadable" \
                "hub.port is not readable; using default 7700"
            printf '7700\n'
            return 0
        fi
        p=$(tr -d '[:space:]' < "$port_file" 2>/dev/null)
        if [[ "$p" =~ ^[0-9]+$ ]]; then
            printf '%s\n' "$p"
            return 0
        fi
        if [[ -n "$p" ]]; then
            _emit_warning "hub_port_invalid" \
                "hub.port contains non-integer content; using default 7700"
        fi
        # empty (whitespace-only / truncated write) → silent default.
        printf '7700\n'
        return 0
    fi
    # Default — matches launcher's server.rs::DEFAULT_PORT.
    printf '7700\n'
}

# ── Hub auth-token discovery ────────────────────────────────────────────
# CORRUPT-INPUT CONTRACT (F-8) — MUST MATCH the ps1 sibling
# `vct_project_config.ps1::Get-HubToken` and the python sibling
# `vco_lib/project_config.py::_discover_hub` (token branch):
#   * env `VCT_HUB_TOKEN` set → used verbatim (any non-empty string is a
#     legitimate token; no format to validate).
#   * `hub.token` present but UNREADABLE (perm-denied) → emit ONE
#     rate-limited stderr warning (kind `hub_token_unreadable`) and return
#     empty. The token has NO sane default, so an unreadable/absent token
#     is treated as "no token" → the caller degrades to `hub_unreachable`
#     (exit 2 in hub_get). NEVER crash on the read failure.
#
# v0.2.76 Part 4 — PER-PROJECT TOKEN PREFERENCE (MUST MATCH the ps1
# sibling `Get-HubToken` and `vco_lib/project_config.py::_project_token`):
# hub_token accepts an OPTIONAL project id. When given AND a readable
# `hub.token.<project_id>` file exists, that scoped token is returned in
# preference to the global `hub.token`. The env `VCT_HUB_TOKEN` still wins
# over both (tests / dev harnesses pin it). Falls back cleanly to the
# global token when no per-project file is present (compat window). The
# `by-path` lookup passes NO project id (it is not a per-project route, so
# it must use the global token); only the `/config` + `/env` calls pass
# the resolved id.
hub_token() {
    local project_id="${1:-}"
    if [[ -n "${VCT_HUB_TOKEN:-}" ]]; then
        printf '%s' "$VCT_HUB_TOKEN"
        return 0
    fi
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    # Prefer the per-project token file when a project id is known and the
    # scoped file exists + is readable. An unreadable per-project file
    # falls through to the global token (compat), not a hard failure.
    if [[ -n "$project_id" ]]; then
        local proj_file="$state_dir/hub.token.$project_id"
        if [[ -f "$proj_file" && -r "$proj_file" ]]; then
            tr -d '[:space:]' < "$proj_file" 2>/dev/null
            return 0
        fi
    fi
    local token_file="$state_dir/hub.token"
    if [[ -f "$token_file" ]]; then
        if [[ ! -r "$token_file" ]]; then
            _emit_warning "hub_token_unreadable" \
                "hub.token is not readable; treating as no token"
            printf ''
            return 0
        fi
        tr -d '[:space:]' < "$token_file" 2>/dev/null
        return 0
    fi
    printf ''
}

# ── Stale-env hub-token fallback (v0.2.91, WP-D item 4) ─────────────────
#
# MUST MATCH the SSOT `vco_lib/project_config.py::_stale_env_token_fallback`
# and the mirrors in `vct_secrets_resolve.sh`, both `.ps1` siblings,
# `claude_mcp_servers/wrappers/_base.py`,
# `launcher/tools/vct-cli/src/main.rs` and `tools/vct-secrets/vct`.
# Locked by tests/test_stale_env_token_parity_v0291.py (the F-8
# quadruplet + the Python SSOT driven through the same fixtures).
#
# WHY: `$VCT_HUB_TOKEN` wins over the file on every FIRST attempt (that is
# the tests/dev pin contract). But the hub regenerates `hub.token` on each
# start, so a shell that exported the token before an update presents a
# value the hub refuses — every resolve then 401s until that shell dies.
# After a PROVABLE refusal (401/403) we retry ONCE with the on-disk token
# (the always-fresh 0600 SSOT) and emit one definitive line. Nothing
# loops, respawns, or restarts; a failed retry keeps today's error path
# (and therefore every exit code in the header contract).

# The ONE definitive line. Byte-identical across every mirror.
_VCT_STALE_ENV_TOKEN_MESSAGE="stale VCT_HUB_TOKEN in env overridden by on-disk hub.token — run \`unset VCT_HUB_TOKEN\` or open a new shell"
_VCT_STALE_ENV_WARNED=0

_warn_stale_env_token() {
    # Once per process; routed through the rate-limited emitter (this
    # script's stderr policy) with an explicit line override — the
    # default "Falling back to env." shape would be a lie here, since
    # the resolution SUCCEEDED with the on-disk token.
    if (( _VCT_STALE_ENV_WARNED == 0 )); then
        _VCT_STALE_ENV_WARNED=1
        _emit_warning "hub_token_stale_env" "$_VCT_STALE_ENV_TOKEN_MESSAGE" "" \
            "[vct] project_config: hub_token_stale_env: $_VCT_STALE_ENV_TOKEN_MESSAGE"
    fi
}

hub_token_on_disk() {
    # $1 = optional project id. Prints the ON-DISK token, IGNORING
    # $VCT_HUB_TOKEN, preserving this call-site's scoped-vs-global
    # semantics (scoped `hub.token.<id>` preferred when an id is given).
    # Prints empty when nothing readable exists.
    local project_id="${1:-}"
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    if [[ -n "$project_id" ]]; then
        local proj_file="$state_dir/hub.token.$project_id"
        if [[ -f "$proj_file" && -r "$proj_file" ]]; then
            local scoped
            scoped=$(tr -d '[:space:]' < "$proj_file" 2>/dev/null) || scoped=""
            if [[ -n "$scoped" ]]; then
                printf '%s' "$scoped"
                return 0
            fi
        fi
    fi
    local token_file="$state_dir/hub.token"
    if [[ -f "$token_file" && -r "$token_file" ]]; then
        tr -d '[:space:]' < "$token_file" 2>/dev/null || printf ''
        return 0
    fi
    printf ''
}

hub_stale_env_fallback_token() {
    # $1 = optional project id. Prints the on-disk token to retry with
    # and returns 0, or returns 1 (no output) to leave today's path
    # alone. Rules, in order (identical in every mirror):
    #   1. VCT_HUB_TOKEN_STRICT=1        → 1 (the pin is authoritative)
    #   2. VCT_HUB_TOKEN unset/empty     → 1 (nothing was pinned)
    #   3. no readable on-disk token     → 1 (nothing better to try)
    #   4. on-disk == env (whitespace-normalised) → 1 (pin is not stale)
    local project_id="${1:-}"
    # Trimmed comparison to the literal 1 — the SSOT's spelling, so a
    # `VCT_HUB_TOKEN_STRICT=1` written with a trailing newline/CR means the
    # same thing in bash, PowerShell, Rust and Python.
    [[ "$(printf '%s' "${VCT_HUB_TOKEN_STRICT:-}" | tr -d '[:space:]')" == "1" ]] \
        && return 1
    local env_tok="${VCT_HUB_TOKEN:-}"
    env_tok=$(printf '%s' "$env_tok" | tr -d '[:space:]')
    [[ -n "$env_tok" ]] || return 1
    local disk_tok
    disk_tok=$(hub_token_on_disk "$project_id")
    [[ -n "$disk_tok" ]] || return 1
    [[ "$disk_tok" == "$env_tok" ]] && return 1
    printf '%s' "$disk_tok"
    return 0
}

# ── HTTP helper ─────────────────────────────────────────────────────────
# `--header @-` reads the Authorization header from stdin so the token
# never appears in argv (`ps`/`/proc/<pid>/cmdline`). Returns:
#   exit 0 + stdout "<status>\t<body>"  on a completed request
#   exit 1                              on curl failure (conn refused, etc.)
#   exit 2                              on missing token (no hub.token)
_hub_curl() {
    # $1 = full url, $2 = bearer token.
    # echoes "<status>\t<body>"; returns 1 when curl itself fails.
    local url="$1" token="$2"
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

hub_get() {
    local path="$1"
    # v0.2.76 Part 4: optional 2nd arg = the project id for a per-project
    # route (`/config`, `/env`). When set, hub_token prefers the scoped
    # `hub.token.<id>`. The `by-path` lookup passes no id → global token.
    local project_id="${2:-}"
    local port token
    port=$(hub_port)
    token=$(hub_token "$project_id")
    if [[ -z "$token" ]]; then
        return 2
    fi
    local url="http://127.0.0.1:${port}/api/v1/${path}"
    local result status
    result=$(_hub_curl "$url" "$token") || return 1
    status="${result%%$'\t'*}"
    # v0.2.91 (WP-D item 4): a PROVABLE credential refusal is the ONLY
    # trigger for the one-shot on-disk-token retry. Every other status —
    # and a strict pin, an absent env token, an identical on-disk token,
    # or a retry that is ALSO refused — falls through to the original
    # result, so the header's exit-code contract is byte-identical.
    case "$status" in
        401|403)
            local fallback retry retry_status
            if fallback=$(hub_stale_env_fallback_token "$project_id"); then
                if retry=$(_hub_curl "$url" "$fallback"); then
                    retry_status="${retry%%$'\t'*}"
                    # ADOPT only an answer that PROVES the fallback
                    # credential was accepted: 2xx, or 404 (the hub
                    # answers "no row" only AFTER its auth middleware
                    # accepted the bearer — a post-auth answer like a
                    # 200). Anything else — 5xx included — proves
                    # nothing, so the ORIGINAL 401/403 stands and no
                    # definitive line is printed (v0.2.91 wave-3,
                    # MINOR-1).
                    case "$retry_status" in
                        2??|404)
                            _warn_stale_env_token
                            printf '%s\n' "$retry"
                            return 0
                            ;;
                        *) : ;;   # not proof — keep the original refusal
                    esac
                fi
            fi
            ;;
    esac
    printf '%s\n' "$result"
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

# ── schema_version forward-compat check ───────────────────────────────
# Reads `schema_version` from a full-config JSON body. If the hub
# reports a value HIGHER than RESOLVER_PROTOCOL_VERSION, emit a single
# stderr warning and continue (the protocol is additive — newer hubs
# may include fields this client doesn't recognise, but the existing
# fields still parse). Missing `schema_version` is treated as version 1
# (pre-v0.2.22 hub) — no warning.
#
# v0.2.24-A4: the warning now routes through `_emit_warning` with a
# stable cross-PID suppression key (`schema_version_drift_<hub_version>`)
# so 100+ hook invocations in one Claude Code session don't spam stderr
# 100+ times. Window is the standard 5 minutes; VCO_HOOK_DEBUG=1
# bypasses.
_maybe_warn_schema_version() {
    local body="$1"
    local raw
    raw=$(json_extract "$body" '.schema_version' 2>/dev/null) || raw=""
    # Strip non-digits defensively (json_extract emits the bare value
    # for ints via jq -r; python3 fallback wraps non-strings via
    # json.dumps which preserves the integer form).
    if [[ -z "$raw" ]]; then
        return 0
    fi
    if [[ ! "$raw" =~ ^[0-9]+$ ]]; then
        # Malformed schema_version — don't crash, don't warn (the
        # python sibling would raise, but the bash client treats this
        # as defensive degradation).
        return 0
    fi
    local hub_version=$((10#$raw))
    if (( hub_version > RESOLVER_PROTOCOL_VERSION )); then
        local line
        line=$(printf '[vct_project_config] WARNING: hub schema_version=%d > client RESOLVER_PROTOCOL_VERSION=%d; some fields may be unknown. Update the orchestrator clone or downgrade the hub.' \
            "$hub_version" "$RESOLVER_PROTOCOL_VERSION")
        # Suppression key keyed on the OBSERVED hub version (not on PID)
        # so every hook invocation against the same drifted hub shares
        # one 5-min window. The error_kind/detail still feed into the
        # JSONL telemetry row for diagnostics.
        _emit_warning \
            "schema_version_drift" \
            "hub_version=$hub_version client_version=$RESOLVER_PROTOCOL_VERSION" \
            "schema_version_drift_${hub_version}" \
            "$line" || true
    fi
    return 0
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
        _emit_warning "hub_unreachable" "hub.token missing; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
        return 1
    fi
    if [[ $rc -ne 0 ]]; then
        _emit_warning "hub_unreachable" "hub unreachable; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
        return 1
    fi
    status="${result%%$'\t'*}"
    body="${result#*$'\t'}"
    case "$status" in
        200)
            local id
            id=$(json_extract "$body" '.id')
            if [[ -z "$id" ]]; then
                _emit_warning "hub_unreachable" "by-path 200 but no .id field; body=$body"
                return 2
            fi
            printf '%s' "$id"
            ;;
        401)
            _emit_warning "hub_unauthorized" "401 unauthorized on by-path; launcher may have restarted (token rotated). If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
            return 1
            ;;
        403)
            # by-path is NOT a per-project-token route (the flip only gates
            # /env + /config), so a 403 here is anomalous. Still label it as
            # a hard forbidden refusal rather than mislabeling it
            # hub_unreachable — the latter would trigger a spurious
            # env-fallback and hide the real cause.
            _emit_warning "forbidden" "403 forbidden on by-path lookup for: $arg; body=$body"
            return 5
            ;;
        404)
            _emit_warning "project_not_registered" "no project registered at path: $arg"
            return 2
            ;;
        400)
            _emit_warning "project_not_registered" "hub rejected path query: $body"
            return 2
            ;;
        *)
            _emit_warning "hub_unreachable" "hub returned status $status for by-path lookup; body=$body"
            return 1
            ;;
    esac
}

# ── v0.2.47 (extras): code_graph_extra_paths renderer ───────────────────
# Hub wire shape (single-field envelope):
#   {"code_graph_extra_paths": [{"path": "...", "enabled": true,
#                                 "last_indexed_commit": "..."|null}, ...]}
# We render only the `path` of `enabled=true` rows, newline-delimited.
# Empty list → empty stdout + exit 0 (zero extras configured is valid).
# Pre-v0.2.47 hubs return 404/field_not_found — handled upstream as exit 4.
_render_extra_paths() {
    local body="$1" field="$2"
    if command -v jq >/dev/null 2>&1; then
        local out
        # `.field // []` defaults absent → empty array. `.[] | select(.enabled == true)`
        # filters; `.path // empty` skips rows where path is missing (defensive).
        # Trailing newline added by jq -r `.path` so multiple paths print one
        # per line. Single trailing newline on empty output is fine — callers
        # use `tr -d '[:space:]'` or `read -r` when they need a clean value.
        out=$(printf '%s' "$body" | jq -r --arg f "$field" \
            '(.[$f] // []) | map(select(.enabled == true)) | .[] | .path // empty' 2>/dev/null) || {
            _emit_warning "field_decode_failed" "jq failed to decode code_graph_extra_paths; body=$body"
            return 4
        }
        # Emit verbatim. Empty `out` → exit 0 with no stdout (intentional).
        if [[ -n "$out" ]]; then
            printf '%s\n' "$out"
        fi
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        # Pipe body to python3 via stdin; pass field name through env so
        # the script literal stays heredoc-safe (no `$field` interpolation).
        printf '%s' "$body" | VCT_RENDER_FIELD="$field" python3 -c '
import json, os, sys
field = os.environ.get("VCT_RENDER_FIELD", "")
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(4)
items = data.get(field) if isinstance(data, dict) else None
if not isinstance(items, list):
    # Field absent / wrong shape — treat as no extras (exit 0, blank).
    sys.exit(0)
for row in items:
    if not isinstance(row, dict):
        continue
    if row.get("enabled") is True:
        p = row.get("path")
        if isinstance(p, str) and p:
            sys.stdout.write(p + "\n")
sys.exit(0)
'
        local rc=$?
        return $rc
    fi
    err "neither jq nor python3 found on PATH; cannot render code_graph_extra_paths"
    return 1
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
    # Per-project route → pass the resolved project id so hub_token
    # prefers the scoped `hub.token.<id>` (v0.2.76 Part 4).
    result=$(hub_get "$path" "$pid")
    rc=$?
    set -e
    if [[ $rc -eq 2 ]]; then
        _emit_warning "hub_unreachable" "hub.token missing; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
        return 1
    fi
    if [[ $rc -ne 0 ]]; then
        _emit_warning "hub_unreachable" "hub unreachable; is the launcher running? If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
        return 1
    fi

    status="${result%%$'\t'*}"
    body="${result#*$'\t'}"

    case "$status" in
        200)
            # Forward-compat check: warn (once, best-effort) if the
            # hub reports a higher schema_version than we understand.
            # Single-field envelopes omit `schema_version`; the helper
            # treats that as "no warning" (defensive degradation).
            _maybe_warn_schema_version "$body" || true
            if [[ -n "$field" ]]; then
                # v0.2.47 (extras): the `code_graph_extra_paths` field has
                # a structured shape on the wire (array of {path, enabled,
                # last_indexed_commit?}) but consumers — the hook chain in
                # code-graph-incremental.sh — want plain newline-delimited
                # paths of ENABLED rows only. Render it here so every
                # bash caller gets the consumer-friendly format without
                # re-parsing JSON. Empty array → empty output + exit 0
                # (the spec: "no extras configured" is a valid state, not
                # an error). Pre-v0.2.47 hubs that don't ship the field
                # land in the existing "field_decode_failed" path with
                # exit 4, which the hook treats as "no extras" — same
                # observable behaviour as an explicit empty array.
                if [[ "$field" == "code_graph_extra_paths" ]]; then
                    _render_extra_paths "$body" "$field"
                    return $?
                fi
                # Single-field envelope: {"<field>": <value>}. Unwrap.
                local val
                val=$(json_extract "$body" ".\"$field\"")
                if [[ -z "$val" ]]; then
                    _emit_warning "field_decode_failed" "field $field decoded empty from hub response"
                    return 4
                fi
                printf '%s' "$val"
            else
                printf '%s' "$body"
            fi
            ;;
        401)
            _emit_warning "hub_unauthorized" "401 unauthorized; launcher may have restarted (token rotated). If VCO was just updated, restart the launcher and reload the editor window (a pre-update session may hold a stale VCT_HUB_TOKEN)."
            return 1
            ;;
        403)
            # Scoped-credential boundary refusal (v0.2.77 flip). The hub
            # ACCEPTED our request and knows the project, but refused this
            # bearer: either the coarse global hub.token on a per-project
            # route with the compat window closed, or a per-project token
            # minted for a DIFFERENT project. This is a HARD refusal, NOT a
            # transient/unreachable condition — callers must NOT env-fall
            # back (that would mask the misconfiguration). The scoped
            # hub.token.<id> is already preferred by hub_token; a 403
            # therefore means that file was absent/unreadable so we rode the
            # global token, OR the wrong project's token was presented.
            _emit_warning "forbidden" "403 forbidden for project $pid: the global hub.token is refused on /config (per-project token required) or a token for another project was presented. Present the scoped hub.token.$pid, or set VCT_HUB_LEGACY_GLOBAL_ENV=1 on the hub to reopen the one-release compat window. body=$body"
            return 5
            ;;
        404)
            local code
            code=$(json_extract "$body" '.error.code')
            case "$code" in
                project_not_found)
                    _emit_warning "project_not_registered" "project $pid not registered in launcher.db"
                    return 2
                    ;;
                field_not_found)
                    _emit_warning "field_not_found" "field $field not in config for project $pid"
                    return 4
                    ;;
                *)
                    _emit_warning "field_not_found" "hub 404 with unknown code $code; body=$body"
                    return 4
                    ;;
            esac
            ;;
        503)
            _emit_warning "service_misconfigured" "503 for project $pid (primary KG binding missing — fix in launcher GUI)"
            return 3
            ;;
        400)
            _emit_warning "field_not_found" "hub rejected request: $body"
            return 4
            ;;
        *)
            _emit_warning "hub_unreachable" "hub returned status $status; body=$body"
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
