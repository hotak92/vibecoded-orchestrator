# shellcheck shell=bash
# _lib/scrub-env.sh — canonical secret-env scrub list (HK-2, v0.2.73).
#
# THE single source of truth for the set of sensitive environment
# variables every hook unsets BEFORE spawning any subprocess. Previously
# this `unset ...` line was copy-pasted verbatim across ~40 .sh + ~40 .ps1
# hooks (risk R10): adding a new secret env var meant editing 80 files,
# and a single miss is a credential-leak surface.
#
# Two consumers:
#   1. Hooks that source this lib call `vct_scrub_secret_env` as their
#      first executable line (secret-free, so safe to do before the
#      VCT_DISABLE_HOOKS guard).
#   2. The parity gate (tests/test_scrub_env_parity_v0273.py) asserts every
#      hook's inline `unset ... GITHUB_TOKEN ...` line covers EXACTLY this
#      canonical set — so a hook that drifts from the list fails CI.
#
# MUST MATCH ``_lib/scrub-env.ps1::$VctScrubSecretKeys`` and the inline
# unset lists in every templates/hooks/*.sh (enforced by the parity gate).
# Add a new secret key HERE (and the .ps1 sibling) first; the gate then
# points at every hook whose inline list needs the same key.

# Canonical list — keep sorted-by-topic, one string, space-separated.
VCT_SCRUB_SECRET_KEYS="SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY"
export VCT_SCRUB_SECRET_KEYS

# Unset every canonical secret key in the current shell. Idempotent; never
# fails (unset of an absent var is a no-op). Safe to call before the
# VCT_DISABLE_HOOKS guard — it spawns no subprocess and reads no secret.
vct_scrub_secret_env() {
    # shellcheck disable=SC2086 — deliberate word-splitting over the list.
    unset $VCT_SCRUB_SECRET_KEYS 2>/dev/null || true
}
