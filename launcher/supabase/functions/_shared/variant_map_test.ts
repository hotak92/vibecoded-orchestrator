// Admin variant resolution unit tests.
//
// Run with:  deno test --no-check launcher/supabase/functions/_shared/variant_map_test.ts
//
// CI runs the same command (no Supabase project required — purely
// pure-function tests on lookupVariant + isAdminVariant).

import { assertEquals, assertThrows } from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  assertNoPlaceholderKeysInProduction,
  constantTimeEq,
  isAdminVariant,
  lookupVariant,
  lookupVaultAdminToken,
} from "./variant_map.ts";

function withAdminEnv<T>(value: string | string[] | undefined, fn: () => T): T {
  // deno-lint-ignore no-explicit-any
  const g = globalThis as any;
  const prev = g.__VCT_ADMIN_VARIANT_IDS__;
  if (value === undefined) {
    delete g.__VCT_ADMIN_VARIANT_IDS__;
  } else {
    g.__VCT_ADMIN_VARIANT_IDS__ = value;
  }
  try {
    return fn();
  } finally {
    if (prev === undefined) {
      delete g.__VCT_ADMIN_VARIANT_IDS__;
    } else {
      g.__VCT_ADMIN_VARIANT_IDS__ = prev;
    }
  }
}

Deno.test("isAdminVariant: returns false when env unset", () => {
  withAdminEnv(undefined, () => {
    assertEquals(isAdminVariant("999"), false);
  });
});

Deno.test("isAdminVariant: matches a variant in the array env", () => {
  withAdminEnv(["999", "1000"], () => {
    assertEquals(isAdminVariant("999"), true);
    assertEquals(isAdminVariant("1000"), true);
    assertEquals(isAdminVariant("1001"), false);
  });
});

Deno.test("isAdminVariant: matches when env is a JSON-encoded string", () => {
  withAdminEnv('["999"]', () => {
    assertEquals(isAdminVariant("999"), true);
    assertEquals(isAdminVariant("1000"), false);
  });
});

Deno.test("isAdminVariant: fails closed on malformed JSON", () => {
  withAdminEnv("not-json", () => {
    assertEquals(isAdminVariant("999"), false);
  });
});

Deno.test("isAdminVariant: fails closed on non-array JSON", () => {
  withAdminEnv('{"999": true}', () => {
    assertEquals(isAdminVariant("999"), false);
  });
});

Deno.test("lookupVariant: admin variant returns admin tier on orchestrator", () => {
  withAdminEnv(["999"], () => {
    const m = lookupVariant("999");
    assertEquals(m, { appId: "orchestrator", tier: "admin" });
  });
});

Deno.test("lookupVariant: admin classification overrides any static map entry", () => {
  // Even if 999 were in VARIANT_MAP as a Pro key, admin env would win.
  withAdminEnv(["ORCHESTRATOR_PRO_MONTHLY_PLACEHOLDER"], () => {
    const m = lookupVariant("ORCHESTRATOR_PRO_MONTHLY_PLACEHOLDER");
    assertEquals(m, { appId: "orchestrator", tier: "admin" });
  });
});

Deno.test("lookupVariant: unknown variant returns undefined", () => {
  withAdminEnv(undefined, () => {
    assertEquals(lookupVariant("does-not-exist"), undefined);
  });
});

