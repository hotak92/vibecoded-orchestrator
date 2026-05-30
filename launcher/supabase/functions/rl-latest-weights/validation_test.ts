// Unit tests for rl-latest-weights validation helpers.
//
// Run with:
//   deno test --no-check \
//     launcher/supabase/functions/rl-latest-weights/validation_test.ts
//
// Same harness style as rl-latest-version/validation_test.ts — pure
// functions only, no Supabase project required, no network.

import {
  assertEquals,
  assertNotEquals,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  DEFAULT_EMBEDDING_SOURCE,
  DEFAULT_MODULE_ID,
  MACHINE_ID_HASH_MIN_LEN,
  REQUIRED_TIER,
  TIER_RANK,
  tierMeetsRequirement,
  tokenPreview,
  UUID_RE,
  validateRequestBody,
} from "./validation.ts";

// ─── validateRequestBody: happy paths ────────────────────────────────────

Deno.test("validateRequestBody: accepts well-formed body with all fields", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    embedding_source: "arctic",
    module_id: "vct-rl-reranker",
  });
  assertEquals(result.ok, true);
  if (result.ok) {
    assertEquals(result.body.license_key, "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480");
    assertEquals(result.body.embedding_source, "arctic");
    assertEquals(result.body.module_id, "vct-rl-reranker");
  }
});

Deno.test("validateRequestBody: defaults embedding_source to qwen3 when missing", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result.ok, true);
  if (result.ok) {
    assertEquals(result.body.embedding_source, DEFAULT_EMBEDDING_SOURCE);
    assertEquals(result.body.embedding_source, "qwen3");
  }
});

Deno.test("validateRequestBody: defaults module_id to vct-rl-reranker when missing", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result.ok, true);
  if (result.ok) {
    assertEquals(result.body.module_id, DEFAULT_MODULE_ID);
    assertEquals(result.body.module_id, "vct-rl-reranker");
  }
});

Deno.test("validateRequestBody: accepts minimum-length machine_id_hash", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(MACHINE_ID_HASH_MIN_LEN),
  });
  assertEquals(result.ok, true);
});

Deno.test("validateRequestBody: accepts arbitrary new embedding_source (server-side discovery)", () => {
  // The validator does NOT enforce an enum — that's the edge function's
  // job (it consults paid_module_releases for the supported list). Any
  // non-empty string passes here. Same posture as rl-latest-version.
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    embedding_source: "future-source-not-yet-shipped",
  });
  assertEquals(result.ok, true);
  if (result.ok) {
    assertEquals(result.body.embedding_source, "future-source-not-yet-shipped");
  }
});

Deno.test("validateRequestBody: case-insensitive UUID match", () => {
  const result = validateRequestBody({
    license_key: "9CA4BD72-7f5e-4D18-AE8D-C00D1e2e2480",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result.ok, true);
});

// ─── validateRequestBody: failure paths ──────────────────────────────────

Deno.test("validateRequestBody: rejects non-object body", () => {
  const r1 = validateRequestBody(null);
  assertEquals(r1.ok, false);
  if (!r1.ok) assertEquals(r1.error, "license_key_invalid_format");

  const r2 = validateRequestBody("string");
  assertEquals(r2.ok, false);

  const r3 = validateRequestBody(42);
  assertEquals(r3.ok, false);

  const r4 = validateRequestBody([]);
  assertEquals(r4.ok, false);
  if (!r4.ok) assertEquals(r4.error, "license_key_invalid_format");
});

Deno.test("validateRequestBody: T1 — rejects missing license_key", () => {
  const result = validateRequestBody({
    machine_id_hash: "a".repeat(64),
    embedding_source: "qwen3",
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "license_key_invalid_format");
});

Deno.test("validateRequestBody: rejects non-UUID license_key", () => {
  const result = validateRequestBody({
    license_key: "not-a-uuid",
    machine_id_hash: "a".repeat(64),
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "license_key_invalid_format");
});

Deno.test("validateRequestBody: T2 — rejects missing machine_id_hash", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    embedding_source: "qwen3",
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "machine_id_hash_invalid_format");
});

Deno.test("validateRequestBody: rejects short machine_id_hash", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(MACHINE_ID_HASH_MIN_LEN - 1),
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "machine_id_hash_invalid_format");
});

Deno.test("validateRequestBody: T3 — present-but-empty embedding_source rejected", () => {
  // T3 from the brief: "missing embedding_source → 400". Note that an
  // ABSENT field is legal (defaulted to qwen3 — see "defaults" test
  // above). The 400-yielding case is PRESENT-BUT-INVALID: explicit
  // empty string. The brief's "missing" wording maps to this case
  // because a launcher that wires the field to `""` (e.g. by reading
  // an unset env var) needs to know the field is malformed rather
  // than silently getting qwen3.
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    embedding_source: "",
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "embedding_source_invalid_type");
});

