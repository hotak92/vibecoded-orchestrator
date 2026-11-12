// Pure validation + tier-check helpers for rl-artifact-url.
//
// Split out from index.ts so they can be tested without spinning up a
// Deno server or mocking Deno.serve / Deno.env. The Deno test harness
// runs these directly via `deno test`.
//
// No I/O, no Deno globals — pure functions only.

import type { OrchestratorTier } from "../_shared/variant_map.ts";

// License key UUID regex — paid-tier customers (Lemon Squeezy).
export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Vault-admin token regex — Path A admin licenses (`vct_admin_` prefix +
// URL-safe base64 body). Mirrors the same check in
// `validate-tier/index.ts::isValidVaultAdminToken`. Discovered missing
// 2026-05-26 during v0.2.35 post-update validation: an admin-tier user reached the
// pull-token gateway with their `vct_admin_*` token and got `400
// license_key_invalid_format` because this validator was UUID-only.
// validate-tier accepts both shapes; rl-artifact-url MUST also accept
// both so the same license credential works through both endpoints.
export const VAULT_ADMIN_TOKEN_RE = /^vct_admin_[A-Za-z0-9_-]+$/;

/** Accepts either a Lemon Squeezy UUID or a Vault-admin `vct_admin_*` token. */
export function isValidLicenseKeyShape(s: string): boolean {
  return UUID_RE.test(s) || VAULT_ADMIN_TOKEN_RE.test(s);
}

// Minimum machine-id-hash length (SHA-256 hex would be 64; we accept
// ≥16 to allow for shorter dev hashes during testing).
export const MACHINE_ID_HASH_MIN_LEN = 16;

export const TIER_RANK: Record<OrchestratorTier, number> = {
  free: 0,
  pro: 1,
  mao: 2,
  enterprise: 3,
  admin: 4,
};

// Tier required to pull the RL Reranker image.
export const REQUIRED_TIER: OrchestratorTier = "pro";

export interface RequestBody {
  license_key: string;
  machine_id_hash: string;
}

export type ValidationError =
  | "license_key_invalid_format"
  | "machine_id_hash_invalid_format";

/** Validate the request body shape. Returns null on success, error code on failure. */
export function validateRequestBody(body: unknown): ValidationError | null {
  if (typeof body !== "object" || body === null) {
    return "license_key_invalid_format";
  }
  const b = body as Partial<RequestBody>;
  if (typeof b.license_key !== "string" || !isValidLicenseKeyShape(b.license_key)) {
    return "license_key_invalid_format";
  }
  if (
    typeof b.machine_id_hash !== "string" ||
    b.machine_id_hash.length < MACHINE_ID_HASH_MIN_LEN
  ) {
    return "machine_id_hash_invalid_format";
  }
  return null;
}

/** Returns true if `tier` is at least `required`. */
export function tierMeetsRequirement(
  tier: OrchestratorTier,
  required: OrchestratorTier,
): boolean {
  return TIER_RANK[tier] >= TIER_RANK[required];
}

/**
 * Returns the first 8 hex chars of SHA-256(token) as a deterministic
 * preview tag for logs. The full token never leaves the function;
 * the preview is enough for cross-correlation against the corresponding
 * `podman pull` audit log without leaking the secret.
 */
export async function tokenPreview(token: string): Promise<string> {
  const enc = new TextEncoder().encode(token);
  const hash = await crypto.subtle.digest("SHA-256", enc);
  const arr = Array.from(new Uint8Array(hash));
  return arr.slice(0, 4).map((b) => b.toString(16).padStart(2, "0")).join("");
}
