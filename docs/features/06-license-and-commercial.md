# Licensing, Tier Model & Commercial Integration

How `vibecoded-orchestrator` distinguishes free from paid usage, enforces entitlements, integrates with Lemon Squeezy for payment, collects opt-in anonymous telemetry, and satisfies licensing and contribution legal obligations. Python validator at `VCThelpers/license/`; server-side tier classification at `launcher/supabase/functions/validate-tier/`; telemetry at `VCThelpers/telemetry/`.

---

## Tier Model

The orchestrator runs in one of five tiers (`free` < `pro` < `mao` < `enterprise` < `admin`). Higher tiers inherit lower-tier capabilities; gating is done with a single `feature_enabled(name)` API rather than scattered `if tier == "pro"` checks. Free tier is the fail-open default — any network or validation error degrades back to it without blocking startup.

### Five-tier hierarchy: free / pro / mao / enterprise / admin
Tiers form a strict ordering (`TIER_ORDER` in `VCThelpers/license/validator.py`): free=0, pro=1, mao=2, enterprise=3, admin=4. `require_tier(min_tier)` evaluates as `TIER_ORDER[current] >= TIER_ORDER[min_tier]`, so every higher tier inherits lower-tier features.

### Free tier (always available, no key required)
Unlocks: `knowledge_graph`, `code_graph`, `hooks`, `hybrid_search`. No license key, no network call, no expiry. Fail-open design means any network or validation failure falls back here.

### Pro tier
Adds: `rl_retrieval`, `auto_update`, `curated_agent_packs`. Requires a valid Lemon Squeezy license key mapped to a Pro variant. (`watermark_disabled` was removed in v0.2.54 — the watermark feature never shipped a consumer.)

### MAO tier
Adds: `multi_agent_orchestration`. Requires an MAO-tier LS license. Intended for teams running the full multi-agent orchestrator stack.

### Enterprise tier
Adds: `soc2_compliance`, `priority_support`. Superset of MAO.

