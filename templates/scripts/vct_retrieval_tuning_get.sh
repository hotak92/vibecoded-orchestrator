#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# vct_retrieval_tuning_get.sh — read the global retrieval tuning values
# (KG tier thresholds + codegraph injection floor) from the launcher hub.
#
# v0.2.22 Item #13 (2026-05-20). The launcher's Preferences →
# Retrieval tuning panel writes <vct_root_dir>/retrieval-tuning.toml;
# this script reads the resolved values back via the hub's per-project
# config resolver (no per-project override yet; the resolver embeds
# the same global block in every response).
#
# Discovery follows the resolver-client convention: hub.port + hub.token
# from $VCT_HUB_PORT / $VCT_HUB_TOKEN or <vct_root_dir>/{hub.port,hub.token}.
# This script delegates to `vct_project_config.sh --field retrieval_tuning`
# so the hub-discovery + 401-retry + soft-fail logic lives in ONE place.
#
# Usage:
#   vct_retrieval_tuning_get.sh <project_folder> [--field NAME]
#       Print the full retrieval-tuning JSON object for the project.
#       With --field NAME, print only one threshold (e.g. kg_tier_min).
#
# Exit codes (mirror vct_project_config.sh):
#   0  success
#   1  hub unreachable (falls back to reading the file directly)
#   2  project not registered
#   3  service misconfigured (no primary KG binding)
#   4  field not found
#   5  forbidden (hub refused the token on /env|/config; NO file fallback —
#      a refusal must never silently fall back to the local TOML)
#   64 usage error
#
# Hub-down fallback: if the hub is unreachable, the script falls back
# to reading <vct_root_dir>/retrieval-tuning.toml directly and printing
# either the whole block or the requested field. This keeps the
# headless flow working when the launcher is closed and the hub binary
# isn't running. The fallback path emits ONE [vct-retrieval-tuning]
# warning line to stderr so the caller can see when it kicked in.

set -euo pipefail

# Locate the resolver-client helper (sibling file in templates/scripts/).
_THIS_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
_RESOLVER_CLIENT="${_THIS_DIR}/vct_project_config.sh"

err() { printf '[vct-retrieval-tuning] %s\n' "$*" >&2; }

# ── File-fallback reader ────────────────────────────────────────────────
# When the hub is unreachable, fall back to the TOML file directly so
# offline / launcher-closed flows still work. Uses python3 for the
# parse (TOML in pure bash is too fragile to be worth maintaining).
read_from_file_fallback() {
    local field="${1:-}"
    local state_dir="${VCT_STATE_DIR:-$HOME/.vct}"
    local toml_path="$state_dir/retrieval-tuning.toml"

    if [[ ! -f "$toml_path" ]]; then
        # File missing → emit hard-coded defaults so callers always see
        # the same numbers the GUI defaults to. Defaults pinned from
        # knowledge/concepts/score-driven-retrieval-tiers.md.
        if command -v python3 >/dev/null 2>&1; then
            python3 -c "
import json, sys
defaults = {
    'code_graph_score_floor': 0.35,
    'kg_tier_min': 0.42,
    'kg_tier_single_chunk': 0.55,
    'kg_tier_three_chunks': 0.65,
    'kg_tier_full': 0.75,
}
field = '${field}'
if field:
    if field in defaults:
        sys.stdout.write(str(defaults[field]))
    else:
        sys.exit(4)
else:
    sys.stdout.write(json.dumps(defaults))
" || return $?
            return 0
        fi
        err "neither hub reachable nor python3 available; cannot synthesize defaults"
        return 1
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        err "hub unreachable and python3 missing; cannot parse $toml_path"
        return 1
    fi

    VCT_TOML_PATH="$toml_path" VCT_TOML_FIELD="${field}" python3 -c "
import json, os, sys
path = os.environ['VCT_TOML_PATH']
field = os.environ.get('VCT_TOML_FIELD', '')

# Pure-python TOML parser available in stdlib since 3.11.
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore
    except ImportError:
        sys.stderr.write('[vct-retrieval-tuning] need python>=3.11 or tomli\n')
        sys.exit(1)

defaults = {
    'code_graph_score_floor': 0.35,
    'kg_tier_min': 0.42,
    'kg_tier_single_chunk': 0.55,
    'kg_tier_three_chunks': 0.65,
    'kg_tier_full': 0.75,
}

try:
    with open(path, 'rb') as f:
        parsed = tomllib.load(f)
except Exception as e:
    sys.stderr.write(f'[vct-retrieval-tuning] could not parse {path}: {e}; using defaults\n')
    parsed = {}

# Fill missing keys from defaults so the output is always complete.
out = {k: parsed.get(k, defaults[k]) for k in defaults}

if field:
    if field in out:
        sys.stdout.write(str(out[field]))
    else:
        sys.exit(4)
else:
    sys.stdout.write(json.dumps(out))
"
}

