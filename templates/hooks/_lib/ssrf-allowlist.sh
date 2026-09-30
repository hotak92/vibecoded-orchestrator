# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
# _lib/ssrf-allowlist.sh — WHICH private-network URLs the pre-tool-use SSRF
# guard lets a WebFetch reach: this machine's own VCO services.
# Windows mirror: _lib/ssrf-allowlist.ps1 (MUST stay logically identical).
#
# v0.2.100 WP-18B: the allowlist used to be the literal port set
# 8081|8082|11435|11440|7860 on localhost / 127.0.0.1. A user whose
# service_endpoints row moved Weaviate (e.g. to 18081) had WebFetch to their
# own Weaviate blocked, and the message told them to hand-edit a shipped hook
# — which the next bundle update adopts back. The pairs are now DERIVED at run
# time from the projected env (config_projection writes these from the
# service_endpoints rows into .claude/settings.json env / .claude/env):
#
#   Weaviate   WEAVIATE_URL, else http://localhost:$WEAVIATE_PORT, else :8081
#              (the weaviate_helpers.weaviate_url_default ladder)
#   Ollama     OLLAMA_URL, else :11435
#   code-embed CODE_EMBED_SERVICE_URL, else :11440
#   vct-hub    the resolver's `hub-port` (VCT_HUB_PORT -> hub.port -> 7700)
#   always     :8082 and Gradio :7860 (no env knob; the historical literals)
#
# The literal ports are the per-service FALLBACK when that service's env is
# silent — never an addition to a stated value. A loopback pair is allowed as
# localhost, 127.0.0.1 and [::1] alike; a non-loopback pair (an adopted
# service on gpu.lan) is allowed as exactly that host:port.
#
# Matching is on the URL's AUTHORITY (scheme default port applied, userinfo
# dropped, case-folded), not a substring of the whole URL: the old grep let
# `http://localhost:8081@internal-host:22/` through as "whitelisted".
#
# Usage:
#     . "$SCRIPT_DIR/_lib/ssrf-allowlist.sh"
#     vco_ssrf_url_allowed "$URL" "$PROJECT_ROOT"   # exit 0 = allowed
#     vco_ssrf_allowed_pairs "$PROJECT_ROOT"        # one host:port per line

# vco_ssrf_authority URL :: prints lowercase `host:port` (IPv6 host bracketed;
# the scheme's default port when none is given), or fails when unparsable.
vco_ssrf_authority() {
    local u="$1" scheme="http" auth host port
    if [[ "$u" == *"://"* ]]; then
        scheme="${u%%://*}"
        u="${u#*://}"
    fi
    scheme="$(printf '%s' "$scheme" | tr '[:upper:]' '[:lower:]')"
    auth="${u%%[/?#]*}"
    auth="${auth##*@}"
    auth="$(printf '%s' "$auth" | tr '[:upper:]' '[:lower:]')"
    if [[ "$auth" == \[* ]]; then
        host="${auth%%]*}]"
        port="${auth#"$host"}"
        port="${port#:}"
    elif [[ "$auth" == *:* ]]; then
        host="${auth%:*}"
        port="${auth##*:}"
    else
        host="$auth"
        port=""
    fi
    if [[ -z "$port" ]]; then
        case "$scheme" in
            https) port=443 ;;
            *) port=80 ;;
        esac
    fi
    [[ -n "$host" && "$port" =~ ^[0123456789]{1,5}$ ]] || return 1
    printf '%s:%s\n' "$host" "$((10#$port))"
}

# _vco_ssrf_expand HOST:PORT :: the pair, or its three loopback spellings.
_vco_ssrf_expand() {
    local pair="$1" host port
    host="${pair%:*}"
    port="${pair##*:}"
    case "$host" in
        localhost|127.0.0.1|"[::1]")
            printf 'localhost:%s\n127.0.0.1:%s\n[::1]:%s\n' "$port" "$port" "$port"
            ;;
        *) printf '%s\n' "$pair" ;;
    esac
}

# _vco_ssrf_env VAR :: the variable's value with surrounding whitespace removed
# (an empty or whitespace-only value is UNSET, as in weaviate_url_default).
_vco_ssrf_env() {
    local v="${!1:-}" pad=$' \t\n\v\f\r'
    v="${v#"${v%%[!$pad]*}"}"
    v="${v%"${v##*[!$pad]}"}"
    printf '%s' "$v"
}

# _vco_ssrf_hub_port PROJECT_ROOT :: the hub port through the ONE shell
# resolver (vct_project_config.sh hub-port); 7700 when it is absent or fails.
_vco_ssrf_hub_port() {
    local resolver="${1:-.}/.claude/scripts/vct_project_config.sh" p=""
    if [[ -f "$resolver" ]]; then
        p="$(bash "$resolver" hub-port 2>/dev/null)" || p=""
    fi
    [[ "$p" =~ ^[0123456789]{1,5}$ ]] || p=7700
    printf '%s\n' "$p"
}

# vco_ssrf_allowed_pairs [PROJECT_ROOT] :: every allowed host:port, one per line.
vco_ssrf_allowed_pairs() {
    local root="${1:-.}" v pair
    local -a urls=()
    v="$(_vco_ssrf_env WEAVIATE_URL)"
    if [[ -z "$v" ]]; then
        v="$(_vco_ssrf_env WEAVIATE_PORT)"
        v="http://localhost:${v:-8081}"
    fi
    urls+=("$v")
    v="$(_vco_ssrf_env OLLAMA_URL)"
    urls+=("${v:-http://localhost:11435}")
    v="$(_vco_ssrf_env CODE_EMBED_SERVICE_URL)"
    urls+=("${v:-http://localhost:11440}")
    urls+=("http://localhost:$(_vco_ssrf_hub_port "$root")")
    urls+=("http://localhost:8082" "http://localhost:7860")
    for v in "${urls[@]}"; do
        pair="$(vco_ssrf_authority "$v")" || continue
        _vco_ssrf_expand "$pair"
    done
}

# vco_ssrf_url_allowed URL [PROJECT_ROOT] :: exit 0 when URL's authority is
# one of the allowed pairs.
vco_ssrf_url_allowed() {
    local target line
    target="$(vco_ssrf_authority "$1")" || return 1
    while IFS= read -r line; do
        [[ "$line" == "$target" ]] && return 0
    done < <(vco_ssrf_allowed_pairs "${2:-.}")
    return 1
}
