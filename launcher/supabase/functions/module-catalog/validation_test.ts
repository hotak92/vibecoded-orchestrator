// Unit tests for module-catalog input validation (audit RLS-2).
//
// Run with:
//   deno test --no-check \
//     launcher/supabase/functions/module-catalog/validation_test.ts
//
// Same harness style as rl-latest-version/validation_test.ts — pure
// functions only, no Supabase project required, no network. The 400-on-
// mismatch wiring in index.ts can't be exercised without a live Supabase
// storage bucket; this pins the guard predicate that gates it.

import { assertEquals } from "https://deno.land/std@0.224.0/assert/mod.ts";
import { isValidModuleId, MODULE_ID_RE } from "./validation.ts";

// ─── Accepts well-formed slugs ──────────────────────────────────────────────

Deno.test("isValidModuleId: accepts a real module slug", () => {
  assertEquals(isValidModuleId("vct-rl-reranker"), true);
});

Deno.test("isValidModuleId: accepts digits and single tokens", () => {
  assertEquals(isValidModuleId("orchestrator"), true);
  assertEquals(isValidModuleId("module2"), true);
  assertEquals(isValidModuleId("a"), true);
  assertEquals(isValidModuleId("1"), true);
  assertEquals(isValidModuleId("multi-word-9-slug"), true);
});

// ─── Rejects path-traversal / extension-confusion attempts ──────────────────

Deno.test("isValidModuleId: rejects path separators", () => {
  assertEquals(isValidModuleId("../secrets"), false);
  assertEquals(isValidModuleId("foo/bar"), false);
  assertEquals(isValidModuleId("/etc/passwd"), false);
});

Deno.test("isValidModuleId: rejects dots (extension confusion)", () => {
  assertEquals(isValidModuleId("module.json"), false);
  assertEquals(isValidModuleId(".."), false);
  assertEquals(isValidModuleId("a.b"), false);
});

Deno.test("isValidModuleId: rejects whitespace", () => {
  assertEquals(isValidModuleId("mod x"), false);
  assertEquals(isValidModuleId(" mod"), false);
  assertEquals(isValidModuleId("mod\n"), false);
  assertEquals(isValidModuleId("mod\t"), false);
});

Deno.test("isValidModuleId: rejects uppercase", () => {
  assertEquals(isValidModuleId("Module"), false);
  assertEquals(isValidModuleId("VCT-RL"), false);
});

Deno.test("isValidModuleId: rejects empty string", () => {
  assertEquals(isValidModuleId(""), false);
});

Deno.test("isValidModuleId: rejects URL-encoded and special chars", () => {
  assertEquals(isValidModuleId("mod%2e%2e"), false);
  assertEquals(isValidModuleId("mod_underscore"), false);
  assertEquals(isValidModuleId("mod;drop"), false);
  assertEquals(isValidModuleId("mod*"), false);
  assertEquals(isValidModuleId("mod?q=1"), false);
});

// ─── Regex is anchored (no partial match acceptance) ────────────────────────

Deno.test("MODULE_ID_RE: is anchored at both ends", () => {
  // A regex missing ^ or $ would accept these via partial match.
  assertEquals(MODULE_ID_RE.test("good\nbad/path"), false);
  assertEquals(MODULE_ID_RE.test("bad/path\ngood"), false);
});