### Admin tier
Server-only tier; strict superset of enterprise. Two server-authoritative classification paths exist (see [§Admin License](#admin-license) below):
- **Path A — Vault-token admin (recommended)**: token shape `vct_admin_<URL-safe-base64>` resolved against the `vct_admin_tokens` Supabase Vault secret. No LS product required.
- **Path B — Lemon Squeezy admin variant (legacy)**: a real LS license whose `variant_id` appears in the `LS_ADMIN_VARIANT_IDS` env var.

Both paths yield identical client-side flags (`is_admin`, `unlock_all_modules`, `dev_features_enabled`) and unlock the same dev affordances in the launcher (ADMIN badge, Admin sidebar, `private-test` catalog modules). Neither bypasses server-gated capabilities — a self-claimed admin tier on a patched client yields nothing the AGPL source doesn't already.

<details>
<summary>Details</summary>

`TIER_FEATURES` in `validator.py` is the single source of truth for which feature string maps to which minimum tier. Adding a new feature gate = one dict entry. Tiers not in `TIER_ORDER` are coerced to `free` with a warning log, so unknown values from a future server can never grant elevated access.

</details>

### Feature gate API: `feature_enabled(feature: str) -> bool`
Returns True if the current tier satisfies the feature's minimum. Unknown feature names default to True (fail-open for ungated features). `VCThelpers/license/validator.py`.

### `require_tier(min_tier: Tier) -> bool`
Low-level gate; returns True iff `TIER_ORDER[current] >= TIER_ORDER[min_tier]`.

---

## License Validator (Python client, `VCThelpers/license/validator.py`)

The Python client's job is narrow: find a license key (env var, file, argument), POST it to the Supabase edge function, cache the verdict, and answer `feature_enabled()` queries against it. Anything more interesting — the actual classification, the LS API calls, the Vault lookups — happens server-side.

### Key resolution priority
`validate_license()` resolves the license key in order: (1) `VIBECODED_TIER=free` env forces free tier immediately; (2) explicit `key` argument; (3) `VIBECODED_LICENSE_KEY` env var; (4) `~/.vct-secrets/shared/license_key` file (preferred) or legacy `~/.vct-secrets/license_key` (fallback). First non-empty value wins. No key → free tier.

### Remote validation via Supabase edge function
`_remote_validate()` POSTs `{license_key, machine_id_hash}` to the configured `validate-tier` endpoint (`VIBECODED_LICENSE_URL` / `VCT_VALIDATE_TIER_URL` env overrides; both honored for compatibility with the Rust launcher).

<details>
<summary>Details</summary>

The function call sequence: LS `/licenses/validate` → LS `/licenses/activate` (machine binding) → variant_id→tier mapping → signed response. The Python layer only calls the Supabase function; it never calls LS directly.

</details>

### 3-day offline grace period
If the remote call fails (network error, 5xx, timeout), the validator falls back to the local cache. Cache age is checked: within 3 days → return cached tier with "X days grace remaining" message; beyond 3 days → degrade to free with a clear message written to `~/.vibecoded/license_status.txt`. Nothing blocks startup.

### `~/.vibecoded/license_cache.json` — validation cache
`LicenseResult` is JSON-serialized here on every successful remote validation. Contains: `tier`, `valid`, `expires_at` (ISO 8601 or null for lifetime), `last_validated_at` (epoch seconds), `message`.

### `~/.vibecoded/license_status.txt` — human-readable status
Plain-text file updated on every validation attempt (success or fallback). Consumed by CLI diagnostics and launcher introspection.

### `license_status() -> dict` — non-blocking introspection
Returns `{tier, has_key, key_source, cached, cache_age_days, in_grace_period, status_message}` without triggering a network call. Used by the CLI and launcher status screen.

### `get_tier(force_refresh=False) -> Tier` — process-level cache
Caches the validated tier for the process lifetime in `_cached_tier`. Pass `force_refresh=True` to re-run full validation (e.g. after the user activates a key in the launcher).

### `is_admin() -> bool`
Returns `get_tier() == "admin"`. Cached via `get_tier()`. Does not consult any env var or signing key on the client — the admin classification is made server-side (by either the Vault-token Path A or the LS-variant Path B in `validate-tier`) and the client only sees the resulting cached `tier="admin"` response.

### Machine ID hash — one-way, never raw hardware
`_machine_id_hash()` returns `sha256(<platform-stable-host-id>)` as a 64-char lowercase hex string. The host-id source is OS-dependent: Windows reads `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`; macOS reads `IOPlatformUUID` via `ioreg -rd1 -c IOPlatformExpertDevice`; Linux reads `/etc/machine-id` (fallback `/var/lib/dbus/machine-id`). Only the hash is sent to the validation endpoint; the raw host id never leaves the process. The Rust launcher mirrors the algorithm in `commands/licensing.rs::machine_id_hash`. Same hash is used in telemetry. Test override: `VCT_MACHINE_ID_OVERRIDE=<string>` pins the input across OSes (production code MUST NOT set it). Algorithm switched from MAC-based (`uuid.getnode()`) to platform-stable in v0.2.36 — see `docs/license/MACHINE_BINDING.md` for the rationale (laptops with shifting NICs broke the MAC-based binding).

### `VIBECODED_TIER=free` — dev override
Setting this env var to `"free"` short-circuits all validation and returns `LicenseResult(tier="free", valid=True)` immediately. Values other than `"free"` are ignored (no env-var-claimed paid tiers without a validated key).

### CLI diagnostic
`python -m VCThelpers.license.validator` prints tier, validity, message, and per-feature gate status.

---

## validate-tier Edge Function (server-side)

This is the trust root of the licensing system. The client sends a license key + a sha256 of its platform-stable host id (Windows MachineGuid / macOS IOPlatformUUID / Linux /etc/machine-id — v0.2.36+); the function validates, optionally machine-binds, and returns a tier. The LS API key never leaves the function. Two classification paths run inside it: Path A (Vault-token admin) short-circuits on tokens shaped `vct_admin_*` and never touches LS; Path B (UUID-shaped LS license) goes through the full validate→activate LS round-trip.

### Supabase Deno edge function
`launcher/supabase/functions/validate-tier/index.ts`. Accepts `POST {license_key, machine_id_hash}`. All logic runs server-side; the LS API key never leaves the edge function.

### Input validation
Three validators in `validate-tier/index.ts`: `isValidLicenseKey` checks 36-char UUID format (Path B / LS license keys); `isValidVaultAdminToken` checks `vct_admin_<URL-safe-base64>` shape, length 30–256, regex `^vct_admin_[A-Za-z0-9_-]+$` (Path A / Vault-token admin); `isValidMachineHash` checks 64-char lowercase hex (required for both paths). `machine_id_hash` is validated first; malformed requests return 400 with `{valid: false, tier: "free"}` immediately.

### `maskKey(key)` — never log the full license key
Logs only the first 8 chars + ellipsis, consistent across all log lines.

### `fetchWithTimeout` — 8s timeout on all LS calls
Wraps `fetch` with `AbortController`. Applied to both the validate and activate LS calls.

### Two-step LS flow: validate → activate (Path B only)
Step 1: `lsValidate` — confirms the license key is valid and active. Step 2: `lsActivate` — binds the `machine_id_hash` as an instance name using the server-side LS API key. Re-activating the same instance_name is idempotent. Path A (Vault-token admin) skips both LS calls entirely; the LS API key is therefore not required for Path-A-only deployments (the function only hard-fails on a missing `LEMON_SQUEEZY_API_KEY` once a UUID-shaped license key reaches the LS branch).

### Machine-limit handling (422 from LS activate)
Returns `{valid: false, tier: "free", error: "instance_limit", message: "...Deactivate old instance at vibecodedtools.it/account"}` with HTTP 200.

### `LEMON_SQUEEZY_API_KEY` hard-fail (Path B only)
Checked at the top of the LS-license branch (after Path A has had a chance to short-circuit on `vct_admin_*` tokens). If missing, the function returns 500 `{"error": "Service misconfigured"}` and logs `FATAL`. Never silently falls through to free tier. Path A also hard-fails on missing `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` (required for the Vault RPC and audit-log insert) with the same 500 response — so misconfiguration is loud whichever path the request lands in.

### Admin extras in response
When `tier === "admin"`, the response body includes `{is_admin: true, unlock_all_modules: true, dev_features_enabled: true}`. The Path-A response additionally carries `admin_user: "<username>"` (resolved from the Vault map) so the client can display "Logged in as admin: <username>" in the UI; Path B has no per-license username concept and omits this field.

### CORS headers — wildcard origin
`Access-Control-Allow-Origin: *` with `Access-Control-Max-Age: 86400`. Safe because authentication is via the license key body, not cookies.

---

## Variant Map & Admin Classification

### `VARIANT_MAP` — static LS variant_id → app/tier mapping
`launcher/supabase/functions/_shared/variant_map.ts`. Maps LS `variant_id` strings to `{appId, tier?}`. Shared between `validate-tier` and `lemon-squeezy-webhook`. Retail variant IDs are filled in when LS products are created in the dashboard.

### `lookupVariant(variantId)` — resolution with admin override (Path B)
Resolution order: (1) `isAdminVariant(variantId)` → returns `{appId: "orchestrator", tier: "admin"}` if match; (2) `VARIANT_MAP[variantId]`. Returns `undefined` for unknown variants. This function is invoked only after the LS validate→activate round-trip in Path B; Path A (Vault-token admin) doesn't use the variant map at all.

### `isAdminVariant(variantId)` — runtime env check, fail-closed
Reads `LS_ADMIN_VARIANT_IDS` (JSON array of strings) from `Deno.env`. Returns false on any parse error or missing env (fail-closed). Admin variant IDs are NOT in the public AGPL source. A `__VCT_ADMIN_VARIANT_IDS__` test seam on `globalThis` lets unit tests inject a synthetic list without touching `Deno.env`.

### `appId` vs `tier` separation
`appId` is always set (controls which app entry appears in `profiles.apps`). `tier` is only set when `appId === "orchestrator"`. This allows non-orchestrator LS products (Transcrypt, Arzillibus, etc.) to use the same webhook without polluting the orchestrator tier.

### Vault-token admin helpers in `_shared/variant_map.ts`
Five helpers added in commit `772508d` support the Path A flow alongside the LS-variant code in the same file: `lookupVaultAdminToken` (pure function: takes the submitted key + machine hash + decrypted vault JSON, returns `{user, outcome, bind_machine_hash?}` or `null`), `constantTimeEq` (XOR-loop string equality used by `lookupVaultAdminToken` to defeat timing oracles against the Vault map), `fetchVaultAdminTokensJson` (POSTs to `/rest/v1/rpc/get_vault_admin_tokens` with the service-role key), `bindVaultAdminMachine` (POSTs to `/rest/v1/rpc/bind_vault_admin_machine` for TOFU first-bind), and `appendAdminAuthLog` (PostgREST insert into `admin_auth_log`; failure is silent and non-blocking — audit-log gaps are preferred over login failures).

### `VaultAdminTokenRecord` schema
TypeScript shape of one entry in the `vct_admin_tokens` map: `{ token: string; expires_at: string | null; machine_id_hash: string | null }`. Top-level map is `Record<username, VaultAdminTokenRecord>`. Defined in `_shared/variant_map.ts`. The schema is open-source-safe: readers see the structure but cannot derive token values (high-entropy, encrypted in Supabase Vault).

---

## Admin License

Two server-authoritative paths classify a request as admin tier. Both yield the same wire response (`tier="admin"` plus `is_admin`/`unlock_all_modules`/`dev_features_enabled` flags) and unlock the same client-side dev affordances.

The TL;DR of the architecture: an earlier draft used a local `MAINTAINER_TOKEN` + Ed25519 client check. That was dropped because a one-line patch to the AGPL client defeated it. Both Path A and Path B fix this by classifying server-side — patching the client only suppresses the visual ADMIN badge, it doesn't unlock anything the server gates. Authoritative reference: [`docs/ADMIN_LICENSE.md`](../ADMIN_LICENSE.md).

### Two paths to admin tier
**Path A — Vault-token admin (recommended for maintainer / small-team use).** High-entropy `vct_admin_<URL-safe-base64>` token resolved against the `vct_admin_tokens` Supabase Vault secret. No LS product required. Per-token leak containment via TOFU machine binding + optional expiration. Adding/revoking team members = a single SQL statement.

**Path B — Lemon Squeezy admin variant (legacy).** Real LS license keyed to a hidden variant whose `variant_id` lives in the `LS_ADMIN_VARIANT_IDS` env var. Useful for per-license LS-dashboard revocability (e.g. issuing admin to a contractor on a dated subscription). Currently parked pending tax/legal LS product setup.

`docs/ADMIN_LICENSE.md` notes that an earlier draft mentioning a local `MAINTAINER_TOKEN` / Ed25519 bypass was dropped — neither Path A nor Path B is bypassable by patching the AGPL client; classification is server-side in `validate-tier`.

### Path A: Vault secret format (`vct_admin_tokens`)
JSON map of `{username: VaultAdminTokenRecord}` stored in Supabase Vault (encrypted at rest by Supabase KMS). Each record: `{token: "vct_admin_<48-byte-url-safe-base64>", expires_at: ISO-8601 | null, machine_id_hash: sha256-hex | null}`. NULL `expires_at` means no expiration; NULL `machine_id_hash` means TOFU-pending — first successful auth from any machine writes the hash back, and subsequent auths from a different machine are rejected.

### Path A: token shape and pre-check
Tokens carry the `vct_admin_` prefix + URL-safe base64 body, length 30–256 chars. The validate-tier function checks the prefix first (cheap pre-check); UUID-shaped LS keys lack the prefix and fall through to Path B without touching the Vault map.

### Path A: `validate-tier` flow
Path A precedes Path B in `validate-tier/index.ts:227-314`. On a `vct_admin_*` shape: (1) fetch decrypted Vault map via `fetchVaultAdminTokensJson`; (2) `lookupVaultAdminToken` (constant-time scan, expiration check, machine binding check); (3) on `outcome="success"` with `bind_machine_hash` set, call `bindVaultAdminMachine` (TOFU first-bind via SECURITY DEFINER RPC); (4) `appendAdminAuthLog` for every outcome (success / expired / machine_mismatch); (5) return `{tier:"admin", is_admin, unlock_all_modules, dev_features_enabled, admin_user, expires_at: null}`. On `outcome="expired"` or `"machine_mismatch"`: 401 with a clear message. On null (token not found): 401 — does NOT fall through to Path B (LS keys can't have `vct_admin_` shape).

### Path A: SECURITY DEFINER RPCs
Two server-side functions defined in `20260427_admin_auth_log.sql`, owned by `postgres`, executable only by `service_role`:
- `public.get_vault_admin_tokens()` — returns the decrypted contents of `vct_admin_tokens` from `vault.decrypted_secrets`. Edge functions cannot query the Vault view directly; this RPC is the standard Supabase escape hatch.
- `public.bind_vault_admin_machine(p_user, p_machine_id_hash)` — TOFU writer: sets `machine_id_hash` for `p_user` IFF the current binding is NULL (never overwrites an existing bind). Returns `BOOLEAN`. Updates `vault.secrets` via `vault.update_secret()`. Rebinding requires explicit SQL by the project owner.

Both RPCs `REVOKE ALL FROM PUBLIC, anon, authenticated` and `GRANT EXECUTE TO service_role` — open-source readers see the SQL but cannot call them from a client.

### `admin_auth_log` table
`launcher/supabase/migrations/20260427_admin_auth_log.sql`. Columns: `id BIGSERIAL`, `admin_user TEXT NOT NULL`, `machine_id_hash TEXT NOT NULL`, `authenticated_at TIMESTAMPTZ DEFAULT now()`, `outcome TEXT CHECK IN ('success','expired','machine_mismatch')`, `ip_hash TEXT NULL`, `user_agent TEXT NULL`. Two forensic indexes: `(admin_user, authenticated_at DESC)` for per-admin auth history queries and `(machine_id_hash, authenticated_at DESC)` for "what tokens authenticated from this machine?". RLS enabled with an explicit `deny_all_to_clients` policy on `anon`/`authenticated` (USING false, WITH CHECK false) — only `service_role` (used by the edge function) bypasses RLS. Append-only; not pruned.

<details>
<summary>Details</summary>

The audit log is the forensic primitive that makes per-token leak containment practical: `SELECT admin_user, count(DISTINCT machine_id_hash) FROM admin_auth_log WHERE outcome='success' GROUP BY admin_user HAVING count(...) > 1` surfaces tokens used from more than one machine; `SELECT * FROM admin_auth_log WHERE outcome != 'success' ORDER BY id DESC LIMIT 20` surfaces machine-mismatch rejections (the leak signal). Failure to insert is non-blocking by design — the auth itself succeeds even if the log write fails — because audit-log gaps are preferable to login failures.

</details>

### Path A: setup runbook (project owner, one-time)
(1) Apply migration `20260427_admin_auth_log.sql`. (2) Generate first token: `TOKEN="vct_admin_$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"`. (3) Create the Vault secret in the Supabase SQL editor: `SELECT vault.create_secret('{"<user>":{"token":"<TOKEN>","expires_at":null,"machine_id_hash":null}}', 'vct_admin_tokens', '...');`. (4) `supabase functions deploy validate-tier --project-ref <ref>`. (5) Activate in launcher Settings → License. Full SQL snippets in `docs/ADMIN_LICENSE.md` §Setup.

### Path A: adding/revoking team members
Adding: project owner generates a new token client-side and `UPDATE vault.secrets SET secret = (decrypted_secret::jsonb || jsonb_build_object('<user>', jsonb_build_object('token', '<TOKEN>', 'expires_at', NULL, 'machine_id_hash', NULL)))::text WHERE name='vct_admin_tokens';`. Token is sent to the teammate via Signal / 1Password share / encrypted email — never via plaintext channels. Revoking: single SQL statement `UPDATE vault.secrets SET secret = (decrypted_secret::jsonb - '<user>')::text WHERE name='vct_admin_tokens';` — effective on next `license_refresh` (~24h cache TTL or on-demand from Settings). Rebinding (e.g. after laptop replacement): `jsonb_set(..., '{<user>,machine_id_hash}', 'null'::jsonb)`. Token rotation after suspected leak: replace `{<user>,token}` and reset `{<user>,machine_id_hash}` in one statement. All four runbooks documented in `docs/ADMIN_LICENSE.md`.

### Path A: TOFU machine binding
On first successful auth, `lookupVaultAdminToken` returns `bind_machine_hash` set to the requesting machine's hash (v0.2.36+: sha256 of the platform-stable host id; pre-v0.2.36: sha256 of the MAC); the edge function then calls `bindVaultAdminMachine` to write it back into the Vault map. Subsequent auths from a different machine return `outcome="machine_mismatch"` (401 + audit log entry). Trade-off: legitimate machine changes require a rebind — either via SQL by the project owner, or via the launcher's **Settings → License → Rebind to this machine** button (Agent S, v0.2.36) which authenticates with the current token and calls the `/rebind-admin-token` edge function. The v0.2.36 algorithm change is also the rebind path for admins upgrading from MAC-based binding: existing entries' bound hashes are stale on upgrade, the rebind button writes the new platform-stable hash. Threat model: a leaked token can't be used from another machine after first activation.

### Path A: constant-time comparison
`constantTimeEq(a, b)` in `_shared/variant_map.ts` — XOR-loop string equality that returns true iff `a === b` without leaking length (it short-circuits only on the length check, so the timing channel is "equal vs not equal", not "matched first N chars"). Used by `lookupVaultAdminToken` for token comparison and machine-hash comparison. Replacing it with native `===` would open a timing oracle attack against the Vault map.

### Path A: Deno test suite (`_shared/variant_map_test.ts`)
11 Vault-admin tests (alongside 7 Bug-33 LS-variant tests) covering: missing prefix → null short-circuit, null/malformed/non-object Vault JSON, token-not-in-map, TOFU bind on unbound user, bound user matching/mismatching machine, expired token (regardless of machine), future expiration, malformed entry skipped, top-level-array defensive case. Plus `constantTimeEq` equal/different cases. Run with `deno test --no-check launcher/supabase/functions/_shared/variant_map_test.ts` (no Supabase project required — purely pure-function tests).

### Path B: setup — one-time maintainer flow
(Path B is currently parked pending tax/legal LS product setup; use Path A for now.) (1) Create LS product/variant "Admin / Maintainer" at $0. (2) Set `LS_ADMIN_VARIANT_IDS='["<variant_id>"]'` via `supabase secrets set`. (3) Issue complimentary/test license keys per teammate. Steps documented in `docs/ADMIN_LICENSE.md` §"Path B: Setup".

### Path B: revocation
Disable the LS license in the LS dashboard. The validator re-checks within 24 hours (cache TTL). On next refresh, tier drops to `free`; ADMIN badge disappears, admin routes 404 server-side. To force immediate refresh, run `license_refresh` from the launcher's Settings panel.

### `verify_jwt = false` for `validate-tier` and `lemon-squeezy-webhook`
`launcher/supabase/config.toml` (added in commit `772508d`) sets `verify_jwt = false` for these two functions. This is a deliberate architectural choice — both endpoints authenticate via the request body (license key / Vault admin token for `validate-tier`; HMAC-SHA256 signature for `lemon-squeezy-webhook`), not via the JWT. Adding a JWT requirement on top would force the launcher to obtain a Supabase anon key just to validate a license, and `sb_publishable_*` keys aren't accepted by `verify_jwt` edge functions. The auth boundary is at the body level, not the JWT level. → See also [07-architecture.md](07-architecture.md#admin-tier--two-server-authoritative-paths-no-local-bypass) for the trust boundary.

### CI test with real LS test-mode admin license (Path B)
`LS_ADMIN_TEST_LICENSE` GitHub Actions secret holds a real LS test-mode admin key. Tests run against the live `validate-tier` endpoint (test-mode recognizes test keys). One code path, exercised in both prod and CI. Path A is exercised by the pure-function Deno test suite (`variant_map_test.ts`) — no live Supabase project required because lookup is pure given the JSON input.

### Tier Cache (Launcher)
`launcher/src-tauri/vct-launcher-core/src/db/migrations/005_tier_cache_admin.sql` extends the SQLite CHECK constraint on `tier_cache.tier` to include `'admin'` so the Rust launcher can persist admin tier responses from either path (the wire response is identical). → See also [01-launcher.md](01-launcher.md#tier-cache-offline-safe-3-day-grace).

---

## Lemon Squeezy Webhook

The `validate-tier` function is the read path; the `lemon-squeezy-webhook` function is the write path. LS calls it on `order_created` and subscription lifecycle events; the webhook updates `profiles.apps` and `profiles.orchestrator_tier` server-side (RLS prevents the client from doing it). HMAC-SHA256 signature verification is mandatory — a missing or invalid signature returns 401.

### `lemon-squeezy-webhook` edge function
`launcher/supabase/functions/lemon-squeezy-webhook/index.ts`. Handles purchase and subscription lifecycle events. Grants app access in `profiles.apps` and sets `profiles.orchestrator_tier`.

### HMAC-SHA256 signature verification — mandatory, hard-fail
`verifySignature` uses Web Crypto API (no external deps). `LEMON_SQUEEZY_WEBHOOK_SECRET` must be set; missing → 500 `"Webhook misconfigured"`. Invalid signature → 401. Constant-time comparison via `timingSafeEqual`.

### Handled event types
`order_created`, `subscription_cancelled`, `subscription_expired`, `subscription_payment_failed`, `order_refunded`. All other event types return 200 with `Ignored event:` message.

### `activateAppForUser` — idempotent entitlement grant
Adds `appId` to `profiles.apps` array (array_append, idempotent) and sets `profiles.orchestrator_tier` if `appId === "orchestrator"`. Service role bypasses RLS.

---

## Supabase Schema & RLS

### `profiles` table — entitlement store
Columns: `id` (UUID FK → `auth.users`), `name`, `apps` (text array), `orchestrator_tier`. Five Supabase migrations as of v0.1.0: `20260418_profiles_schema.sql` (table + auto-create trigger), `20260418_orchestrator_tier.sql` (tier column), `20260418_tier_ends_at.sql` (subscription expiry tracking), `20260418_license_rls.sql` (RLS policy locking `apps`/`orchestrator_tier` to service-role writes), `20260427_admin_auth_log.sql` (Vault-token Path A: `admin_auth_log` table + `get_vault_admin_tokens` / `bind_vault_admin_machine` SECURITY DEFINER RPCs).

### RLS policy — `apps` and `orchestrator_tier` client-immutable
`20260418_license_rls.sql`: the `update_own_name` policy uses a `WITH CHECK` clause that rejects any client write that changes `apps` or `orchestrator_tier`. Only the service_role key (used by the webhook edge function) can write those columns.

<details>
<summary>Details</summary>

Without this policy, a browser client could call `supabase.from('profiles').upsert({apps: ['mao', 'orchestrator']})` and self-grant paid tiers. The RLS policy closes this; the old permissive "Users can update own profile" policy is explicitly dropped and replaced.

</details>

### `handle_new_user` trigger — server-side profile row creation
No client `INSERT` policy exists on `profiles`. Row creation is handled by a `SECURITY DEFINER` trigger on `auth.users`, so clients can never forge a profile row.

---

## Telemetry System

Default off. Opt-in by category, never global. The collection layer enforces what can and can't be sent; the schema of the local SQLite queue is the second line of defence (it stores only fields the `collect_*` helpers pass in — there's no path for a stray prompt or file path to make it into the queue). Source code, file contents, license keys, prompt text, and command outputs are explicitly excluded by both layers.

### Default-OFF policy
`VIBECODED_TELEMETRY` env var must be explicitly set to `"true"`, `"1"`, `"yes"`, or `"on"` for any upload to happen. Both `collector.telemetry_enabled()` and `uploader._disabled()` enforce this independently (defense-in-depth).

### First-launch consent prompt
`prompt_consent_if_needed()` in `VCThelpers/telemetry/consent.py`. Binary prompt: accept-all or deny-all opt-in categories. Non-interactive runs (CI, cron, piped stdin) default to always-on-only with no opt-in. Consent persisted to `~/.vibecoded/config.json` with `consent_version`.

### Opt-in categories
Four categories: `rl_data`, `routing_data`, `instinct_data`, `hardware`. Each collector checks `telemetry_enabled(category)` before enqueuing. Users can edit per-category flags directly in `~/.vibecoded/config.json`.

### Always-on baseline: `collect_session_start()`
Collects: OS name/version, Python version, orchestrator version, machine hash (sha256 of the platform-stable host id; v0.2.36+ — was sha256 of MAC pre-v0.2.36), license tier and validity status (NOT the key itself), hashed session ID.

### Opt-in: `collect_rl_retrieval()` — embedding similarity data
Collects query/node embeddings (rounded to 4 decimal places) + similarity scores + latency_ms + result count. Never any query text, code, or file paths.

### Opt-in: `collect_qlearning_routing()` — routing decision metadata
Collects task_type, chosen_agent, outcome, reward_signal, model_tier, routing_latency_ms. Callers must pass enum-like labels, not free text.

### Opt-in: `collect_instinct_event()` — tool-use behavioral data
Collects tool_name, scrubbed args summary (max 500 chars), outcome, hashed session ID, duration_ms. Args are PII-scrubbed via `_scrub_args()` before enqueue.

### Opt-in: `collect_hardware()` — weekly hardware profile
Detects CPU model/arch/cores, RAM GB and speed, NVIDIA/AMD/Apple GPU names. Cached in `~/.vibecoded/hardware.json` for 7 days. Never collects hostnames, usernames, or MAC addresses.

### PII scrubbing at collection layer
`_scrub_pii(text)` replaces: user home paths, email addresses, GitHub PATs (`ghp_*`, `github_pat_*`), OpenAI keys (`sk-*`), Anthropic keys (`sk-ant-*`), Slack tokens (`xox*`), JWTs, Bearer tokens, IPv4, IPv6.

### SQLite local queue — `~/.vibecoded/telemetry.db`
WAL mode. Schema: `events(id, event_type, payload_json, created_at, uploaded_at)`. Cap at 1000 events; overflow drops oldest uploaded rows first, then oldest pending. Thread-safe via `threading.Lock`.

### Batch uploader — `upload_pending(endpoint, batch_size, queue)`
Pulls up to 100 oldest un-uploaded events. Retries 3× with exponential backoff (1s, 4s, 16s) on network errors, 5xx, and 429. 4xx (non-429) → permanent failure, events left in queue for inspection.

### Diversion to `telemetry_pending.jsonl`
When no upload endpoint is configured, opted-in events are written to `~/.vibecoded/telemetry_pending.jsonl` (one JSON object per line) instead of POSTed. `UploadResult.error == "endpoint_pending_deployment"` signals this path.

### `VIBECODED_TELEMETRY_URL` — endpoint override
Set to any live endpoint to bypass the pre-launch diversion and post events normally. Allows staging/custom deployments without code changes.

### `dashboard.py` CLI — inspect and control local telemetry
Subcommands: `show` (table of recent events), `show --all` (include uploaded), `clear` (delete all events), `status` (consent + pending count). Plain stdlib, no dependencies.

### Telemetry never collects: source code, file paths, prompt content, KG node bodies, command outputs, or license keys
Enforced by the collection layer; the queue schema stores only what the `collect_*` helpers pass in. `docs/TELEMETRY.md` documents the guarantee.

---

## Secrets & Key Rotation

### `scripts/check-no-secrets.sh` — pre-commit blocklist guard
Scans staged files (or full tracked tree with `--all`) for blocklisted token patterns. Exits non-zero with instructions. Wire as a pre-commit hook via `ln -sf ../../scripts/check-no-secrets.sh .git/hooks/pre-commit`.

### Env scrubbing in hooks
All 31 project hooks scrub `SUPABASE_KEY`, `GITHUB_TOKEN`, `OPENAI_API_KEY`, AWS credentials, `TELEGRAM_BOT_TOKEN`, etc. before spawning subprocesses. See `SECURITY.md`.

### Supabase key rotation runbook
Secrets rotation runbook (maintainer docs): (1) Roll key in Supabase dashboard, (2) write to `~/.vct-secrets/shared/supabase_token`, (3) `supabase secrets set`, (4) update Vercel env, (5) restart local services. Old key valid ~24 h (zero-downtime window).

### GitHub PAT rotation — single file write
PAT lives at `~/.vct-secrets/shared/github_pat` (Phase 1 layout, since 2026-04-24). Legacy flat path `~/.vct-secrets/github_pat` is still honored as a fallback. Rotation = update file + chmod 600. Credential helper, search MCP wrapper, and `gh` CLI all re-read on each invocation.

### Security reporting: `security@vibecodedtools.it`
Preferred: GitHub Security Advisories at `https://github.com/hotak92/vibecoded-orchestrator/security/advisories/new`. Email as fallback for reporters who cannot use GitHub. SLA: ack 3 days, initial assessment 10 days, fix plan 30 days (high/critical) / 90 days (medium/low).

---

## AGPL-3.0 Distribution Model

### License: GNU AGPL-3.0-or-later
`LICENSE` file at repo root. SPDX identifier `AGPL-3.0-or-later` in all Python source headers. Copyleft requirement applies to network use, not just distribution.

### SPDX headers — required on all new source files
Pattern: `# SPDX-License-Identifier: AGPL-3.0-or-later` + `# Copyright (c) 2026 VibeCoded Tools`. Consistent across all Python and TypeScript files.

### Paid modules as signed pre-compiled binaries
Paid-tier features may be distributed as pre-compiled, signed binaries outside the AGPL source tree. Lemon Squeezy handles payment; the signed-URL gateway gates artifact downloads by re-validating the JWT.

---

## Dependency License Audit

### Full transitive audit — cleared for AGPL-3.0 release
`docs/DEPENDENCY_LICENSES.md`. All direct and transitive Python deps carry MIT, BSD-2/3, Apache-2.0, ISC, MPL-2.0, PSF, or LGPL (dynamically linked) licenses. No GPL-2-only entries found. Status: cleared.

### NVIDIA CUDA libs — not redistributed
15 `nvidia-*-cu12` packages are transitive deps of `torch` on GPU systems. Not bundled in the repo, installers, or Docker images. `install.py --cpu-only` avoids them entirely.

### Re-audit trigger
Re-run `pip-licenses` whenever `requirements.txt` gains a new entry or before cutting a public release.

---

## CLA (Contributor License Agreement)

### CLA version 1.0 — 2026-04-18
`CLA.md`. Individual CLA only; Corporate CLA available by arrangement at `team@vibecodedtools.com`.

### Perpetual, royalty-free copyright + patent license
Contributors grant VibeCoded Tools a perpetual, worldwide, non-exclusive, irrevocable copyright AND patent license. Covers both AGPL-3.0 distribution and the proprietary paid distribution channel.

### `Signed-off-by` trailer as acceptance mechanism
First commit in a PR must include `Signed-off-by: Full Name <email>` (compatible with `git commit -s`). Acceptance is by PR submission; no click-wrap required.

### Governing law: Italy
The CLA and any disputes arising under it are governed by Italian law. Specific venue and arbitration terms in `CLA.md`.

---

## Code of Conduct

### Contributor Covenant v2.1
`CODE_OF_CONDUCT.md`. Reports of abusive or harassing behavior go to `team@vibecodedtools.com`.

### Four-tier enforcement ladder
(1) Correction — private warning; (2) Warning — time-limited no-contact; (3) Temporary Ban; (4) Permanent Ban.

---

## Sub-processor & Infrastructure Notes

### Supabase
Hosts `profiles` table (entitlements), `validate-tier` and `lemon-squeezy-webhook` edge functions, and email functions.

### Vercel
Only the `vibecodedtools` project (hosted on the maintainers' Vercel account). `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_ANON_KEY` set as Vercel project env vars.

### IONOS
Static-only web hosting. SMTP fallback credentials stored as Supabase edge function secret (`IONOS_SMTP_PASSWORD`).

### Lemon Squeezy
License keys are LS UUIDs. Variant catalog drives tier mapping. Webhook events drive entitlement grants. API key rotated via LS dashboard → `supabase secrets set`.

### Cloudflare
Referenced in infrastructure notes as a planned CDN / DDoS layer. Not yet deployed as of v0.1.0.

### Contact addresses
- Team: `team@vibecodedtools.com`
- Security reports: `security@vibecodedtools.it`
- News/announcements: `news@vibecodedtools.com`
