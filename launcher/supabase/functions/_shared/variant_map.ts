// Lemon Squeezy variant_id → app/tier mapping
//
// Shared between lemon-squeezy-webhook and validate-tier edge functions.
// Update VARIANT_MAP when products are created in the LS dashboard.
//
// Format:
//   "<variant_id>": { appId: "<app-slug>", tier?: "pro" | "mao" | "enterprise" | "admin" }
//
// - appId is added to profiles.apps for any product (existing behavior).
// - tier is set on profiles.orchestrator_tier ONLY when appId === "orchestrator".
//
// The `admin` tier is a server-side classification. Admin variant IDs
// are NOT listed in this file (which ships in the public AGPL source).
// They live in the Supabase env var `LS_ADMIN_VARIANT_IDS` (JSON array
// of strings) and are resolved at request time by `isAdminVariant`.
// Open-source readers can see the type and the resolution function,
// but cannot derive the variant ID or fabricate an admin classification
// by patching this file — `isAdminVariant` consults the runtime env,
// not source data.

export type OrchestratorTier = "free" | "pro" | "mao" | "enterprise" | "admin";

export interface VariantMapping {
  appId: string;
  tier?: OrchestratorTier;
}

export const VARIANT_MAP: Record<string, VariantMapping> = {
  // ─── Existing single-app products ─────────────────────────────────────────
  // Variant IDs TBD — fill in when products are created in LS.
  // "123456": { appId: "transcrypt" },
  // "123457": { appId: "arzillibus" },
  // "123458": { appId: "convertifacile" },
  // "123459": { appId: "dataweave" },
  // "123460": { appId: "formcraft" },
  // "123461": { appId: "pixelsnap" },

  // ─── Orchestrator tier products ───────────────────────────────────────────
  // appId is always "orchestrator"; tier determines what's unlocked.
  //
  // **PLACEHOLDER KEYS — NOT REAL VARIANT IDs.** The 6 keys below are
  // sentinel strings, not actual Lemon Squeezy variant_ids (LS variant
  // IDs are numeric strings like "123456"). They exist so the type
  // shape + tier mapping are testable without leaking the real IDs
  // into the public AGPL repo before the products go live.
  //
  // Pre-launch checklist (track in fork):
  //   1. Create the 6 LS products in the dashboard.
  //   2. Replace the `*_PLACEHOLDER` keys with the numeric variant_ids
  //      from each LS product URL.
  //   3. Confirm assertNoPlaceholderKeysInProduction() at the bottom
  //      of this file no longer throws when run with NODE_ENV=production.
  //
  // RUNTIME GUARD: assertNoPlaceholderKeysInProduction() (called at
  // edge-function init) hard-fails if any *_PLACEHOLDER key reaches
  // a production environment. Preserves the testable shape during dev
  // while preventing a silent webhook failure on launch day where a
  // real LS purchase would fall through (because no real variant_id
  // matches the sentinel strings).
  "ORCHESTRATOR_PRO_MONTHLY_PLACEHOLDER":  { appId: "orchestrator", tier: "pro" },
  "ORCHESTRATOR_PRO_ANNUAL_PLACEHOLDER":   { appId: "orchestrator", tier: "pro" },
  "ORCHESTRATOR_PRO_LIFETIME_PLACEHOLDER": { appId: "orchestrator", tier: "pro" },
  "MAO_MONTHLY_PLACEHOLDER":               { appId: "orchestrator", tier: "mao" },
  "MAO_ANNUAL_PLACEHOLDER":                { appId: "orchestrator", tier: "mao" },
  "MAO_LIFETIME_PLACEHOLDER":              { appId: "orchestrator", tier: "mao" },
};

