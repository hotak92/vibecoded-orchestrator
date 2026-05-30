// Pure validation helpers for rl-latest-weights.
//
// Split out from index.ts so they can be tested without spinning up a
// Deno server or mocking Deno.serve / Deno.env. The Deno test harness
// runs these directly via `deno test`.
//
// No I/O, no Deno globals — pure functions only.
//
// Why a separate validator from rl-latest-version's:
//   The two endpoints have OVERLAPPING but DIFFERENT request shapes.
//   rl-latest-version takes `current_weights_version` (poll: "do I have
//   the head?"); rl-latest-weights doesn't (fast path: "just give me
//   the head"). Keeping the validators isolated means a future contract
//   change to one endpoint doesn't risk silently bending the other.

import type { OrchestratorTier } from "../_shared/variant_map.ts";

// License key UUID regex — same shape validate-tier / rl-latest-version accept.
export const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// Minimum machine-id-hash length (SHA-256 hex would be 64; we accept
// ≥16 to allow for shorter dev hashes during testing). Matches the
// posture of rl-artifact-url / rl-latest-version.
export const MACHINE_ID_HASH_MIN_LEN = 16;

export const TIER_RANK: Record<OrchestratorTier, number> = {
  free: 0,
  pro: 1,
  mao: 2,
  enterprise: 3,
  admin: 4,
};

// Tier required to fetch RL weights — same gate as rl-latest-version.
// Locked decision: paid-module weights are pro-or-higher.
export const REQUIRED_TIER: OrchestratorTier = "pro";

// Defaults applied to optional fields. The defaults match the
// most-common consumer (the launcher's "Download default weights"
// manifest button for the RL Reranker module, qwen3 embedding source
// as of v0.2.32).
export const DEFAULT_MODULE_ID = "vct-rl-reranker";
export const DEFAULT_EMBEDDING_SOURCE = "qwen3";

export interface RequestBody {
  license_key: string;
  machine_id_hash: string;
  embedding_source: string;
  module_id: string;
}

export type ValidationError =
  | "license_key_invalid_format"
  | "machine_id_hash_invalid_format"
  | "embedding_source_invalid_type"
  | "module_id_invalid_type";

export interface ValidationOk {
  ok: true;
  body: RequestBody;
}

export interface ValidationFail {
  ok: false;
  error: ValidationError;
}

/**
 * Validate the request body shape. Returns a discriminated union with
 * either the normalized body (defaults applied) or an error code.
 *
 * Required fields:
 *   - license_key: UUID v4 (case insensitive)
 *   - machine_id_hash: ≥16 hex chars
 *
 * Optional fields:
 *   - embedding_source: defaults to "qwen3"
 *   - module_id: defaults to "vct-rl-reranker"
 *
 * Note: embedding_source is NOT validated against an enum here — the
 * edge function does server-side discovery against the
 * `paid_module_releases` table and returns a 400 with the discovered
 * supported list on miss. This keeps the supported-sources vocabulary
 * data-driven, not code-driven. Same posture as rl-latest-version.
 */
export function validateRequestBody(body: unknown): ValidationOk | ValidationFail {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { ok: false, error: "license_key_invalid_format" };
  }
  const b = body as Record<string, unknown>;

  if (typeof b.license_key !== "string" || !UUID_RE.test(b.license_key)) {
    return { ok: false, error: "license_key_invalid_format" };
  }
  if (
    typeof b.machine_id_hash !== "string" ||
    b.machine_id_hash.length < MACHINE_ID_HASH_MIN_LEN
  ) {
    return { ok: false, error: "machine_id_hash_invalid_format" };
  }
  // embedding_source: optional, defaulted. If present must be a non-empty string.
  let embeddingSource = DEFAULT_EMBEDDING_SOURCE;
  if (b.embedding_source !== undefined) {
    if (
      typeof b.embedding_source !== "string" ||
      b.embedding_source.length === 0
    ) {
      return { ok: false, error: "embedding_source_invalid_type" };
    }
    embeddingSource = b.embedding_source;
  }
  // module_id: optional, defaulted. If present must be a non-empty string.
  let moduleId = DEFAULT_MODULE_ID;
  if (b.module_id !== undefined) {
    if (typeof b.module_id !== "string" || b.module_id.length === 0) {
      return { ok: false, error: "module_id_invalid_type" };
    }
    moduleId = b.module_id;
  }

  return {
    ok: true,
    body: {
      license_key: b.license_key,
      machine_id_hash: b.machine_id_hash,
      embedding_source: embeddingSource,
      module_id: moduleId,
    },
  };
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
 * audit log without leaking the secret (e.g. a signed URL with a
 * 15-minute TTL).
 */
export async function tokenPreview(token: string): Promise<string> {
  const enc = new TextEncoder().encode(token);
  const hash = await crypto.subtle.digest("SHA-256", enc);
  const arr = Array.from(new Uint8Array(hash));
  return arr.slice(0, 4).map((b) => b.toString(16).padStart(2, "0")).join("");
}
