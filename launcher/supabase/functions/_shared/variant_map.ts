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
// Bug 33: the `admin` tier is a server-side classification. Admin
// variant IDs are NOT listed in this file (which ships in the public
// AGPL source). They live in the Supabase env var
// `LS_ADMIN_VARIANT_IDS` (JSON array of strings) and are resolved at
// request time by `isAdminVariant`. Open-source readers can see the
// type and the resolution function, but cannot derive the variant ID
// or fabricate an admin classification by patching this file —
// `isAdminVariant` consults the runtime env, not source data.

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
  // Replace the *_TODO keys with the real variant_id from each LS product URL.
  "ORCHESTRATOR_PRO_MONTHLY_TODO":  { appId: "orchestrator", tier: "pro" },
  "ORCHESTRATOR_PRO_ANNUAL_TODO":   { appId: "orchestrator", tier: "pro" },
  "ORCHESTRATOR_PRO_LIFETIME_TODO": { appId: "orchestrator", tier: "pro" },
  "MAO_MONTHLY_TODO":               { appId: "orchestrator", tier: "mao" },
  "MAO_ANNUAL_TODO":                { appId: "orchestrator", tier: "mao" },
  "MAO_LIFETIME_TODO":              { appId: "orchestrator", tier: "mao" },
};

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
