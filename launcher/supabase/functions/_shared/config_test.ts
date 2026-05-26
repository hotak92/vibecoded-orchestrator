// SPDX-License-Identifier: AGPL-3.0-or-later
// Unit tests for _shared/config.ts.
//
// Run with:
//   deno test --no-check --allow-env \
//     launcher/supabase/functions/_shared/config_test.ts
//
// Pure-function half (validatePaidImageRepo, validatePaidTag): no
// Deno.env needed, just string-in / discriminated-result-out.
//
// Runtime-read half (resolvePaidImageRepo, resolvePaidTagDefault):
// uses Deno.env.set / Deno.env.delete in setup/teardown. We capture
// console.warn output via a swap-and-restore on globalThis.console.warn
// to verify the malformed-value warning fires (and doesn't fire on
// the legitimate unset case).

import {
  assertEquals,
  assertStringIncludes,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  PAID_IMAGE_REPO_DEFAULT,
  PAID_TAG_DEFAULT_FALLBACK,
  resolveGhcrUsername,
  resolvePaidImageRepo,
  resolvePaidTagDefault,
  validateGhcrUsername,
  validatePaidImageRepo,
  validatePaidTag,
} from "./config.ts";

// ─── validatePaidImageRepo (pure) ────────────────────────────────────────

Deno.test("validatePaidImageRepo: accepts canonical owner/image", () => {
  const r = validatePaidImageRepo("hotak92/vct-rl-reranker");
  assertEquals(r, { ok: true, value: "hotak92/vct-rl-reranker" });
});

Deno.test("validatePaidImageRepo: accepts org/image (post-migration shape)", () => {
  const r = validatePaidImageRepo("vibecodedtools/vct-rl-reranker");
  assertEquals(r, { ok: true, value: "vibecodedtools/vct-rl-reranker" });
});

Deno.test("validatePaidImageRepo: trims surrounding whitespace", () => {
  const r = validatePaidImageRepo("  org/image  ");
  assertEquals(r, { ok: true, value: "org/image" });
});

Deno.test("validatePaidImageRepo: accepts dots and underscores", () => {
  const r = validatePaidImageRepo("my.org/my_image");
  assertEquals(r, { ok: true, value: "my.org/my_image" });
});

Deno.test("validatePaidImageRepo: rejects undefined as empty", () => {
  const r = validatePaidImageRepo(undefined);
  assertEquals(r, { ok: false, reason: "empty" });
});

Deno.test("validatePaidImageRepo: rejects empty string as empty", () => {
  const r = validatePaidImageRepo("");
  assertEquals(r, { ok: false, reason: "empty" });
});

Deno.test("validatePaidImageRepo: rejects whitespace-only as whitespace_only", () => {
  const r = validatePaidImageRepo("   ");
  assertEquals(r, { ok: false, reason: "whitespace_only" });
});

Deno.test("validatePaidImageRepo: rejects missing slash", () => {
  const r = validatePaidImageRepo("just-an-image");
  assertEquals(r, { ok: false, reason: "no_slash" });
});

