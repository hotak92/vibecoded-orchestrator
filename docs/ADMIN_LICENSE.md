# Admin license

Admin licenses unlock dev-only affordances in the launcher: a persistent
ADMIN badge, an Admin sidebar group with feature-flag / diagnostic /
license-test routes, and visibility on `private-test` modules in the
catalog.

There are **two paths** to admin tier, both server-authoritative:

- **Path A — Vault-token admin** (recommended for maintainer / small-team use).
  High-entropy random tokens stored in the `vct_admin_tokens` Supabase
  Vault secret. No Lemon Squeezy product required. Per-token leak
  containment via TOFU machine binding + optional expiration.
  Adding/revoking team members = a single SQL statement.
- **Path B — Lemon Squeezy admin variant** (Bug 33; legacy). Real LS
  license keys backed by a hidden variant. Useful when you want
  per-license LS-dashboard revocability (e.g. issuing admin to a
  contractor on a dated subscription). Requires LS product configuration.

Both paths share the same `tier="admin"` outcome on the wire and identical
client-side capability flags (`is_admin`, `unlock_all_modules`,
`dev_features_enabled`). The launcher and Python validator don't need to
care which path was used; only `validate-tier`'s server logic distinguishes.

This document supersedes any earlier draft mentioning a local
`MAINTAINER_TOKEN` / Ed25519 bypass — that approach was dropped because it
was too easy to defeat with a one-line client patch. Both Path A and Path B
above are server-side classifications; client-side patches accomplish nothing.

---

## Path A: Vault-token admin (recommended)

### Architecture

```
launcher / orchestrator
   │
   │  POST /functions/v1/validate-tier { license_key, machine_id_hash }
   │       (license_key = "vct_admin_<URL-safe-base64>")
   ▼
Supabase edge function (validate-tier)
   │
   ├── isValidVaultAdminToken(license_key) ?
   │     └─ fetchVaultAdminTokensJson() via SECURITY DEFINER RPC
   │     └─ lookupVaultAdminToken(key, machine_hash, vault_json)
   │           ├── prefix mismatch → null (fall through to Path B)
   │           ├── token not in map → null
   │           ├── token expired → outcome="expired"
   │           ├── machine_id_hash bound + mismatched → outcome="machine_mismatch"
   │           ├── machine_id_hash NULL (TOFU) → outcome="success", bind_machine_hash=...
   │           └── machine_id_hash matches → outcome="success"
   │     └─ on success: bindVaultAdminMachine() if TOFU
   │     └─ always: appendAdminAuthLog() with outcome
   │     └─ returns { tier="admin", admin_user="<name>", is_admin=true, ... }
   │
   └── (else fall through to Path B — see below)
```

### Vault secret format

The `vct_admin_tokens` secret holds a JSON map:

```json
{
  "martino": {
    "token": "vct_admin_<48-byte-url-safe-base64>",
    "expires_at": null,
    "machine_id_hash": null
  },
  "fabio": { "token": "vct_admin_...", "expires_at": "2026-12-31T00:00:00Z", "machine_id_hash": "abc123..." },
  "vartan": { "token": "vct_admin_...", "expires_at": null, "machine_id_hash": null }
}
```

- **`token`**: the high-entropy admin token (~74 chars total, prefix +
  64 url-safe base64 chars). Generated client-side; only the server
  hashed comparison is constant-time.
- **`expires_at`**: optional ISO-8601 UTC. NULL = no expiration. After
  expiry, lookup returns `outcome="expired"` (no admin tier).
- **`machine_id_hash`**: optional sha256 hex. NULL = not yet bound. The
  FIRST successful auth from any machine writes the hash back into the
  Vault secret (TOFU pattern). Subsequent auths from a different
  machine_id_hash are rejected.

### Setup (project owner, one-time)

1. **Apply migration** `launcher/supabase/migrations/20260427_admin_auth_log.sql`
   (creates `admin_auth_log` table, `get_vault_admin_tokens()` RPC,
   `bind_vault_admin_machine()` RPC).
2. **Generate the first token** (your own machine):
   ```bash
   TOKEN_MARTINO="vct_admin_$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
   echo -n "$TOKEN_MARTINO" > ~/.vct-secrets/shared/vct_admin_token
   chmod 600 ~/.vct-secrets/shared/vct_admin_token
   ```
3. **Create the Vault secret** in Supabase dashboard SQL editor:
   ```sql
   SELECT vault.create_secret(
     '{"martino":{"token":"<TOKEN_MARTINO>","expires_at":null,"machine_id_hash":null}}',
     'vct_admin_tokens',
     'JSON map of {username: VaultAdminTokenRecord}. See docs/ADMIN_LICENSE.md.'
   );
   ```
4. **Deploy `validate-tier`** (already wired to read the Vault secret):
   ```bash
   cd launcher && supabase functions deploy validate-tier --project-ref <project-ref>
   ```
5. **Activate** in the launcher: Settings → License → Activate → paste
   `$TOKEN_MARTINO`. ADMIN badge appears.

### Adding a team member

Generate the token on YOUR machine (not theirs — you control issuance):

```bash
TOKEN_FABIO="vct_admin_$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
echo "Send this to Fabio (via Signal / 1Password share / encrypted email):"
echo "$TOKEN_FABIO"
```

