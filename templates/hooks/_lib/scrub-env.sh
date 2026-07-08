# shellcheck shell=bash
# _lib/scrub-env.sh — canonical secret-env scrub list (HK-2, v0.2.73).
#
# THE single source of truth for the set of sensitive environment
# variables every hook unsets BEFORE spawning any subprocess. Previously
# this `unset ...` line was copy-pasted verbatim across ~40 .sh + ~40 .ps1
# hooks (risk R10): adding a new secret env var meant editing 80 files,
# and a single miss is a credential-leak surface.
#
# Single consumer:
#   * The parity gate (tests/test_scrub_env_parity_v0273.py) reads
#     ``VCT_SCRUB_SECRET_KEYS`` as the canonical set and asserts every hook's
#     inline `unset ... GITHUB_TOKEN ...` line covers EXACTLY it — so a hook
#     that drifts from the list fails CI.
#
# HK-2 (v0.2.75): the `vct_scrub_secret_env()` HELPER FUNCTION that used to
# live here was DELETED — it had ZERO callers (every hook carries its own
# inline `unset ...` line, enforced by the parity gate, so hooks stay
# dependency-free single files). Keeping a never-called function around was
# dead code that implied a sourcing contract no hook actually uses. The
# canonical VALUE below stays; the parity gate is the enforcement.
#
# MUST MATCH ``_lib/scrub-env.ps1::$VctScrubSecretKeys`` and the inline
# unset lists in every templates/hooks/*.sh (enforced by the parity gate).
# Add a new secret key HERE (and the .ps1 sibling) first; the gate then
# points at every hook whose inline list needs the same key.

# Canonical list — keep sorted-by-topic, one string, space-separated.
VCT_SCRUB_SECRET_KEYS="SUPABASE_KEY SUPABASE_URL GITHUB_TOKEN GH_TOKEN OPENAI_API_KEY ANTHROPIC_API_KEY AWS_SECRET_ACCESS_KEY AWS_ACCESS_KEY_ID TELEGRAM_BOT_TOKEN POSTGRES_PASSWORD VERCEL_TOKEN CLAUDE_API_KEY"
export VCT_SCRUB_SECRET_KEYS