Deno.test("validatePaidImageRepo: rejects too many slashes", () => {
  const r = validatePaidImageRepo("a/b/c");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

Deno.test("validatePaidImageRepo: rejects URL-shaped value (defensive)", () => {
  // Common fat-finger: someone puts the full ghcr.io URL in the env var
  // instead of just the owner/image. The `://` chars are illegal in our
  // regex so this correctly falls back.
  const r = validatePaidImageRepo("https://ghcr.io/hotak92/vct-rl-reranker");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

Deno.test("validatePaidImageRepo: rejects empty owner half", () => {
  const r = validatePaidImageRepo("/image");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

Deno.test("validatePaidImageRepo: rejects empty image half", () => {
  const r = validatePaidImageRepo("owner/");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

// ─── validatePaidTag (pure) ──────────────────────────────────────────────

Deno.test("validatePaidTag: accepts semver", () => {
  const r = validatePaidTag("0.1.0");
  assertEquals(r, { ok: true, value: "0.1.0" });
});

Deno.test("validatePaidTag: accepts channel name (latest)", () => {
  const r = validatePaidTag("latest");
  assertEquals(r, { ok: true, value: "latest" });
});

Deno.test("validatePaidTag: accepts hyphenated pre-release", () => {
  const r = validatePaidTag("0.2.0-rc1");
  assertEquals(r, { ok: true, value: "0.2.0-rc1" });
});

Deno.test("validatePaidTag: trims surrounding whitespace", () => {
  const r = validatePaidTag("  0.1.0  ");
  assertEquals(r, { ok: true, value: "0.1.0" });
});

Deno.test("validatePaidTag: rejects undefined", () => {
  const r = validatePaidTag(undefined);
  assertEquals(r, { ok: false, reason: "empty" });
});

Deno.test("validatePaidTag: rejects empty string", () => {
  const r = validatePaidTag("");
  assertEquals(r, { ok: false, reason: "empty" });
});

Deno.test("validatePaidTag: rejects whitespace-only", () => {
  const r = validatePaidTag("   ");
  assertEquals(r, { ok: false, reason: "whitespace_only" });
});

Deno.test("validatePaidTag: rejects leading dot (Docker spec)", () => {
  const r = validatePaidTag(".0.1.0");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

Deno.test("validatePaidTag: rejects leading hyphen (Docker spec)", () => {
  const r = validatePaidTag("-rc1");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

Deno.test("validatePaidTag: rejects slash (would be repo, not tag)", () => {
  const r = validatePaidTag("0.1.0/extra");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

// ─── resolvePaidImageRepo (runtime) ──────────────────────────────────────

// Helper: capture console.warn calls into an array for the duration of a
// test body, then restore the original. Pattern from sibling
// validation_test.ts (no captured-warn tests there yet, but the harness
// allows it).
function withCapturedWarn(fn: () => void): string[] {
  const captured: string[] = [];
  const orig = console.warn;
  console.warn = (...args: unknown[]) => {
    captured.push(args.map((a) => String(a)).join(" "));
  };
  try {
    fn();
  } finally {
    console.warn = orig;
  }
  return captured;
}

Deno.test("resolvePaidImageRepo: returns default when env unset", () => {
  Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  const warnings = withCapturedWarn(() => {
    const r = resolvePaidImageRepo();
    assertEquals(r, PAID_IMAGE_REPO_DEFAULT);
  });
  // Unset case must NOT emit a warning (normal operation).
  assertEquals(warnings.length, 0);
});

Deno.test("resolvePaidImageRepo: returns env value when set + valid", () => {
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "vibecodedtools/vct-rl-reranker");
  try {
    const warnings = withCapturedWarn(() => {
      const r = resolvePaidImageRepo();
      assertEquals(r, "vibecodedtools/vct-rl-reranker");
    });
    assertEquals(warnings.length, 0);
  } finally {
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolvePaidImageRepo: falls back + WARNS on no-slash value", () => {
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "just-an-image");
  try {
    const warnings = withCapturedWarn(() => {
      const r = resolvePaidImageRepo();
      assertEquals(r, PAID_IMAGE_REPO_DEFAULT);
    });
    assertEquals(warnings.length, 1);
    assertStringIncludes(warnings[0], "GHCR_PAID_IMAGE_REPO");
    assertStringIncludes(warnings[0], "no_slash");
  } finally {
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolvePaidImageRepo: falls back + WARNS on whitespace-only", () => {
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "   ");
  try {
    const warnings = withCapturedWarn(() => {
      const r = resolvePaidImageRepo();
      assertEquals(r, PAID_IMAGE_REPO_DEFAULT);
    });
    assertEquals(warnings.length, 1);
    assertStringIncludes(warnings[0], "whitespace_only");
  } finally {
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolvePaidImageRepo: falls back + WARNS on empty string", () => {
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "");
  try {
    const warnings = withCapturedWarn(() => {
      const r = resolvePaidImageRepo();
      assertEquals(r, PAID_IMAGE_REPO_DEFAULT);
    });
    // Empty-string-set is distinguishable from unset (Deno.env.get
    // returns "" not undefined), so we DO warn — the user explicitly
    // set the variable, just to nothing. Helps debugging
    // "secret deployed but didn't take effect".
    assertEquals(warnings.length, 1);
    assertStringIncludes(warnings[0], "empty");
  } finally {
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolvePaidImageRepo: trims whitespace from valid env value", () => {
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "  org/image  ");
  try {
    const r = resolvePaidImageRepo();
    assertEquals(r, "org/image");
  } finally {
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

// ─── resolvePaidTagDefault (runtime) ─────────────────────────────────────

Deno.test("resolvePaidTagDefault: returns default when env unset", () => {
  Deno.env.delete("GHCR_PAID_TAG_DEFAULT");
  const warnings = withCapturedWarn(() => {
    const r = resolvePaidTagDefault();
    assertEquals(r, PAID_TAG_DEFAULT_FALLBACK);
  });
  assertEquals(warnings.length, 0);
});

Deno.test("resolvePaidTagDefault: returns env value when set + valid", () => {
  Deno.env.set("GHCR_PAID_TAG_DEFAULT", "0.2.0");
  try {
    const r = resolvePaidTagDefault();
    assertEquals(r, "0.2.0");
  } finally {
    Deno.env.delete("GHCR_PAID_TAG_DEFAULT");
  }
});

Deno.test("resolvePaidTagDefault: falls back + WARNS on whitespace-only", () => {
  Deno.env.set("GHCR_PAID_TAG_DEFAULT", "   ");
  try {
    const warnings = withCapturedWarn(() => {
      const r = resolvePaidTagDefault();
      assertEquals(r, PAID_TAG_DEFAULT_FALLBACK);
    });
    assertEquals(warnings.length, 1);
    assertStringIncludes(warnings[0], "GHCR_PAID_TAG_DEFAULT");
    assertStringIncludes(warnings[0], "whitespace_only");
  } finally {
    Deno.env.delete("GHCR_PAID_TAG_DEFAULT");
  }
});

Deno.test("resolvePaidTagDefault: falls back + WARNS on invalid chars", () => {
  Deno.env.set("GHCR_PAID_TAG_DEFAULT", "0.1.0/extra");
  try {
    const warnings = withCapturedWarn(() => {
      const r = resolvePaidTagDefault();
      assertEquals(r, PAID_TAG_DEFAULT_FALLBACK);
    });
    assertEquals(warnings.length, 1);
    assertStringIncludes(warnings[0], "invalid_chars");
  } finally {
    Deno.env.delete("GHCR_PAID_TAG_DEFAULT");
  }
});

// ─── Default-value sanity ────────────────────────────────────────────────
//
// Locks the v0.2.35-shipped defaults. Tripwire: if someone bumps these
// values during an org migration, this test fails and forces a deliberate
// update (the migration story is to set the env var, NOT bump the
// hardcoded fallback — that keeps forks that re-deploy without secrets
// from breaking).

Deno.test("PAID_IMAGE_REPO_DEFAULT: locked to v0.2.35 personal-account value", () => {
  assertEquals(PAID_IMAGE_REPO_DEFAULT, "hotak92/vct-rl-reranker");
});

Deno.test("PAID_TAG_DEFAULT_FALLBACK: locked to v0.2.35 0.1.0", () => {
  assertEquals(PAID_TAG_DEFAULT_FALLBACK, "0.1.0");
});

// ─── validateGhcrUsername (pure) ─────────────────────────────────────────

Deno.test("validateGhcrUsername: accepts canonical login", () => {
  const r = validateGhcrUsername("vct-bot-rl");
  assertEquals(r, { ok: true, value: "vct-bot-rl" });
});

Deno.test("validateGhcrUsername: accepts alphanumeric", () => {
  const r = validateGhcrUsername("hotak92");
  assertEquals(r, { ok: true, value: "hotak92" });
});

Deno.test("validateGhcrUsername: trims surrounding whitespace", () => {
  const r = validateGhcrUsername("  vct-bot-rl  ");
  assertEquals(r, { ok: true, value: "vct-bot-rl" });
});

Deno.test("validateGhcrUsername: rejects undefined", () => {
  const r = validateGhcrUsername(undefined);
  assertEquals(r, { ok: false, reason: "empty" });
});

Deno.test("validateGhcrUsername: rejects empty string", () => {
  const r = validateGhcrUsername("");
  assertEquals(r, { ok: false, reason: "empty" });
});

Deno.test("validateGhcrUsername: rejects whitespace-only", () => {
  const r = validateGhcrUsername("   ");
  assertEquals(r, { ok: false, reason: "whitespace_only" });
});

Deno.test("validateGhcrUsername: rejects slashes", () => {
  const r = validateGhcrUsername("owner/image");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

Deno.test("validateGhcrUsername: rejects @ and dots", () => {
  assertEquals(validateGhcrUsername("a@b").ok, false);
  assertEquals(validateGhcrUsername("a.b").ok, false);
});

Deno.test("validateGhcrUsername: rejects spaces", () => {
  const r = validateGhcrUsername("vct bot");
  assertEquals(r, { ok: false, reason: "invalid_chars" });
});

// ─── resolveGhcrUsername (env-driven) ────────────────────────────────────

function captureWarn(): { restore: () => void; messages: () => string[] } {
  const original = globalThis.console.warn;
  const captured: string[] = [];
  globalThis.console.warn = (...args: unknown[]) => {
    captured.push(args.map(String).join(" "));
  };
  return {
    restore: () => {
      globalThis.console.warn = original;
    },
    messages: () => captured,
  };
}

Deno.test("resolveGhcrUsername: prefers GHCR_USERNAME when set", () => {
  Deno.env.set("GHCR_USERNAME", "vct-bot-rl");
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "hotak92/vct-rl-reranker");
  try {
    assertEquals(resolveGhcrUsername(), "vct-bot-rl");
  } finally {
    Deno.env.delete("GHCR_USERNAME");
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolveGhcrUsername: per-module bot scenario (username differs from package owner)", () => {
  // The load-bearing v0.2.36 case: package lives at hotak92/vct-rl-reranker
  // but the PAT belongs to vct-bot-rl. Login MUST use vct-bot-rl or GHCR
  // returns 403.
  Deno.env.set("GHCR_USERNAME", "vct-bot-rl");
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "hotak92/vct-rl-reranker");
  try {
    const username = resolveGhcrUsername();
    const repoOwner = "hotak92";
    assertEquals(username, "vct-bot-rl");
    assertEquals(username === repoOwner, false);
  } finally {
    Deno.env.delete("GHCR_USERNAME");
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolveGhcrUsername: falls back to owner-half when GHCR_USERNAME unset", () => {
  Deno.env.delete("GHCR_USERNAME");
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "myorg/my-image");
  try {
    assertEquals(resolveGhcrUsername(), "myorg");
  } finally {
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolveGhcrUsername: backwards-compat with v0.2.35 default", () => {
  // Both env vars unset → falls back to default repo "hotak92/vct-rl-reranker"
  // → username = "hotak92". This is the pre-bot-user behaviour preserved
  // for in-flight migrations.
  Deno.env.delete("GHCR_USERNAME");
  Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  assertEquals(resolveGhcrUsername(), "hotak92");
});

Deno.test("resolveGhcrUsername: warns + falls back on malformed value", () => {
  const warn = captureWarn();
  Deno.env.set("GHCR_USERNAME", "invalid user with spaces");
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "owner/image");
  try {
    assertEquals(resolveGhcrUsername(), "owner");
    const messages = warn.messages();
    assertEquals(messages.length >= 1, true);
    assertStringIncludes(messages[0], "GHCR_USERNAME malformed");
    assertStringIncludes(messages[0], "invalid_chars");
  } finally {
    warn.restore();
    Deno.env.delete("GHCR_USERNAME");
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolveGhcrUsername: silent fallback when GHCR_USERNAME unset (normal case)", () => {
  const warn = captureWarn();
  Deno.env.delete("GHCR_USERNAME");
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "owner/image");
  try {
    assertEquals(resolveGhcrUsername(), "owner");
    // Unset is normal; should NOT warn (warning is for fat-finger / typo).
    assertEquals(warn.messages().length, 0);
  } finally {
    warn.restore();
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});

Deno.test("resolveGhcrUsername: warns on empty-string explicit set", () => {
  const warn = captureWarn();
  Deno.env.set("GHCR_USERNAME", "");
  Deno.env.set("GHCR_PAID_IMAGE_REPO", "owner/image");
  try {
    assertEquals(resolveGhcrUsername(), "owner");
    // Empty-string set is treated as unset (raw === "" → reason "empty"
    // but raw !== undefined → warning fires). Implementation choice:
    // explicit empty-string IS a fat-finger worth flagging, distinct
    // from "never set the var at all".
    assertEquals(warn.messages().length, 1);
  } finally {
    warn.restore();
    Deno.env.delete("GHCR_USERNAME");
    Deno.env.delete("GHCR_PAID_IMAGE_REPO");
  }
});
