// SPDX-License-Identifier: AGPL-3.0-or-later
// Tests for _shared/http.ts (H-7 extraction).
//
// Pins the wire-compatibility contract: each consuming function's
// post-extraction headers must be byte-identical to its pre-extraction
// copy. Run: deno test --no-check launcher/supabase/functions/_shared/http_test.ts

import {
  buildCorsHeaders,
  DEFAULT_ALLOW_HEADERS,
  makeJsonResponse,
} from "./http.ts";

function assertEquals(actual: unknown, expected: unknown, msg?: string) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) {
    throw new Error(msg ?? `expected ${e}, got ${a}`);
  }
}

Deno.test("default shape matches the rl-*/telemetry pre-extraction copies", () => {
  assertEquals(buildCorsHeaders({ methods: "POST, OPTIONS" }), {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": DEFAULT_ALLOW_HEADERS,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
  });
});

Deno.test("module-catalog shape: GET + Cache-Control extra", () => {
  assertEquals(
    buildCorsHeaders({
      methods: "GET, OPTIONS",
      extra: { "Cache-Control": "public, max-age=60" },
    }),
    {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": DEFAULT_ALLOW_HEADERS,
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Cache-Control": "public, max-age=60",
    },
  );
});

Deno.test("validate-tier/rebind shape: narrow allowlist + preflight max-age", () => {
  assertEquals(
    buildCorsHeaders({
      methods: "POST, OPTIONS",
      allowHeaders: "Content-Type, Authorization",
      extra: { "Access-Control-Max-Age": "86400" },
    }),
    {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Max-Age": "86400",
    },
  );
});

Deno.test("makeJsonResponse: status, content type, CORS, body", async () => {
  const cors = buildCorsHeaders({ methods: "POST, OPTIONS" });
  const jsonResponse = makeJsonResponse(cors);

  const ok = jsonResponse({ ok: true });
  assertEquals(ok.status, 200);
  assertEquals(ok.headers.get("Content-Type"), "application/json");
  assertEquals(ok.headers.get("Access-Control-Allow-Origin"), "*");
  assertEquals(await ok.json(), { ok: true });

  const err = jsonResponse({ error: "tier_insufficient" }, 401);
  assertEquals(err.status, 401);
  assertEquals(await err.json(), { error: "tier_insufficient" });
});