Deno.test("lookupVariant: static map fallback when not admin", () => {
  withAdminEnv(undefined, () => {
    const m = lookupVariant("ORCHESTRATOR_PRO_MONTHLY_TODO");
    assertEquals(m, { appId: "orchestrator", tier: "pro" });
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Vault-token admin path (separate from Bug 33 LS-variant path)
// ────────────────────────────────────────────────────────────────────────────

const TEST_TOKEN_ALICE = "vct_admin_" + "x".repeat(64);
const TEST_TOKEN_BOB   = "vct_admin_" + "y".repeat(64);
const TEST_MACHINE_HASH  = "a".repeat(64);
const OTHER_MACHINE_HASH = "b".repeat(64);
const NOW_ISO = "2026-04-26T20:00:00Z";

const SAMPLE_VAULT = JSON.stringify({
  alice: { token: TEST_TOKEN_ALICE, expires_at: null, machine_id_hash: null },
  bob:   { token: TEST_TOKEN_BOB,   expires_at: "2026-12-31T00:00:00Z", machine_id_hash: TEST_MACHINE_HASH },
});

Deno.test("constantTimeEq: equal strings", () => {
  assertEquals(constantTimeEq("abc", "abc"), true);
  assertEquals(constantTimeEq("", ""), true);
});

Deno.test("constantTimeEq: different strings", () => {
  assertEquals(constantTimeEq("abc", "abd"), false);
  assertEquals(constantTimeEq("abc", "ab"), false); // length mismatch
  assertEquals(constantTimeEq("abc", "abcd"), false);
});

Deno.test("lookupVaultAdminToken: missing prefix returns null (fast path)", () => {
  // LS license keys (UUIDs) lack the prefix → null without touching vault map
  assertEquals(
    lookupVaultAdminToken("12345678-1234-1234-1234-123456789012", TEST_MACHINE_HASH, SAMPLE_VAULT),
    null,
  );
});

Deno.test("lookupVaultAdminToken: null vault JSON returns null", () => {
  assertEquals(
    lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, null),
    null,
  );
});

Deno.test("lookupVaultAdminToken: malformed JSON returns null", () => {
  assertEquals(
    lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, "not json"),
    null,
  );
});

Deno.test("lookupVaultAdminToken: token not in map returns null", () => {
  assertEquals(
    lookupVaultAdminToken("vct_admin_unknown" + "z".repeat(50), TEST_MACHINE_HASH, SAMPLE_VAULT),
    null,
  );
});

Deno.test("lookupVaultAdminToken: TOFU bind on unbound user", () => {
  const r = lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, SAMPLE_VAULT, NOW_ISO);
  assertEquals(r, {
    user: "alice",
    outcome: "success",
    bind_machine_hash: TEST_MACHINE_HASH,
  });
});

Deno.test("lookupVaultAdminToken: bound user matching machine succeeds", () => {
  const r = lookupVaultAdminToken(TEST_TOKEN_BOB, TEST_MACHINE_HASH, SAMPLE_VAULT, NOW_ISO);
  assertEquals(r, { user: "bob", outcome: "success" });
});

Deno.test("lookupVaultAdminToken: bound user from a different machine rejects", () => {
  const r = lookupVaultAdminToken(TEST_TOKEN_BOB, OTHER_MACHINE_HASH, SAMPLE_VAULT, NOW_ISO);
  assertEquals(r, { user: "bob", outcome: "machine_mismatch" });
});

Deno.test("lookupVaultAdminToken: expired token rejects (regardless of machine)", () => {
  const expiredVault = JSON.stringify({
    stale: { token: TEST_TOKEN_ALICE, expires_at: "2026-01-01T00:00:00Z", machine_id_hash: TEST_MACHINE_HASH },
  });
  const r = lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, expiredVault, NOW_ISO);
  assertEquals(r, { user: "stale", outcome: "expired" });
});

Deno.test("lookupVaultAdminToken: future expiration is fine", () => {
  const futureVault = JSON.stringify({
    user: { token: TEST_TOKEN_ALICE, expires_at: "2099-01-01T00:00:00Z", machine_id_hash: TEST_MACHINE_HASH },
  });
  const r = lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, futureVault, NOW_ISO);
  assertEquals(r, { user: "user", outcome: "success" });
});

Deno.test("lookupVaultAdminToken: malformed entry (missing token field) is skipped", () => {
  const badVault = JSON.stringify({
    user1: { /* no token */ expires_at: null, machine_id_hash: null },
    user2: { token: TEST_TOKEN_ALICE, expires_at: null, machine_id_hash: null },
  });
  const r = lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, badVault, NOW_ISO);
  // user2 still matches; user1 is skipped
  assertEquals(r?.user, "user2");
});

Deno.test("lookupVaultAdminToken: array (not object) at top level returns null", () => {
  // Defensive: malformed Vault content shouldn't crash
  assertEquals(
    lookupVaultAdminToken(TEST_TOKEN_ALICE, TEST_MACHINE_HASH, JSON.stringify([1, 2, 3])),
    null,
  );
});

// ─── Placeholder-key production guard (audit blocker #3, 2026-05-07) ──

Deno.test("assertNoPlaceholderKeysInProduction: throws when NODE_ENV=production", () => {
  // Current VARIANT_MAP still has *_PLACEHOLDER keys (pre-launch state).
  // The guard MUST throw under production env to prevent silent webhook
  // failure on launch day.
  assertThrows(
    () =>
      assertNoPlaceholderKeysInProduction({ NODE_ENV: "production" }),
    Error,
    "placeholder",
  );
});

