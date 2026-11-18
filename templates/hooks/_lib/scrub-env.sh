# shellcheck shell=bash
# _lib/scrub-env.sh — canonical secret-env scrub list (HK-2, v0.2.73).
#
# THE single source of truth for the set of sensitive environment
# variables every hook unsets BEFORE spawning any subprocess. Previously
# this `unset ...` line was copy-pasted verbatim across ~40 .sh + ~40 .ps1
# hooks (risk R10): adding a new secret env var meant editing 80 files,
# and a single miss is a credential-leak surface.
#
# Consumers (v0.2.107):
#   * Every templates/hooks/*.sh sources this file at its top and calls
#     vct_scrub_secret_env; the hook's inline `unset ...` line runs only when
#     this file is missing or fails to load (a broken install), as the .ps1
#     hooks do with _lib/scrub-env.ps1.
#   * The parity gate (tests/test_scrub_env_parity_v0273.py) reads
#     ``VCT_SCRUB_SECRET_KEYS`` as the canonical set and asserts every hook's
#     inline fallback covers EXACTLY it.
#
# HK-2 (v0.2.75) had deleted an uncalled `vct_scrub_secret_env`; v0.2.107
# brings it back WITH its callers, so this file is the live list rather
# than a reference copy (tests/test_v02107_scrub_env_lib_sourced.py).
#
# MUST MATCH ``_lib/scrub-env.ps1::$VctScrubSecretKeys`` and the inline
# unset lists in every templates/hooks/*.sh (enforced by the parity gate).
# Add a new secret key HERE (and the .ps1 sibling) first; the gate then
# points at every hook whose inline list needs the same key.

# Canonical list — keep sorted-by-topic, one string, space-separated.
# Internal (docs/CONFIGURATION.md): assigned here, never read from the
# environment, and not exported -- no child needs the list.
VCT_SCRUB_SECRET_KEYS="SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY"

# Unset every key in the canonical list (this shell and every child it
# starts). Always succeeds, so a hook's `... && vct_scrub_secret_env ||
# <inline unset>` takes the fallback only when this file did not load.
vct_scrub_secret_env() {
    local _vct_k
    for _vct_k in $VCT_SCRUB_SECRET_KEYS; do
        unset "$_vct_k" 2>/dev/null
    done
    return 0
}
