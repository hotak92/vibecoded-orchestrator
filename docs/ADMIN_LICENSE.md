# Admin license (Bug 33)

Admin licenses unlock dev-only affordances in the launcher: a persistent
ADMIN badge, an Admin sidebar group with feature-flag / diagnostic /
license-test routes, and visibility on `private-test` modules in the
catalog.

This document is the canonical reference. It supersedes any earlier draft
mentioning a local `MAINTAINER_TOKEN` / Ed25519 bypass — that approach was
dropped because it was too easy to defeat with a one-line client patch.

## Architecture

Admin is a real Lemon Squeezy license, not a local bypass. The flow is
identical to Pro / MAO / Enterprise, with one extra branch on the server:

```
launcher / orchestrator
   │
   │  POST /functions/v1/validate-tier { license_key, machine_id_hash }
   ▼
Supabase edge function (validate-tier)
   │
   ├── lookupVariant(variant_id)
   │     1. isAdminVariant(variant_id) ?  ──► tier="admin"
   │                                          (variant_id ∈ env LS_ADMIN_VARIANT_IDS)
   │     2. else VARIANT_MAP[variant_id]  ──► tier="pro"|"mao"|"enterprise"
   │
   ▼
returns { valid: true, tier: "admin", is_admin: true,
          unlock_all_modules: true, dev_features_enabled: true }
```

The admin variant ID lives in the Supabase env var `LS_ADMIN_VARIANT_IDS`
(JSON array of strings), NEVER in the public AGPL source. Open-source
readers see the resolution function (`isAdminVariant` in
`launcher/supabase/functions/_shared/variant_map.ts`) but cannot derive the
variant ID — `isAdminVariant` consults the runtime env, not source data.

## Setup (maintainer, one-time)

1. **Lemon Squeezy dashboard**: create a new product OR a new variant
   under an existing product. Name it "Admin / Maintainer". Set price = $0.
   Configure for unlimited issuance via test-mode or complimentary
   license keys.
2. **Note the variant ID** from the variant URL or API response.
3. **Supabase env**: set `LS_ADMIN_VARIANT_IDS` to a JSON array
   containing that variant ID (and any future admin variant IDs):
   ```
   LS_ADMIN_VARIANT_IDS=["VARIANT_ID_HERE"]
   ```
   This is set on the Supabase project's env settings, NOT in repo
   source. The validate-tier edge function reads it via
   `Deno.env.get("LS_ADMIN_VARIANT_IDS")`.
4. **Issue admin license keys** to teammates via the LS dashboard:
   Admin variant → Issue test license OR complimentary license.
5. **Distribute the license key** to the teammate (DM, encrypted note,
   etc.).

## Activation (teammate)

1. Run the launcher.
2. Open Settings → License → Activate.
3. Paste the admin license key.
4. The launcher calls validate-tier, gets back `tier=admin`, caches it.
5. The ADMIN badge appears in the bottom-right corner. The Admin
   sidebar group becomes visible.

The admin tier is treated as a strict superset of `enterprise` for
feature gates: `require_tier("enterprise")` → True for admins.

## Revocation

1. Open the LS dashboard → Admin variant → find the license → Disable.
2. The launcher's validator re-checks at most every 24h (cache TTL +
   on-demand refresh). On the next refresh, tier drops to `free` (or
   whatever underlying retail tier the user might have on the same
   account).
3. The ADMIN badge disappears, admin routes hide, private-test modules
   leave the catalog.

To force an immediate refresh on the user side, they can run
`license_refresh` from the Settings panel.

## Security model

| Capability                  | Tampering with the client gives the attacker… |
|-----------------------------|-----------------------------------------------|
| ADMIN badge                 | …a yellow badge. Nothing else.               |
| Admin sidebar group visible | …route links that 4xx server-side.           |
| `private-test` modules visible in catalog | …a list of names. Module artifacts are still gated by signed-URL gateway, which re-validates the JWT issued by validate-tier on every download. |
| `is_admin()` returns True locally | …local feature flags lit up. Server-gated capabilities (paid-module artifact downloads, license issuance, telemetry inspector data) all re-validate against the Supabase JWT — a self-claimed admin tier yields nothing the AGPL source doesn't already. |

The only way to actually be admin is to own a real LS admin license key
+ have the Supabase server classify it via the `LS_ADMIN_VARIANT_IDS`
env. Patching the open-source client is no shortcut.

### What about `LS_ADMIN_VARIANT_IDS` leaking?

If the env var leaks (e.g. via a Supabase config dump), an attacker
still needs to OWN an LS license for that variant. The variant ID is
the lookup key; the LS license is the auth token. Without both, no
admin classification.

Mitigation if the variant ID does leak: rotate by creating a new
admin variant in LS, updating `LS_ADMIN_VARIANT_IDS`, and re-issuing
admin licenses for the new variant. Old variant keys remain valid for
the original variant (which is now empty / disabled).

## CI / E2E tests

Tests that need the admin tier use a real LS test-mode admin license
key, stored in the GitHub Actions secret `LS_ADMIN_TEST_LICENSE`.
Tests run against the live validate-tier endpoint (which has a
test-mode that recognizes test keys).

Dev/test infrastructure uses real LS — no parallel auth path. This
simplifies the security model: there's exactly one path to admin
(LS license → validate-tier classifies as admin), exercised in
production AND tests.

## Relationship to the Tier type

`VCThelpers/license/validator.py` declares:

```python
Tier = Literal["free", "pro", "mao", "enterprise", "admin"]
TIER_ORDER = {"free": 0, "pro": 1, "mao": 2, "enterprise": 3, "admin": 4}
```

`is_admin() -> bool` is the canonical helper (returns
`get_tier() == "admin"`). It does NOT consult any env var, file, or
signing key — only the cached server response.

`launcher/src-tauri/src/db/migrations/005_tier_cache_admin.sql` extends
the SQLite CHECK constraint on the `tier_cache` table to allow
`'admin'` so the validator's response can be persisted.