Deno.test("assertNoPlaceholderKeysInProduction: throws when DENO_DEPLOYMENT_ID is set", () => {
  // Supabase edge functions don't set NODE_ENV but DO set
  // DENO_DEPLOYMENT_ID — both signal production.
  assertThrows(
    () =>
      assertNoPlaceholderKeysInProduction({ DENO_DEPLOYMENT_ID: "abc-123" }),
    Error,
    "placeholder",
  );
});

Deno.test("assertNoPlaceholderKeysInProduction: silent in dev (no env)", () => {
  // Local dev / unit tests: empty env → no NODE_ENV, no
  // DENO_DEPLOYMENT_ID → guard skips. Test as no-throw.
  assertNoPlaceholderKeysInProduction({});
});

Deno.test("assertNoPlaceholderKeysInProduction: silent in dev (NODE_ENV=development)", () => {
  assertNoPlaceholderKeysInProduction({ NODE_ENV: "development" });
});

Deno.test("assertNoPlaceholderKeysInProduction: error message names offending keys", () => {
  // Useful for the on-call engineer: error tells them WHICH keys to fix.
  try {
    assertNoPlaceholderKeysInProduction({ NODE_ENV: "production" });
    throw new Error("expected throw");
  } catch (e) {
    const msg = (e as Error).message;
    // Each of the 6 placeholder keys should be named.
    for (const k of [
      "ORCHESTRATOR_PRO_MONTHLY_PLACEHOLDER",
      "ORCHESTRATOR_PRO_ANNUAL_PLACEHOLDER",
      "ORCHESTRATOR_PRO_LIFETIME_PLACEHOLDER",
      "MAO_MONTHLY_PLACEHOLDER",
      "MAO_ANNUAL_PLACEHOLDER",
      "MAO_LIFETIME_PLACEHOLDER",
    ]) {
      if (!msg.includes(k)) {
        throw new Error(`error message missing key ${k}: ${msg}`);
      }
    }
  }
});

// ─── Pre-LS-products opt-out (Path A-only deployments, 2026-05-21) ─────

Deno.test("assertNoPlaceholderKeysInProduction: bypassed by VCT_LS_VARIANTS_NOT_YET_SET=true", () => {
  // Path A (Vault-token admin) doesn't touch VARIANT_MAP. Until LS
  // products are provisioned, the placeholder keys are intentional
  // pre-launch state. The opt-out flag acknowledges this explicitly
  // and lets the function come up under Supabase runtime
  // (DENO_DEPLOYMENT_ID set) without crashing at module init.
  assertNoPlaceholderKeysInProduction({
    DENO_DEPLOYMENT_ID: "supabase-prod-deploy-abc",
    VCT_LS_VARIANTS_NOT_YET_SET: "true",
  });
});

Deno.test("assertNoPlaceholderKeysInProduction: opt-out does NOT accept truthy non-'true'", () => {
  // Strict-string match: "true" only. "1" / "yes" / "TRUE" should not
  // bypass — typos must fail loudly, not silently degrade to the
  // legacy production-deploy-with-placeholders bug. Tighter than a
  // generic truthy check on purpose.
  for (const sloppy of ["1", "yes", "TRUE", "True", "y", " true "]) {
    assertThrows(
      () =>
        assertNoPlaceholderKeysInProduction({
          NODE_ENV: "production",
          VCT_LS_VARIANTS_NOT_YET_SET: sloppy,
        }),
      Error,
      "placeholder",
    );
  }
});

Deno.test("assertNoPlaceholderKeysInProduction: opt-out absent → still throws", () => {
  // Without the flag (or with it set to anything else), production
  // deployment with placeholder keys must still crash at init —
  // protecting against silent production-with-placeholders regression
  // if someone removes the flag after real LS variants land.
  assertThrows(
    () =>
      assertNoPlaceholderKeysInProduction({
        DENO_DEPLOYMENT_ID: "supabase-prod-deploy-xyz",
        VCT_LS_VARIANTS_NOT_YET_SET: "false",
      }),
    Error,
    "placeholder",
  );
});

Deno.test("assertNoPlaceholderKeysInProduction: error message points to the opt-out flag", () => {
  // On-call diagnosability: a future maintainer hitting the assertion
  // should see in the error message that the opt-out flag exists, so
  // they can decide whether to set it (Path A only) or replace the
  // placeholders (real LS products launching).
  try {
    assertNoPlaceholderKeysInProduction({ NODE_ENV: "production" });
    throw new Error("expected throw");
  } catch (e) {
    const msg = (e as Error).message;
    if (!msg.includes("VCT_LS_VARIANTS_NOT_YET_SET")) {
      throw new Error(
        `error message must mention the opt-out flag: ${msg}`,
      );
    }
  }
});
