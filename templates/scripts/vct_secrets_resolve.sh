#!/usr/bin/env bash
# vct_secrets_resolve.sh — agent-facing secret resolver.
#
# ONE RESOLUTION CHAIN (v0.2.73 unification — MUST MATCH the other two
# implementations: `templates/scripts/vct_secrets_resolve.ps1` and
# `vco_lib/agent_secrets.py::get`; keep tier order, fall-through rules,
# and the tier-3 parsing rule identical across all three):
#
#   Tier 1  vct-hub  GET /api/v1/projects/{id}/env?key=NAME
#           (OS-keychain values, per-(secret × requester) active-flag
#           gated — the launcher's permission matrix).
#   Tier 2  file store  $VCT_SECRETS_DIR (default ~/.vct-secrets):
#           projects/<NAME>/<key>  →  shared/<key>.
#   Tier 3  the project's own `.env` — READ-ONLY, lowest priority.
#           Only consulted when the first arg is a FOLDER (a bare
#           project id gives no folder to read; tier 3 is skipped and
#           the miss diagnostic says so).
#
# Fall-through: tier 1 → 2 on hub unreachable / project not registered /
# 401 / key_not_active (the hub cannot distinguish "paused" from "never
# declared", and the file store is an independent store); tier 2 → 3 on
# file absent/unreadable. All-miss → non-zero exit preserving the tier-1
# exit-code contract below (exit 3 `key_not_active` is only returned
# after tiers 2 and 3 also missed). Errors name the KEY and the tiers
# consulted — NEVER the value.
#
# Tier-3 parsing rule (identical ×3): line-oriented; accept `KEY=VALUE`
# and `export KEY=VALUE`; strip one matching pair of single/double
# quotes; NO variable expansion, NO command substitution; first match
# wins. The value is never logged, cached to disk, or re-exported into
# any VCO-written file. The pause model does not apply to tier 3 — the
# user pauses by editing their own file.
#
# Usage:
#   vct_secrets_resolve.sh <project_id_or_folder> <secret_key>
#       Print the value of <secret_key> for the resolved project.
#       Exit codes: 0=ok, 1=hub unreachable, 2=project not registered,
#                   3=key not active for this project, 4=key not found
#                   in the hub's response (treated like 3 for callers).
#                   Non-zero codes mean the key ALSO missed the file
#                   store and the project `.env`.
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
# Auth (H5, 2026-05-08):
#   Every request carries `Authorization: Bearer <token>` where
#   <token> is read from:
#     1. $VCT_HUB_TOKEN env var (tests / dev harnesses)
#     2. ${VCT_STATE_DIR:-$HOME/.vct}/hub.token (written by the
#        launcher on startup, mode 0o600 on Unix). Mirrors
#        `launcher/src-tauri/src/hub/auth.rs::write_token_file`.
#   If the token file is missing → exit 1 ("hub unreachable" — the
#   launcher hasn't started yet, or it crashed before persisting the
#   token). The hub also returns 401 if the token is wrong (e.g. file
#   stale because the launcher restarted between resolves); we map 401
#   to exit 1 too, so callers see one consistent "talk to the launcher"
#   diagnostic.
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

# ── Hub auth-token discovery ────────────────────────────────────────────
#
# Returns the token on stdout, or empty string if no token file exists.
# Caller treats empty as "hub not running" (callers can't authenticate
# without it; the hub will return 401).
hub_token() {
    if [[ -n "${VCT_HUB_TOKEN:-}" ]]; then
        printf '%s' "$VCT_HUB_TOKEN"
        return 0
    fi
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    local token_file="$state_dir/hub.token"
    if [[ -f "$token_file" ]]; then
        # Strip ALL whitespace including stray trailing newline.
        # Same pattern as hub_port — robust against editor-saved files.
        tr -d '[:space:]' < "$token_file"
        return 0
    fi
    # No token file → emit empty. Caller maps that to exit 1.
    printf ''
}

