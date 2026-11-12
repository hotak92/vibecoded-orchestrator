// Unit tests for rl-artifact-url validation helpers.
//
// Run with:
//   deno test --no-check \
//     launcher/supabase/functions/rl-artifact-url/validation_test.ts
//
// Same harness style as _shared/variant_map_test.ts — pure-function
// tests, no Supabase project required, no network.

import {
  assertEquals,
  assertNotEquals,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  MACHINE_ID_HASH_MIN_LEN,
  REQUIRED_TIER,
  TIER_RANK,
  tierMeetsRequirement,
  tokenPreview,
  UUID_RE,
  validateRequestBody,
} from "./validation.ts";

// ─── validateRequestBody ─────────────────────────────────────────────────

Deno.test("validateRequestBody: accepts well-formed body", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash:
      "a".repeat(64), // full SHA-256 hex
  });
  assertEquals(result, null);
});

Deno.test("validateRequestBody: accepts minimum-length hash", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(MACHINE_ID_HASH_MIN_LEN),
  });
  assertEquals(result, null);
});

Deno.test("validateRequestBody: rejects non-object body", () => {
  assertEquals(validateRequestBody(null), "license_key_invalid_format");
  assertEquals(validateRequestBody("string"), "license_key_invalid_format");
  assertEquals(validateRequestBody(42), "license_key_invalid_format");
  assertEquals(validateRequestBody([]), "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects missing license_key", () => {
  const result = validateRequestBody({ machine_id_hash: "a".repeat(64) });
  assertEquals(result, "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects non-UUID license_key", () => {
  const result = validateRequestBody({
    license_key: "not-a-uuid",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result, "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects malformed UUID (wrong section count)", () => {
  // Missing the last hex group.
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result, "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects short machine_id_hash", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(MACHINE_ID_HASH_MIN_LEN - 1),
  });
  assertEquals(result, "machine_id_hash_invalid_format");
});

Deno.test("validateRequestBody: rejects non-string machine_id_hash", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: 12345,
  });
  assertEquals(result, "machine_id_hash_invalid_format");
});

Deno.test("validateRequestBody: case-insensitive UUID match", () => {
  // Mixed case is a valid UUID v4.
  const result = validateRequestBody({
    license_key: "9CA4BD72-7f5e-4D18-AE8D-C00D1e2e2480",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result, null);
});

// ─── tierMeetsRequirement ────────────────────────────────────────────────

Deno.test("tierMeetsRequirement: free fails pro", () => {
  assertEquals(tierMeetsRequirement("free", "pro"), false);
});

Deno.test("tierMeetsRequirement: pro meets pro", () => {
  assertEquals(tierMeetsRequirement("pro", "pro"), true);
});

Deno.test("tierMeetsRequirement: mao meets pro (higher tier)", () => {
  assertEquals(tierMeetsRequirement("mao", "pro"), true);
});

Deno.test("tierMeetsRequirement: admin meets every tier", () => {
  assertEquals(tierMeetsRequirement("admin", "free"), true);
  assertEquals(tierMeetsRequirement("admin", "pro"), true);
  assertEquals(tierMeetsRequirement("admin", "mao"), true);
  assertEquals(tierMeetsRequirement("admin", "enterprise"), true);
});

Deno.test("tierMeetsRequirement: enterprise meets pro but not admin", () => {
  assertEquals(tierMeetsRequirement("enterprise", "pro"), true);
  assertEquals(tierMeetsRequirement("enterprise", "admin"), false);
});

Deno.test("TIER_RANK: monotonic ordering free < pro < mao < enterprise < admin", () => {
  assertEquals(TIER_RANK["free"] < TIER_RANK["pro"], true);
  assertEquals(TIER_RANK["pro"] < TIER_RANK["mao"], true);
  assertEquals(TIER_RANK["mao"] < TIER_RANK["enterprise"], true);
  assertEquals(TIER_RANK["enterprise"] < TIER_RANK["admin"], true);
});

Deno.test("REQUIRED_TIER: is pro (locked decision 2026-05-16)", () => {
  // If we ever bump the required tier (e.g., RL Reranker becomes
  // mao-tier), this test forces a deliberate update + downstream
  // launcher version bump rather than silent drift.
  assertEquals(REQUIRED_TIER, "pro");
});

// ─── tokenPreview ────────────────────────────────────────────────────────

Deno.test("tokenPreview: deterministic 8 hex chars", async () => {
  const preview = await tokenPreview("test-token-secret");
  assertEquals(preview.length, 8);
  assertEquals(/^[0-9a-f]{8}$/.test(preview), true);
});

Deno.test("tokenPreview: different tokens produce different previews", async () => {
  const a = await tokenPreview("token-a");
  const b = await tokenPreview("token-b");
  assertNotEquals(a, b);
});

Deno.test("tokenPreview: identical tokens produce identical previews", async () => {
  const a = await tokenPreview("same-token");
  const b = await tokenPreview("same-token");
  assertEquals(a, b);
});

Deno.test("tokenPreview: empty token still produces 8 chars", async () => {
  const preview = await tokenPreview("");
  assertEquals(preview.length, 8);
});

// ─── UUID_RE direct ──────────────────────────────────────────────────────

Deno.test("UUID_RE: matches lemon-squeezy-style license keys", () => {
  // Sample from validate-tier docstring shape.
  assertEquals(UUID_RE.test("9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480"), true);
});

Deno.test("UUID_RE: rejects 32-char no-hyphen UUIDs", () => {
  assertEquals(UUID_RE.test("9ca4bd727f5e4d18ae8dc00d1e2e2480"), false);
});

Deno.test("UUID_RE: rejects keys with whitespace", () => {
  assertEquals(UUID_RE.test(" 9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480"), false);
  assertEquals(UUID_RE.test("9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480 "), false);
});
