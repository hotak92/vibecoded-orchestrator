// Bug 33 — admin variant resolution unit tests.
//
// Run with:  deno test --no-check launcher/supabase/functions/_shared/variant_map_test.ts
//
// CI runs the same command (no Supabase project required — purely
// pure-function tests on lookupVariant + isAdminVariant).

import { assertEquals } from "https://deno.land/std@0.224.0/assert/mod.ts";
import { isAdminVariant, lookupVariant } from "./variant_map.ts";

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
  withAdminEnv(["ORCHESTRATOR_PRO_MONTHLY_TODO"], () => {
    const m = lookupVariant("ORCHESTRATOR_PRO_MONTHLY_TODO");
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
