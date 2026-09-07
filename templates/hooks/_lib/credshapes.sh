# shellcheck shell=bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
#
# credshapes.sh — the bash MIRROR of the credential-SHAPE vocabulary SSOT.
#
# SSOT: vco_lib/credential_shapes.py. Python is the source of truth (A>B>C).
# This file re-implements the DATA in pure bash because its consumers cannot
# take a Python dependency: _lib/credscan.sh is pure `grep` with no interpreter
# at all, and these hooks ship into user projects where no venv / PYTHONPATH is
# guaranteed. A `python -m` call would silently no-op the scanner exactly where
# it is most needed. Locked to the SSOT by
# tests/fixtures/credential_shape_parity.json (see
# tests/test_credential_shapes_parity.py).
#
# THE FOUR CONTEXTS ARE NOT INTERCHANGEABLE. Each encodes a different
# precision/recall bias earned from a different haystack — read the SSOT
# docstring before using one. In short:
#   repo_scan     whole tree incl. vendored bundles + binaries. PRECISION.
#   content_scan  one text file a user just edited. Recall-leaning.
#   command_scan  one shell command line. MAXIMUM recall.
#   argv_name     one hand-typed identifier, whole-string anchored.
# Picking the wrong one is a silent security or noise bug, so the accessor
# below REFUSES an unknown context rather than defaulting to anything.
#
# Consumers of this file (all under the hooks deployment root):
#   scripts/check-no-secrets.sh          -> repo_scan
#   templates/hooks/_lib/credscan.sh     -> content_scan
#   templates/hooks/post-tool-security.sh-> content_scan
#
# A SECOND bash mirror exists at tools/vct-secrets/lib/credential_shapes.sh for
# the `vct` CLI. That is forced by deployment topology (.claude/hooks/_lib/ and
# ~/.vct-secrets/lib/ are disjoint trees, and a relative symlink between them
# would dangle under the documented `cp -a` deployment), and it carries ONLY the
# argv_name projection — it is not a copy of this file.
#
# This library never prints, logs or echoes a candidate value; it only hands
# back labels and patterns.
#
# Portability: the patterns are the ERE / Python / .NET intersection. Use with
# `grep -E`. bash 3.2+ (incl. macOS's bundled bash); no GNU-only constructs.

# ---------------------------------------------------------------------------
# The vocabulary — mirrors vco_lib/credential_shapes.py::SHAPES EXACTLY, in
# declaration order (label order is part of the contract: scanners report
# labels in this sequence).
#
# Absence of a shape from a context is DELIBERATE, never an omission:
#   aws_access_key_id / atlassian_token / jwt are absent from repo_scan because
#   short upper-alnum and `eyJ` prefixes collide with base64 payloads inside
#   vendored bundles (PROVEN: the Excalidraw base64 WASM/font chunk in this
#   repo contains an AKIA+16 run by chance).
#   pem_private_key is absent from command_scan and argv_name.
# ---------------------------------------------------------------------------