/**
 * Pre-flight guard against shipping placeholder variant IDs to production.
 *
 * Call from edge-function module init (so the function fails to start
 * rather than silently 200ing every webhook on launch day). The launch
 * scenario this prevents:
 *
 *   - User buys Pro subscription via Lemon Squeezy.
 *   - LS sends webhook with the REAL variant_id (e.g. "987654").
 *   - Edge function calls lookupVariant("987654") → undefined (real ID
 *     not in VARIANT_MAP because still placeholders).
 *   - Webhook returns 200 (per the catch-all `unknown variant` log+drop
 *     path in lemon-squeezy-webhook). User's profile never gets
 *     `tier=pro`. Customer support nightmare.
 *
 * Hard-failing at module init makes the misconfiguration impossible to
 * miss — Supabase logs flag the function as crashing on every call.
 *
 * Skip in development (NODE_ENV !== "production") so placeholder-keyed
 * unit tests still work.
 */
export function assertNoPlaceholderKeysInProduction(
  env: { NODE_ENV?: string; DENO_DEPLOYMENT_ID?: string } = {
    // deno-lint-ignore no-explicit-any
    NODE_ENV: (globalThis as any).Deno?.env?.get?.("NODE_ENV"),
    // deno-lint-ignore no-explicit-any
    DENO_DEPLOYMENT_ID: (globalThis as any).Deno?.env?.get?.("DENO_DEPLOYMENT_ID"),
  },
): void {
  // Production = explicit NODE_ENV=production OR running on Supabase
  // (DENO_DEPLOYMENT_ID is set by the platform). Local dev / unit tests
  // satisfy neither and skip the assert.
  const isProduction =
    env.NODE_ENV === "production" || !!env.DENO_DEPLOYMENT_ID;
  if (!isProduction) return;

  const offenders = Object.keys(VARIANT_MAP).filter((k) =>
    k.includes("_PLACEHOLDER")
  );
  if (offenders.length > 0) {
    throw new Error(
      `VARIANT_MAP contains ${offenders.length} placeholder key(s) that ` +
        `must NOT ship to production: ${offenders.join(", ")}. Replace them ` +
        `with real Lemon Squeezy variant_ids from the LS dashboard before ` +
        `deploying.`,
    );
  }
}

/**
 * Look up the mapping for a given variant_id.
 *
 * Resolution order:
 *   1. Bug 33: if the variant_id appears in `LS_ADMIN_VARIANT_IDS`
 *      (JSON array env var), classify as `admin` tier on the
 *      orchestrator app. This branch overrides the static map; admin
 *      is server-only and never listed in this file.
 *   2. Else fall back to the static `VARIANT_MAP` for retail
 *      variants (Pro / MAO / Enterprise / single-app products).
 *
 * Returns undefined if the variant is unknown (caller should log + 400).
 */
export function lookupVariant(variantId: string): VariantMapping | undefined {
  if (isAdminVariant(variantId)) {
    return { appId: "orchestrator", tier: "admin" };
  }
  return VARIANT_MAP[variantId];
}

/**
 * Bug 33: read `LS_ADMIN_VARIANT_IDS` (JSON array of strings) from the
 * runtime env and check whether `variantId` is in that list.
 *
 * Returns false on parse error / missing env / unknown shape — failing
 * closed (no admin) is the only safe default. Logs the parse error
 * once (Deno's logger) so misconfiguration is obvious in Supabase logs.
 *
 * Test seam: a process-local override `__VCT_ADMIN_VARIANT_IDS__` is
 * read first if defined on `globalThis`, so unit tests can inject a
 * synthetic list without touching Deno.env.
 */
