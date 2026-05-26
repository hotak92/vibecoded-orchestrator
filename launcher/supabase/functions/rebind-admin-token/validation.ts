// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Pure validation helpers for the rebind-admin-token edge function.
//
// Split from index.ts so they can be exercised with `deno test` against
// pure inputs (no Deno.serve, no Deno.env, no network). Same harness
// style as rl-artifact-url/validation.ts.

/** Vault admin token regex — same shape as validate-tier accepts. */
export const VAULT_ADMIN_TOKEN_RE = /^vct_admin_[A-Za-z0-9_-]+$/;

/** Minimum token length — matches `isValidVaultAdminToken` in validate-tier. */
export const VAULT_ADMIN_TOKEN_MIN_LEN = 30;
export const VAULT_ADMIN_TOKEN_MAX_LEN = 256;

/** sha256 hex string — 64 lowercase hex chars. */
export const MACHINE_ID_HASH_RE = /^[0-9a-f]{64}$/;

export interface RequestBody {
  license_key: string;
  new_machine_id_hash: string;
}

export type ValidationError =
  | "license_key_invalid_format"
  | "machine_id_hash_invalid_format";

/**
 * Accept a candidate `vct_admin_*` token.
 *
 * Stricter than the rl-artifact-url shape check because this endpoint
 * is exclusively for the Vault-admin path — LS UUIDs are not valid
 * inputs (LS license keys can't be machine-rebound through here; their
 * activation/deactivation lives in the LS dashboard at
 * vibecodedtools.it/account).
 */
export function isValidVaultAdminToken(s: string): boolean {
  return (
    s.length >= VAULT_ADMIN_TOKEN_MIN_LEN &&
    s.length <= VAULT_ADMIN_TOKEN_MAX_LEN &&
    VAULT_ADMIN_TOKEN_RE.test(s)
  );
}

/** Validate the request body shape. Returns null on success, error code on failure. */
export function validateRequestBody(body: unknown): ValidationError | null {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return "license_key_invalid_format";
  }
  const b = body as Partial<RequestBody>;
  if (typeof b.license_key !== "string" || !isValidVaultAdminToken(b.license_key)) {
    return "license_key_invalid_format";
  }
  if (
    typeof b.new_machine_id_hash !== "string" ||
    !MACHINE_ID_HASH_RE.test(b.new_machine_id_hash)
  ) {
    return "machine_id_hash_invalid_format";
  }
  return null;
}
