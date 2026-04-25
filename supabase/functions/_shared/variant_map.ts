// Lemon Squeezy variant_id → app/tier mapping
//
// Shared between lemon-squeezy-webhook and validate-tier edge functions.
// Update VARIANT_MAP when products are created in the LS dashboard.
//
// Format:
//   "<variant_id>": { appId: "<app-slug>", tier?: "pro" | "mao" | "enterprise" }
//
// - appId is added to profiles.apps for any product (existing behavior).
// - tier is set on profiles.orchestrator_tier ONLY when appId === "orchestrator".

export type OrchestratorTier = "free" | "pro" | "mao" | "enterprise";

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
 * Returns undefined if the variant is unknown (caller should log + 400).
 */
export function lookupVariant(variantId: string): VariantMapping | undefined {
  return VARIANT_MAP[variantId];
}