# ── Main ────────────────────────────────────────────────────────────────
usage() {
    cat >&2 <<EOF
Usage: $0 <project_folder> [--field NAME]

Returns the global retrieval tuning thresholds (KG tier cutoffs +
codegraph injection floor) for the named project. Falls back to
reading <vct_root_dir>/retrieval-tuning.toml directly if the hub is
unreachable.

Exit codes:
  0  success
  1  hub unreachable AND file fallback failed
  2  project not registered
  3  service misconfigured
  4  field not found
  5  forbidden (hub refused the token; no file fallback)
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
    esac

    local project_arg="$1"
    shift
    local field=""
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

    if [[ ! -x "$_RESOLVER_CLIENT" ]]; then
        err "resolver client not executable: $_RESOLVER_CLIENT"
        exit 64
    fi

    # Try the hub first. The resolver client owns hub discovery + auth.
    local body rc
    set +e
    if [[ -n "$field" ]]; then
        # vct_project_config.sh's --field returns nested objects as JSON
        # strings; we want the leaf VALUE, so always fetch the whole
        # nested block then extract the field locally with jq / python.
        body=$("$_RESOLVER_CLIENT" "$project_arg" --field retrieval_tuning 2>/dev/null)
        rc=$?
    else
        body=$("$_RESOLVER_CLIENT" "$project_arg" --field retrieval_tuning 2>/dev/null)
        rc=$?
    fi
    set -e

    if [[ $rc -eq 0 && -n "$body" ]]; then
        if [[ -z "$field" ]]; then
            printf '%s\n' "$body"
            return 0
        fi
        # Field extraction from the nested JSON envelope.
        local val=""
        if command -v jq >/dev/null 2>&1; then
            val=$(printf '%s' "$body" | jq -r --arg k "$field" '.[$k] // empty')
        elif command -v python3 >/dev/null 2>&1; then
            val=$(printf '%s' "$body" | VCT_FIELD="$field" python3 -c '
import json, os, sys
try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
key = os.environ.get("VCT_FIELD", "")
if isinstance(data, dict) and key in data:
    sys.stdout.write(str(data[key]))
')
        fi
        if [[ -n "$val" ]]; then
            printf '%s\n' "$val"
            return 0
        fi
        err "field $field not in retrieval_tuning envelope (hub mode)"
        return 4
    fi

    # Map the resolver-client exit codes to our shape. Non-1 errors
    # propagate; rc=1 (hub unreachable) triggers file fallback.
    #
    # v0.2.77 L3-F2: rc=5 is FORBIDDEN (the hub refused the token on the
    # gated /env|/config route). A refusal must NEVER fall back to the local
    # TOML — that would silently mask a scoped-token misconfiguration and
    # emit a bogus "hub unreachable" diagnostic. Propagate exit 5 with an
    # honest message instead.
    case "$rc" in
        2|3|4)
            exit "$rc"
            ;;
        5)
            err "hub refused the request (403 forbidden) — present a scoped hub.token.<project_id> or set VCT_HUB_LEGACY_GLOBAL_ENV=1 on the hub to reopen the compat window; NOT falling back to the local TOML"
            exit 5
            ;;
    esac

    # Hub unreachable → file fallback.
    err "hub unreachable; reading <vct_root_dir>/retrieval-tuning.toml directly"
    if read_from_file_fallback "$field"; then
        printf '\n'
        return 0
    fi
    return $?
}

main "$@"
