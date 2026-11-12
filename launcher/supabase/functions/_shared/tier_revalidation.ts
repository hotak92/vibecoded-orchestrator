// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared server-to-server tier re-validation for VCO Supabase edge
// functions.
//
// v0.2.54 Track H (H-7): this function existed as THREE byte-identical
// copies in rl-artifact-url, rl-latest-version, and rl-latest-weights.
// A security fix to the re-validation path (timeout policy, error
// detail redaction, auth header handling) needed three applications and
// could silently drift. The old per-copy comment claimed Supabase
// functions "don't have a cross-function module import path beyond the
// per-function _shared dir" — which is exactly what `_shared/` is for:
// the deploy bundler vendors `_shared/` imports into each function, so
// every function stays independently deployable (rl-artifact-url has
// imported `_shared/config.ts` since v0.2.36).
//
// Security posture (unchanged from the copies): we trust /validate-tier
// as the single source of truth for tier mapping. The launcher could
// pass a stale or forged local cache, so callers MUST re-validate
// server-side via this helper before issuing any credential (registry
// pull token, signed Storage URL). Fail closed: every error path
// returns `{ valid: false, tier: "free" }` with a bounded `reason`
// string (≤200 chars of upstream detail) for the function log.

import { type OrchestratorTier } from "./variant_map.ts";

/** The two request-body fields the re-validation consumes. Each caller's
 *  own `RequestBody` (validated in its `validation.ts`) is structurally
 *  assignable to this. */
export interface TierRevalidationInput {
  license_key: string;
  machine_id_hash: string;
}

export interface TierRevalidationResult {
  valid: boolean;
  tier: OrchestratorTier;
  reason?: string;
}

/**
 * Re-validate the license via the project's own `/validate-tier` edge
 * function (server-to-server, authorized with the service-role key).
 * Calling it directly avoids duplicating the Lemon Squeezy logic here.
 */
export async function revalidateTierViaSupabase(
  body: TierRevalidationInput,
): Promise<TierRevalidationResult> {
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return { valid: false, tier: "free", reason: "service_misconfigured" };
  }

  const url = `${supabaseUrl}/functions/v1/validate-tier`;
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Service-role key authorizes us to call validate-tier
        // server-side without needing a publishable anon key.
        Authorization: `Bearer ${serviceRoleKey}`,
      },
      body: JSON.stringify({
        license_key: body.license_key,
        machine_id_hash: body.machine_id_hash,
      }),
      signal: AbortSignal.timeout(8000),
    });
  } catch (e) {
    return {
      valid: false,
      tier: "free",
      reason: `validate-tier_unreachable: ${String(e).slice(0, 200)}`,
    };
  }

  if (!resp.ok) {
    return {
      valid: false,
      tier: "free",
      reason: `validate-tier_${resp.status}`,
    };
  }

  let parsed: { valid?: boolean; tier?: OrchestratorTier };
  try {
    parsed = await resp.json();
  } catch (e) {
    return {
      valid: false,
      tier: "free",
      reason: `validate-tier_parse: ${String(e).slice(0, 200)}`,
    };
  }

  if (!parsed.valid || !parsed.tier) {
    return { valid: false, tier: "free", reason: "validate-tier_rejected" };
  }
  return { valid: true, tier: parsed.tier };
}
