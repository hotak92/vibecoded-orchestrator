// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Unit tests for rebind-admin-token validation + auth-resolution helpers.
//
// Run with:
//   deno test --no-check \
//     launcher/supabase/functions/rebind-admin-token/validation_test.ts
//
// Same harness style as the other admin-license endpoints — pure
// functions, no network, no Supabase project required.

import {
  assertEquals,
  assertNotEquals,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  isValidVaultAdminToken,
  MACHINE_ID_HASH_RE,
  validateRequestBody,
  VAULT_ADMIN_TOKEN_MAX_LEN,
  VAULT_ADMIN_TOKEN_MIN_LEN,
} from "./validation.ts";
import { lookupVaultAdminTokenUser } from "../_shared/variant_map.ts";

// ─── validateRequestBody: valid_rebind ───────────────────────────────────

Deno.test("validateRequestBody: accepts a well-formed rebind request", () => {
  const result = validateRequestBody({
    license_key: "vct_admin_" + "A".repeat(40),
    new_machine_id_hash: "0".repeat(64),
  });
  assertEquals(result, null);
});

Deno.test("validateRequestBody: rejects non-object body", () => {
  assertEquals(validateRequestBody(null), "license_key_invalid_format");
  assertEquals(validateRequestBody("string"), "license_key_invalid_format");
  assertEquals(validateRequestBody(42), "license_key_invalid_format");
  assertEquals(validateRequestBody([]), "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects UUID license_key (wrong shape)", () => {
  // The validate-tier endpoint accepts BOTH UUIDs and vct_admin_ tokens;
  // rebind-admin-token is exclusively for the Vault-admin path.
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    new_machine_id_hash: "0".repeat(64),
  });
  assertEquals(result, "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects short vct_admin_ token", () => {
  const result = validateRequestBody({
    license_key: "vct_admin_short",
    new_machine_id_hash: "0".repeat(64),
  });
  assertEquals(result, "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects malformed machine_id_hash (too short)", () => {
  const result = validateRequestBody({
    license_key: "vct_admin_" + "A".repeat(40),
    new_machine_id_hash: "0".repeat(32),
  });
  assertEquals(result, "machine_id_hash_invalid_format");
});

Deno.test("validateRequestBody: rejects non-hex machine_id_hash", () => {
  const result = validateRequestBody({
    license_key: "vct_admin_" + "A".repeat(40),
    new_machine_id_hash: "Z".repeat(64),
  });
  assertEquals(result, "machine_id_hash_invalid_format");
});

Deno.test("validateRequestBody: rejects uppercase hex (canonicalize at client)", () => {
  const result = validateRequestBody({
    license_key: "vct_admin_" + "A".repeat(40),
    new_machine_id_hash: "F".repeat(64),
  });
  assertEquals(result, "machine_id_hash_invalid_format");
});

// ─── isValidVaultAdminToken ───────────────────────────────────────────────

Deno.test("isValidVaultAdminToken: accepts vct_admin_ + 40 chars", () => {
  assertEquals(isValidVaultAdminToken("vct_admin_" + "A".repeat(40)), true);
});

Deno.test("isValidVaultAdminToken: rejects missing prefix", () => {
  assertEquals(isValidVaultAdminToken("A".repeat(50)), false);
});

Deno.test("isValidVaultAdminToken: rejects too-long token", () => {
  const tooLong = "vct_admin_" + "A".repeat(VAULT_ADMIN_TOKEN_MAX_LEN);
  assertEquals(isValidVaultAdminToken(tooLong), false);
});

Deno.test("isValidVaultAdminToken: enforces min length", () => {
  // VAULT_ADMIN_TOKEN_MIN_LEN = 30; produce a token EXACTLY one char short.
  const justShort = "vct_admin_" + "A".repeat(VAULT_ADMIN_TOKEN_MIN_LEN - "vct_admin_".length - 1);
  assertEquals(isValidVaultAdminToken(justShort), false);
});

// ─── lookupVaultAdminTokenUser ────────────────────────────────────────────
//
// These exercise the auth-resolution helper that the rebind-admin-token
// edge function calls before invoking the SECURITY DEFINER RPC. The
// edge function trusts a successful match here as "the caller possesses
// this user's token" — same security model validate-tier uses.

Deno.test("lookupVaultAdminTokenUser: returns user on token match", () => {
  const vault = JSON.stringify({
    "admin1": { token: "vct_admin_alpha", expires_at: null, machine_id_hash: null },
    "admin2": { token: "vct_admin_beta", expires_at: null, machine_id_hash: "abc" },
  });
  assertEquals(lookupVaultAdminTokenUser("vct_admin_alpha", vault), "admin1");
  assertEquals(lookupVaultAdminTokenUser("vct_admin_beta", vault), "admin2");
});

Deno.test("lookupVaultAdminTokenUser: returns null on miss", () => {
  const vault = JSON.stringify({
    "admin1": { token: "vct_admin_alpha", expires_at: null, machine_id_hash: null },
  });
  assertEquals(lookupVaultAdminTokenUser("vct_admin_unknown", vault), null);
});

Deno.test("lookupVaultAdminTokenUser: returns null on wrong-prefix submission", () => {
  // Cheap short-circuit before touching the map.
  const vault = JSON.stringify({
    "admin1": { token: "vct_admin_alpha", expires_at: null, machine_id_hash: null },
  });
  assertEquals(
    lookupVaultAdminTokenUser("9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480", vault),
    null,
  );
});

Deno.test("lookupVaultAdminTokenUser: returns null on null/empty vault", () => {
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", null), null);
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", ""), null);
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", undefined), null);
});

Deno.test("lookupVaultAdminTokenUser: returns null on unparseable vault JSON", () => {
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", "{not json"), null);
});

Deno.test("lookupVaultAdminTokenUser: returns null on non-object vault", () => {
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", "[]"), null);
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", "null"), null);
  assertEquals(lookupVaultAdminTokenUser("vct_admin_anything", "\"string\""), null);
});

Deno.test("lookupVaultAdminTokenUser: ignores expired tokens (rebind WORKS for expired)", () => {
  // Rationale captured in variant_map.ts: an admin reinstalling their
  // OS after their token expired should still be able to rebind, then
  // rotate expires_at via SQL afterwards. Blocking on expiry here
  // would defeat the recovery path.
  const vault = JSON.stringify({
    "admin1": {
      token: "vct_admin_alpha",
      expires_at: "2020-01-01T00:00:00Z",
      machine_id_hash: "old-machine",
    },
  });
  assertEquals(lookupVaultAdminTokenUser("vct_admin_alpha", vault), "admin1");
});

// ─── MACHINE_ID_HASH_RE ──────────────────────────────────────────────────

Deno.test("MACHINE_ID_HASH_RE: matches valid sha256 hex", () => {
  assertEquals(MACHINE_ID_HASH_RE.test("0".repeat(64)), true);
  assertEquals(MACHINE_ID_HASH_RE.test("abcdef0123456789".repeat(4)), true);
});

Deno.test("MACHINE_ID_HASH_RE: rejects wrong length", () => {
  assertNotEquals(MACHINE_ID_HASH_RE.test("0".repeat(63)), true);
  assertNotEquals(MACHINE_ID_HASH_RE.test("0".repeat(65)), true);
});

Deno.test("MACHINE_ID_HASH_RE: rejects uppercase hex", () => {
  // Canonical form is lowercase — matches the Rust `hex::encode` output.
  assertNotEquals(MACHINE_ID_HASH_RE.test("A".repeat(64)), true);
});