# ── HTTP helpers ────────────────────────────────────────────────────────
#
# We use curl directly so we can capture the HTTP status separately from
# the body. `--fail` would conflate 404 (project not found) and connection
# refused (hub unreachable) into the same exit code, which is exactly the
# distinction the contract needs to preserve.
#
# Auth: every call carries `Authorization: Bearer <token>`. The token
# is passed via `--header @-` so it's NEVER visible in `ps`/process
# listings (curl reads the header from stdin via the `@-` form).
# Without this, `--header "Authorization: Bearer abc..."` would put the
# secret on argv where any process on the box could read it via
# /proc/<pid>/cmdline.
hub_get() {
    # $1 = path-with-query (no leading slash)
    # echoes "<status>\t<body>" on stdout; exit non-zero only when curl
    # itself fails (e.g. connection refused, not 4xx). Returns exit 2
    # specifically when no token file exists (so caller can map it
    # to "hub unreachable" without trying the request).
    local path="$1"
    local port token
    port=$(hub_port)
    token=$(hub_token)
    if [[ -z "$token" ]]; then
        return 2
    fi
    local url="http://127.0.0.1:${port}/api/v1/${path}"
    local body status
    # `--header @-` reads the header line from stdin. We feed exactly
    # one line: "Authorization: Bearer <token>". This keeps the token
    # off argv. The `<<<` here-string gives us a single-line stdin
    # without an extra subshell.
    if ! body=$(curl --silent --show-error --max-time 5 \
                     --header @- \
                     --output - --write-out '\n%{http_code}' "$url" \
                     <<<"Authorization: Bearer ${token}" 2>&1); then
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
    # Cross-OS note (must match vct_secrets_resolve.ps1::Test-LooksLikePath):
    # this `case`/glob form has no operator-precedence pitfall, so the v0.2.73
    # PowerShell fix (parenthesising the `-and` drive-letter clause) has no
    # analog here — the two stay behaviourally identical.
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
            # Stale token (launcher restarted between resolves) or the
            # token file we read doesn't match what the hub generated.
            # Treat as "hub unreachable" so callers see one consistent
            # diagnostic. The user fix is the same: restart resolver
            # / re-source env.
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

# ── Tier 1: hub (keychain) ──────────────────────────────────────────────
read_key_hub() {
    # $1 = project_id_or_folder, $2 = key. Prints the value on success.
    # Return codes preserve the historical contract (1=hub unreachable,
    # 2=project not registered, 3=key_not_active, 4=key missing from a
    # 200 body) — the chain caller remembers this code and reuses it as
    # the final exit when tiers 2 and 3 also miss.
    local pid_arg="$1" key="$2"
    local pid rc
    set +e
    pid=$(resolve_project_id "$pid_arg")
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
        # resolve_project_id already printed the diagnostic (1 or 2).
        return $rc
    fi
    local result status body
    set +e
    result=$(hub_get "projects/$(url_encode "$pid")/env?key=$(url_encode "$key")")
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
            local val
            val=$(json_extract "$body" ".\"$key\"")
            if [[ -z "$val" ]]; then
                err "hub returned 200 but no $key field; body=$body"
                return 4
            fi
            printf '%s' "$val"
            ;;
        401)
            # See note in resolve_project_id: stale token → exit 1 to
            # match the "hub unreachable" semantics callers already
            # know how to surface.
            err "hub returned 401 unauthorized; the launcher may have restarted (token rotated). Try again."
            return 1
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
                    err "key $key not active for project $pid (paused for this project, or not declared by any installed module)"
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

# ── Tier 2: file store ($VCT_SECRETS_DIR, default ~/.vct-secrets) ───────
#
# Mirrors `vco_lib/agent_secrets.py::_file_store_get` (must match):
# projects/<NAME>/<key> first (when a project NAME applies), then
# shared/<key>. Strips exactly ONE trailing newline (vct-exec
# semantics). A simple first arg (no path separators) is used verbatim
# as the file-store project name; a path walks up for a `.vct-project`
# marker file.
secrets_root() {
    printf '%s' "${VCT_SECRETS_DIR:-$HOME/.vct-secrets}"
}

detect_file_project_name() {
    # $1 = the original project arg (path or simple name). Prints the
    # file-store project NAME, or nothing when none applies.
    local arg="$1"
    if [[ -n "$arg" ]] && ! looks_like_path "$arg"; then
        printf '%s' "$arg"
        return 0
    fi
    local cur
    cur=$(cd "$(dirname "$arg" 2>/dev/null || printf '.')" 2>/dev/null && pwd) || return 0
    if [[ -d "$arg" ]]; then
        cur=$(cd "$arg" && pwd) || return 0
    fi
    while [[ -n "$cur" && "$cur" != "/" ]]; do
        if [[ -f "$cur/.vct-project" ]]; then
            local name
            name=$(head -n 1 "$cur/.vct-project" 2>/dev/null | tr -d '[:space:]')
            [[ -n "$name" ]] && printf '%s' "$name"
            return 0
        fi
        cur=$(dirname "$cur")
    done
    return 0
}

read_file_strip_one_newline() {
    # $1 = file. Prints contents with exactly ONE trailing newline
    # stripped (command substitution would strip ALL — we preserve any
    # additional ones to match the Python helper byte-for-byte).
    local f="$1" raw
    raw=$(cat "$f" 2>/dev/null; printf x) || return 1
    raw="${raw%x}"
    raw="${raw%$'\n'}"
    printf '%s' "$raw"
}

file_store_get() {
    # $1 = project arg, $2 = key. Prints the value; return 1 on miss.
    local proj_arg="$1" key="$2"
    local root name f
    root=$(secrets_root)
    name=$(detect_file_project_name "$proj_arg")
    if [[ -n "$name" && -f "$root/projects/$name/$key" ]]; then
        read_file_strip_one_newline "$root/projects/$name/$key"
        return 0
    fi
    f="$root/shared/$key"
    if [[ -f "$f" ]]; then
        read_file_strip_one_newline "$f"
        return 0
    fi
    return 1
}

# ── Tier 3: the project's own .env (READ-ONLY, lowest priority) ─────────
#
# Parsing rule (must match vct_secrets_resolve.ps1 and
# agent_secrets.py::_parse_dotenv_value): line-oriented; accept
# `KEY=VALUE` and `export KEY=VALUE`; strip one matching pair of
# single/double quotes; NO variable expansion, NO command substitution;
# first match wins. Never writes, never caches, never re-exports.
dotenv_get() {
    # $1 = project folder, $2 = key. Prints the value; return 1 on miss.
    local folder="$1" key="$2"
    local f="$folder/.env"
    [[ -f "$f" ]] || return 1
    local line s k v
    while IFS= read -r line || [[ -n "$line" ]]; do
        # ltrim
        s="${line#"${line%%[![:space:]]*}"}"
        case "$s" in
            "export "*)
                s="${s#export }"
                s="${s#"${s%%[![:space:]]*}"}"
                ;;
        esac
        [[ -z "$s" || "$s" == \#* ]] && continue
        [[ "$s" == *=* ]] || continue
        k="${s%%=*}"
        k="${k%"${k##*[![:space:]]}"}"
        [[ "$k" == "$key" ]] || continue
        v="${s#*=}"
        # trim surrounding whitespace (also strips a CR from CRLF lines)
        v="${v#"${v%%[![:space:]]*}"}"
        v="${v%"${v##*[![:space:]]}"}"
        # strip ONE matching pair of quotes
        if [[ ${#v} -ge 2 ]]; then
            local first="${v:0:1}" last="${v: -1}"
            if [[ "$first" == "$last" && ( "$first" == '"' || "$first" == "'" ) ]]; then
                v="${v:1:${#v}-2}"
            fi
        fi
        printf '%s' "$v"
        return 0
    done < "$f"
    return 1
}

# ── Main subcommand: read a single key through the full chain ───────────
read_key() {
    # $1 = project_id_or_folder, $2 = key.
    # Chain: hub (tier 1) → file store (tier 2) → project .env (tier 3).
    # The final exit code on all-miss is TIER 1's code, preserving the
    # historical contract (exit 3 = key_not_active only after tiers 2
    # and 3 also missed).
    local pid_arg="$1" key="$2"
    local val tier1_rc
    set +e
    val=$(read_key_hub "$pid_arg" "$key")
    tier1_rc=$?
    set -e
    if [[ $tier1_rc -eq 0 ]]; then
        printf '%s' "$val"
        return 0
    fi
    # Tier 2: file store.
    set +e
    val=$(file_store_get "$pid_arg" "$key")
    local rc2=$?
    set -e
    if [[ $rc2 -eq 0 ]]; then
        printf '%s' "$val"
        return 0
    fi
    # Tier 3: project .env — only when the first arg names a folder (a
    # bare project id gives no folder to read; if the hub is up the key
    # already had its tier-1 chance).
    if looks_like_path "$pid_arg"; then
        local env_dir="$pid_arg"
        [[ -f "$env_dir" ]] && env_dir=$(dirname "$env_dir")
        set +e
        val=$(dotenv_get "$env_dir" "$key")
        local rc3=$?
        set -e
        if [[ $rc3 -eq 0 ]]; then
            printf '%s' "$val"
            return 0
        fi
    else
        err "tier 3 (.env) skipped: first arg is a project id, not a folder — re-invoke with the project folder to consult its .env"
    fi
    err "key $key unresolved after hub (tier 1), file store (tier 2), and project .env (tier 3)"
    return $tier1_rc
}

# ── Entry point ─────────────────────────────────────────────────────────
main() {
    if [[ $# -lt 2 ]]; then
        cat >&2 <<EOF
Usage:
  $0 <project_id_or_folder> <secret_key>
  $0 resolve-project <folder>

Resolution chain: hub (keychain) -> file store (~/.vct-secrets) ->
project .env (read-only; folder arg only).

Exit codes (non-zero = key ALSO missed the file store + project .env):
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