# credshapes_for_context CONTEXT
#   Populates the parallel arrays CREDSHAPES_IDS[], CREDSHAPES_LABELS[] and
#   CREDSHAPES_PATTERNS[] (index-aligned; IDS are the stable SSOT shape ids,
#   which consumers should key on rather than on display labels)
#   with every shape serving CONTEXT, in SSOT declaration order.
#   Returns 0 on success; on an unknown context it CLEARS all arrays and
#   returns 1 — a caller that ignores the status scans nothing rather than
#   scanning with the wrong bias.
# CREDSHAPES_IDS / CREDSHAPES_LABELS / CREDSHAPES_PATTERNS are this
# library's OUTPUT contract: they are read by the SOURCING script
# (check-no-secrets.sh, credscan.sh, post-tool-security.sh), which the
# linter cannot see from here - hence the unused-variable suppression.
# shellcheck disable=SC2034
credshapes_for_context() {
    local ctx="${1:-}"
    CREDSHAPES_IDS=()
    CREDSHAPES_LABELS=()
    CREDSHAPES_PATTERNS=()
    case "$ctx" in
        repo_scan)
            CREDSHAPES_IDS=(
                'github_token'
                'github_fine_grained_pat'
                'gitlab_pat'
                'slack_token'
                'sk_vendor_key'
                'pem_private_key'
            )
            CREDSHAPES_LABELS=(
                "GitHub token"
                "GitHub fine-grained PAT"
                "GitLab PAT (glpat-)"
                "Slack token (xox*)"
                "Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)"
                "PEM private key"
            )
            CREDSHAPES_PATTERNS=(
                'gh[pousr]_[A-Za-z0-9]{36}'
                'github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}'
                'glpat-[A-Za-z0-9_-]{20,}'
                'xox[bpoas]-[A-Za-z0-9-]{20,}'
                'sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{32,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}'
                '-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----'
            )
            ;;
        content_scan)
            CREDSHAPES_IDS=(
                'github_token'
                'github_fine_grained_pat'
                'aws_access_key_id'
                'gitlab_pat'
                'slack_token'
                'atlassian_token'
                'jwt'
                'sk_vendor_key'
                'pem_private_key'
                'generic_secret_quoted'
                'generic_secret_unquoted'
                'leak_probe'
            )
            CREDSHAPES_LABELS=(
                "GitHub token"
                "GitHub fine-grained PAT"
                "AWS access key ID (AKIA)"
                "GitLab PAT (glpat-)"
                "Slack token (xox*)"
                "Atlassian API token"
                "JWT / bearer-shaped token"
                "Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)"
                "PEM private key"
                "Generic secret"
                "Generic secret (unquoted)"
                "Hook leak-test marker"
            )
            CREDSHAPES_PATTERNS=(
                'gh[pousr]_[A-Za-z0-9]{36}'
                'github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}'
                'AKIA[0-9A-Z]{16}'
                'glpat-[A-Za-z0-9_-]{20,}'
                'xox[bpoas]-[A-Za-z0-9-]{20,}'
                'ATATT[A-Za-z0-9_=-]{20,}'
                'eyJ[A-Za-z0-9_-]{20,}'
                'sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{32,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{20,}'
                'BEGIN [A-Z0-9 ]*PRIVATE KEY'
                '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*["'"'"'][a-zA-Z0-9+/=_\-]{32,}'
                '(SECRET|API_KEY|ACCESS_TOKEN|PRIVATE_KEY)\s*[:=]\s*[a-zA-Z0-9+/=_\-]{32,}'
                'VCT_HOOK_LEAK_PROBE_a3f7c2'
            )
            ;;
        command_scan)
            CREDSHAPES_IDS=(
                'github_token'
                'github_fine_grained_pat'
                'aws_access_key_id'
                'gitlab_pat'
                'slack_token'
                'atlassian_token'
                'jwt'
                'sk_vendor_key'
            )
            CREDSHAPES_LABELS=(
                "GitHub token"
                "GitHub fine-grained PAT"
                "AWS access key ID (AKIA)"
                "GitLab PAT (glpat-)"
                "Slack token (xox*)"
                "Atlassian API token"
                "JWT / bearer-shaped token"
                "Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)"
            )
            CREDSHAPES_PATTERNS=(
                'gh[pousr]_[A-Za-z0-9]{8,}'
                'github_pat_[A-Za-z0-9_]{8,}'
                'AKIA[0-9A-Z]{16}'
                'glpat-[A-Za-z0-9_-]{8,}'
                'xox[bpoas]-[A-Za-z0-9-]{8,}'
                'ATATT[A-Za-z0-9_=-]{8,}'
                'eyJ[A-Za-z0-9_-]{20,}'
                'sk-([A-Za-z0-9]+-){0,3}[A-Za-z0-9]{16,}|sk-(proj|svcacct|admin)-[A-Za-z0-9_-]{8,}'
            )
            ;;
        argv_name)
            CREDSHAPES_IDS=(
                'github_token'
                'github_fine_grained_pat'
                'aws_access_key_id'
                'gitlab_pat'
                'slack_token'
                'atlassian_token'
                'jwt'
                'sk_vendor_key'
            )
            CREDSHAPES_LABELS=(
                "GitHub token"
                "GitHub fine-grained PAT"
                "AWS access key ID (AKIA)"
                "GitLab PAT (glpat-)"
                "Slack token (xox*)"
                "Atlassian API token"
                "JWT / bearer-shaped token"
                "Vendor API key (sk-*: OpenAI / Anthropic / OpenRouter / compatible)"
            )
            CREDSHAPES_PATTERNS=(
                '^gh[pousr]_[A-Za-z0-9]{36}$'
                '^github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}$'
                '^AKIA[0-9A-Z]{16}$'
                '^glpat-[A-Za-z0-9_-]{20,}$'
                '^xox[bpoas]-[A-Za-z0-9_-]{20,}$'
                '^ATATT[A-Za-z0-9_-]{20,}$'
                '^eyJ[A-Za-z0-9_-]{20,}$'
                '^sk-[A-Za-z0-9_-]{20,}$'
            )
            ;;
        *)
            return 1
            ;;
    esac
    return 0
}

# credshapes_combined CONTEXT
#   Print one `|`-joined alternation of every pattern serving CONTEXT.
#   Prints nothing and returns 1 on an unknown context.
credshapes_combined() {
    credshapes_for_context "${1:-}" || return 1
    local out="" p
    for p in "${CREDSHAPES_PATTERNS[@]}"; do
        if [ -z "$out" ]; then out="$p"; else out="$out|$p"; fi
    done
    printf '%s' "$out"
}