Then add to the Vault map (Supabase dashboard SQL editor):

```sql
UPDATE vault.secrets
SET secret = (
  COALESCE(decrypted_secret::jsonb, '{}'::jsonb) || jsonb_build_object(
    'fabio',
    jsonb_build_object(
      'token', '<TOKEN_FABIO>',
      'expires_at', NULL,           -- or '2027-01-01T00:00:00Z' for 9-month rotation
      'machine_id_hash', NULL       -- TOFU: locked on first auth
    )
  )
)::text
FROM vault.decrypted_secrets ds
WHERE secrets.id = ds.id AND secrets.name = 'vct_admin_tokens';
```

Verify (without printing the token value):

```sql
SELECT jsonb_object_keys(decrypted_secret::jsonb) AS users
FROM vault.decrypted_secrets WHERE name = 'vct_admin_tokens';
-- Should now list "martino" + "fabio".
```

Fabio pastes the token into his launcher's License → Activate field. The
edge function recognizes `admin_user="fabio"`, performs TOFU bind to
his machine, returns admin tier. Audit log records the bind.

### Revoking a team member

```sql
UPDATE vault.secrets
SET secret = (decrypted_secret::jsonb - 'fabio')::text
FROM vault.decrypted_secrets ds
WHERE secrets.id = ds.id AND secrets.name = 'vct_admin_tokens';
```

Effective on the next launcher refresh (~24h cache TTL or
`license_refresh` from Settings).

### Re-binding a token to a new machine (e.g. after laptop replacement)

```sql
-- Wipe the binding; next auth from any machine will re-TOFU.
UPDATE vault.secrets
SET secret = jsonb_set(
  decrypted_secret::jsonb,
  '{fabio,machine_id_hash}',
  'null'::jsonb
)::text
FROM vault.decrypted_secrets ds
WHERE secrets.id = ds.id AND secrets.name = 'vct_admin_tokens';
```

### Rotating a token (after suspected leak)

Generate a new token + replace the entry. Old token immediately invalid:

```sql
UPDATE vault.secrets
SET secret = jsonb_set(
  jsonb_set(decrypted_secret::jsonb, '{fabio,token}', to_jsonb('vct_admin_<NEW_TOKEN>'::text)),
  '{fabio,machine_id_hash}',
  'null'::jsonb
)::text
FROM vault.decrypted_secrets ds
WHERE secrets.id = ds.id AND secrets.name = 'vct_admin_tokens';
```

### Forensics: querying the audit log

```sql
-- Recent activity for a specific user
SELECT id, machine_id_hash, outcome, authenticated_at
FROM admin_auth_log
WHERE admin_user = 'fabio'
ORDER BY authenticated_at DESC LIMIT 50;

-- Anomaly detection: tokens used from > 1 machine
SELECT admin_user, count(DISTINCT machine_id_hash) AS distinct_machines
FROM admin_auth_log
WHERE outcome = 'success'
GROUP BY admin_user
HAVING count(DISTINCT machine_id_hash) > 1;

-- Recent rejections (machine_mismatch is the leak signal)
SELECT * FROM admin_auth_log WHERE outcome != 'success' ORDER BY id DESC LIMIT 20;
```

### Security model

| Capability | Tampering with the client gives the attacker… |
|---|---|
| ADMIN badge in launcher UI | …a yellow badge. Nothing else. |
| Admin sidebar group visible | …route links that 4xx server-side. |
| `private-test` modules in catalog | …a list of names. Module artifacts are still gated by signed-URL gateway, which re-validates the JWT issued by validate-tier on every download. |
| `is_admin()` returns True locally | …local feature flags lit up. Server-gated capabilities re-validate against the Supabase JWT — a self-claimed admin tier yields nothing the AGPL source doesn't already. |

Token-leak threat model:
- **Token never leaves the user's `~/.vct-secrets/`** during normal operation; the launcher reads it at activation, sends ONCE over HTTPS to validate-tier, and caches the resulting tier (not the token).
- **TOFU machine binding** ensures a leaked token can't be used from another machine after first activation. (Trade-off: legitimate machine changes require manual rebind by project owner.)
- **Audit log** records every authentication, including outcome, so unusual patterns are detectable.
- **Constant-time comparison** in `lookupVaultAdminToken` prevents timing oracle attacks against the Vault map.
- **High entropy** (48 random bytes = 256 bits) makes brute force infeasible.

### Why this is open-source-safe

Open-source readers see:
- The lookup function (`lookupVaultAdminToken`) and its branches
- The Vault secret name (`vct_admin_tokens`)
- The schema of records (`token`, `expires_at`, `machine_id_hash`)

But CANNOT derive any actual token value, because tokens are
high-entropy random and live only in the Vault (encrypted at rest by
Supabase KMS) + the holder's local secrets dir. Reading the source
provides zero attack surface.

---

## Path B: Lemon Squeezy admin variant (Bug 33; legacy)

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

### Setup (Path B; maintainer, one-time)

> **Note**: Path B is currently parked pending tax/legal setup with
> commercialista (see `.claude/context/parked/lemon-squeezy-setup-TODO.md`
> in the Claude orchestrator repo). Use Path A for now; come back here
> when LS products are ready or you need per-license LS-dashboard
> revocability for contractors.

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