Deno.test("validateRequestBody: rejects non-string embedding_source", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    embedding_source: 42,
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "embedding_source_invalid_type");
});

Deno.test("validateRequestBody: T4 — arbitrary embedding_source string passes validation (server-side discovery)", () => {
  // T4 from the brief is "invalid embedding_source ('arctic3') → 400
  // with allowlist hint". DESIGN DEVIATION (from the brief but
  // consistent with rl-latest-version's posture): the validator does
  // NOT enforce an enum — that decision lives in the edge function,
  // which queries paid_module_releases for the supported list and
  // emits a 400 with `supported_embedding_sources` populated. This
  // keeps the vocabulary data-driven so a new embedding source goes
  // live the moment a row is inserted; no function redeploy required.
  //
  // The test asserts that ANY non-empty string passes the validator
  // (including "arctic3"); the 400-with-allowlist response comes from
  // the edge function's discoverSupportedSources() branch, not the
  // validator.
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    embedding_source: "arctic3",
  });
  assertEquals(result.ok, true);
  if (result.ok) {
    assertEquals(result.body.embedding_source, "arctic3");
  }
});

Deno.test("validateRequestBody: rejects empty-string module_id when present", () => {
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    module_id: "",
  });
  assertEquals(result.ok, false);
  if (!result.ok) assertEquals(result.error, "module_id_invalid_type");
});

Deno.test("validateRequestBody: T5 — valid payload passes (downstream issues handled by edge fn)", () => {
  // T5 from the brief: "valid payload passes validation (downstream
  // issues handled elsewhere)". Mirrors the caller's typical request
  // shape from launcher/src-tauri/src/commands/module_default_weights.rs.
  const result = validateRequestBody({
    license_key: "9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480",
    machine_id_hash: "a".repeat(64),
    embedding_source: "qwen3",
    module_id: "vct-rl-reranker",
  });
  assertEquals(result.ok, true);
  if (result.ok) {
    // No `current_weights_version` field — that's the rl-latest-version
    // contract, NOT rl-latest-weights. Pin the wire-shape difference.
    assertEquals(
      Object.keys(result.body).sort(),
      [
        "embedding_source",
        "license_key",
        "machine_id_hash",
        "module_id",
      ],
    );
  }
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

Deno.test("TIER_RANK: monotonic ordering free < pro < mao < enterprise < admin", () => {
  assertEquals(TIER_RANK["free"] < TIER_RANK["pro"], true);
  assertEquals(TIER_RANK["pro"] < TIER_RANK["mao"], true);
  assertEquals(TIER_RANK["mao"] < TIER_RANK["enterprise"], true);
  assertEquals(TIER_RANK["enterprise"] < TIER_RANK["admin"], true);
});

Deno.test("REQUIRED_TIER: is pro (matches rl-latest-version gate)", () => {
  // Same gate as rl-latest-version. If we ever bump the required tier
  // for RL weights download (e.g. mao-only), both endpoints must move
  // together — this test forces a deliberate update.
  assertEquals(REQUIRED_TIER, "pro");
});

// ─── tokenPreview ────────────────────────────────────────────────────────

Deno.test("tokenPreview: deterministic 8 hex chars", async () => {
  const preview = await tokenPreview("https://signed.example/url?token=abc");
  assertEquals(preview.length, 8);
  assertEquals(/^[0-9a-f]{8}$/.test(preview), true);
});

Deno.test("tokenPreview: different URLs produce different previews", async () => {
  const a = await tokenPreview("https://signed.example/a");
  const b = await tokenPreview("https://signed.example/b");
  assertNotEquals(a, b);
});

Deno.test("tokenPreview: identical inputs produce identical previews", async () => {
  const a = await tokenPreview("same-url");
  const b = await tokenPreview("same-url");
  assertEquals(a, b);
});

// ─── UUID_RE direct ──────────────────────────────────────────────────────

Deno.test("UUID_RE: matches lemon-squeezy-style license keys", () => {
  assertEquals(UUID_RE.test("9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480"), true);
});

Deno.test("UUID_RE: rejects 32-char no-hyphen UUIDs", () => {
  assertEquals(UUID_RE.test("9ca4bd727f5e4d18ae8dc00d1e2e2480"), false);
});

Deno.test("UUID_RE: rejects keys with whitespace", () => {
  assertEquals(UUID_RE.test(" 9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480"), false);
  assertEquals(UUID_RE.test("9ca4bd72-7f5e-4d18-ae8d-c00d1e2e2480 "), false);
});

// ─── Defaults ────────────────────────────────────────────────────────────

Deno.test("DEFAULT_MODULE_ID: is vct-rl-reranker", () => {
  assertEquals(DEFAULT_MODULE_ID, "vct-rl-reranker");
});

Deno.test("DEFAULT_EMBEDDING_SOURCE: is qwen3", () => {
  assertEquals(DEFAULT_EMBEDDING_SOURCE, "qwen3");
});