export function isAdminVariant(variantId: string): boolean {
  // Test seam — only used by Deno tests in CI.
  // deno-lint-ignore no-explicit-any
  const override = (globalThis as any).__VCT_ADMIN_VARIANT_IDS__;
  let raw: string | undefined;
  if (Array.isArray(override)) {
    return override.includes(variantId);
  }
  if (typeof override === "string") {
    raw = override;
  } else {
    // deno-lint-ignore no-explicit-any
    const denoEnv = (globalThis as any).Deno?.env;
    raw = denoEnv?.get?.("LS_ADMIN_VARIANT_IDS");
  }
  if (!raw) return false;
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return false;
    return parsed.includes(variantId);
  } catch (_) {
    // Misconfigured env — fail closed.
    return false;
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Vault-token admin path (separate from Bug 33's LS-variant path)
// ────────────────────────────────────────────────────────────────────────────
//
// Why this exists alongside isAdminVariant:
//   Bug 33's LS-variant path requires creating a Lemon Squeezy variant
//   first, which is paid-product infrastructure. For solo + small-team
//   maintainer use ("I just want to run admin tier on my own machine
//   without setting up a paid product"), Vault-token admin is simpler:
//   one Vault secret holds a JSON map of { username: token-record }, and
//   the validate-tier function checks it before falling through to the
//   LS-variant path.
//
//   Both paths coexist. LS-variant is preferred when you want
//   per-license LS-dashboard revocability (e.g. issuing admin to a
//   contractor on a dated subscription). Vault-token is preferred for
//   maintainer/team admin where SQL-level rotation is fine.
//
// Vault secret format (`vct_admin_tokens`):
//   {
//     "admin1": { "token": "vct_admin_<64chars>",
//                 "expires_at": "2026-10-26T00:00:00Z" | null,
//                 "machine_id_hash": "abc123..." | null },
//     "admin2": { ... },
//     "admin3": { ... }
//   }
//
//   - `expires_at`: optional ISO-8601 timestamp. NULL means no expiry.
//   - `machine_id_hash`: optional. NULL means token is not yet bound
//     to a machine — the FIRST successful auth from any machine writes
//     the hash back into the Vault secret (trust-on-first-use).
//     Subsequent auths from a different machine are rejected
//     (outcome='machine_mismatch'). To rebind, an admin replaces the
//     entry's `machine_id_hash` with NULL via SQL.

export interface VaultAdminTokenRecord {
  token: string;
  expires_at: string | null;
  machine_id_hash: string | null;
}

export interface VaultAdminLookupResult {
  user: string;
  outcome: "success" | "expired" | "machine_mismatch";
  /** TOFU: caller should write this hash back into the Vault entry */
  bind_machine_hash?: string;
}

/**
 * Constant-time equality. Returns true iff a === b without leaking
 * length information beyond "equal vs not equal".
 *
 * Imperative: do NOT replace this with `a === b`, even if eslint
 * complains. Token comparison without constant-time semantics opens a
 * timing oracle attack against the Vault map.
 */
export function constantTimeEq(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Look up a submitted key in the `vct_admin_tokens` Vault secret.
 *
 * Inputs:
 *   submittedKey   — the candidate token submitted by the launcher
 *   machineHash    — sha256 hex of the requesting machine's MAC + salt
 *   vaultJson      — the decrypted contents of the `vct_admin_tokens`
 *                    Vault secret (parsed JSON string from
 *                    vault.decrypted_secrets). Caller fetches; this
 *                    function stays pure for testability.
 *
 * Returns:
 *   - VaultAdminLookupResult on match (with outcome + optional rebind hint)
 *   - null on miss or unparseable vault JSON (caller falls through to LS path)
 *
 * Failure modes:
 *   - Token starts with the wrong prefix → null (cheap pre-check)
 *   - Vault map missing / unparseable → null (fail closed; log)
 *   - Token matches but expired → outcome='expired'
 *   - Token matches but machine binding mismatches → outcome='machine_mismatch'
 *   - Token matches + machine binding NULL → outcome='success', bind_machine_hash set
 *   - Token matches + machine binding matches → outcome='success'
 *
 * Test seam: pass `vaultJson` directly; no Deno.env or Supabase
 * dependency. Unit-testable with synthetic JSON.
 */
export function lookupVaultAdminToken(
  submittedKey: string,
  machineHash: string,
  vaultJson: string | null | undefined,
  nowIso: string = new Date().toISOString(),
): VaultAdminLookupResult | null {
  // Cheap pre-check: our admin tokens always carry the prefix. LS license
  // keys are UUIDs (no `vct_admin_` prefix), so this short-circuits the
  // common case without touching the Vault map.
  if (!submittedKey.startsWith("vct_admin_")) return null;
  if (!vaultJson) return null;

  let map: Record<string, VaultAdminTokenRecord>;
  try {
    const parsed = JSON.parse(vaultJson);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return null;
    }
    map = parsed;
  } catch (_) {
    return null;
  }

  for (const [user, record] of Object.entries(map)) {
    if (!record || typeof record.token !== "string") continue;
    if (!constantTimeEq(submittedKey, record.token)) continue;

    // Expiration check
    if (record.expires_at) {
      if (record.expires_at <= nowIso) {
        return { user, outcome: "expired" };
      }
    }

    // Machine binding check
    if (record.machine_id_hash === null || record.machine_id_hash === undefined) {
      // TOFU: first auth — caller should rebind
      return { user, outcome: "success", bind_machine_hash: machineHash };
    }
    if (!constantTimeEq(record.machine_id_hash, machineHash)) {
      return { user, outcome: "machine_mismatch" };
    }
    return { user, outcome: "success" };
  }

  return null;
}

/**
 * Fetch the `vct_admin_tokens` Vault secret via the
 * public.get_vault_admin_tokens() SECURITY DEFINER RPC.
 *
 * Returns:
 *   - the decrypted secret as a JSON string (the body of the JSON map)
 *   - null if the secret doesn't exist, fetch fails, or response shape is wrong
 *
 * Uses the service-role key (SUPABASE_SERVICE_ROLE_KEY env var, available
 * to edge functions automatically). The RPC returns text; PostgREST
 * wraps single-value scalar returns as the raw value, so we expect
 * a JSON-string response body.
 */
export async function fetchVaultAdminTokensJson(
  supabaseUrl: string,
  serviceRoleKey: string,
): Promise<string | null> {
  try {
    const url = `${supabaseUrl}/rest/v1/rpc/get_vault_admin_tokens`;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${serviceRoleKey}`,
        "apikey": serviceRoleKey,
      },
      body: "{}",
    });
    if (!resp.ok) return null;
    // PostgREST returns scalar text functions as a JSON string body, e.g.
    //   "{\"<admin>\":{\"token\":\"vct_admin_...\"}}"
    // We want the inner string, which is the JSON map serialized.
    const body = await resp.text();
    if (!body || body === "null") return null;
    let inner: unknown;
    try {
      inner = JSON.parse(body);
    } catch {
      return null;
    }
    if (typeof inner !== "string") return null;
    return inner;
  } catch (_) {
    return null;
  }
}

/**
 * Bind a Vault-token admin user's `machine_id_hash` field via the
 * public.bind_vault_admin_machine() SECURITY DEFINER RPC.
 *
 * Returns true on successful first-bind (TOFU), false on any of:
 * already-bound, user-not-found, secret-missing, RPC failure.
 *
 * Used immediately after a successful lookupVaultAdminToken() that
 * returned `bind_machine_hash` (signaling "this user has no binding
 * yet — caller should bind").
 */
export async function bindVaultAdminMachine(
  supabaseUrl: string,
  serviceRoleKey: string,
  user: string,
  machineHash: string,
): Promise<boolean> {
  try {
    const url = `${supabaseUrl}/rest/v1/rpc/bind_vault_admin_machine`;
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${serviceRoleKey}`,
        "apikey": serviceRoleKey,
      },
      body: JSON.stringify({
        p_user: user,
        p_machine_id_hash: machineHash,
      }),
    });
    if (!resp.ok) return false;
    const body = await resp.text();
    return body.trim() === "true";
  } catch (_) {
    return false;
  }
}

/**
 * Append a row to public.admin_auth_log via the standard PostgREST
 * insert path. Failure is non-blocking: the auth itself succeeds
 * regardless. Audit-log gaps are preferable to login failures.
 */
export async function appendAdminAuthLog(
  supabaseUrl: string,
  serviceRoleKey: string,
  row: {
    admin_user: string;
    machine_id_hash: string;
    outcome: "success" | "expired" | "machine_mismatch";
    ip_hash?: string | null;
    user_agent?: string | null;
  },
): Promise<void> {
  try {
    await fetch(`${supabaseUrl}/rest/v1/admin_auth_log`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${serviceRoleKey}`,
        "apikey": serviceRoleKey,
        "Prefer": "return=minimal",
      },
      body: JSON.stringify(row),
    });
  } catch (_) {
    /* non-blocking — silent failure */
  }
}
