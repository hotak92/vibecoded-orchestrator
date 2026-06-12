// SPDX-License-Identifier: AGPL-3.0-or-later
//
// rebind-admin-token — Explicit machine-rebind for Vault-token admin
// licenses (v0.2.36).
//
// Closes a v0.2.35-deferred recovery gap: when an admin reinstalls
// their OS or swaps laptop, `/validate-tier` returns `machine_mismatch`
// (TOFU binding holds the old machine_id_hash) and the only escape
// was for the project owner to manually edit the Vault secret over SQL.
//
// This endpoint lets the admin self-serve the rebind from the launcher
// GUI (Settings → License → "Rebind to this machine") as long as they
// still possess the original `vct_admin_*` token. Possession of the
// token IS the authorization — same security model `/validate-tier`
// already trusts.
//
// Request:
//   POST /functions/v1/rebind-admin-token
//   Body: {
//     license_key: string (vct_admin_ prefix; the same token used at
//                  /validate-tier),
//     new_machine_id_hash: string (sha256 hex — 64 chars)
//   }
//
// Response (200, success):
//   {
//     success: true,
//     user: "<admin-identifier>",
//     rebound_at: "2026-05-26T14:22:00.000Z"
//   }
//
// Response (401):
//   { error: "license_invalid" }      — token shape OK but doesn't match Vault
//   { error: "service_misconfigured" } — SUPABASE_URL or SERVICE_ROLE_KEY missing
//   { error: "rebind_failed", detail: "..." } — RPC returned false
//
// Response (400):
//   { error: "license_key_invalid_format" }
//   { error: "machine_id_hash_invalid_format" }
//
// Same body-level auth pattern as the other admin-license endpoints —
// `verify_jwt = false` in config.toml. No JWT, no anon key required;
// the auth boundary is the token match against the Vault map.

import {
  appendAdminAuthLog,
  fetchVaultAdminTokensJson,
  lookupVaultAdminTokenUser,
  rebindVaultAdminMachine,
} from "../_shared/variant_map.ts";
import { buildCorsHeaders, makeJsonResponse } from "../_shared/http.ts";
import { validateRequestBody } from "./validation.ts";

const CORS_HEADERS = buildCorsHeaders({
  methods: "POST, OPTIONS",
  allowHeaders: "Content-Type, Authorization",
  extra: { "Access-Control-Max-Age": "86400" },
});
const jsonResponse = makeJsonResponse(CORS_HEADERS);

/** Safe license-key fragment for logs. Never log the full token. */
function maskToken(key: string): string {
  return key.slice(0, 12) + "…";
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  // ── Parse + validate body ───────────────────────────────────────────────
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ error: "license_key_invalid_format" }, 400);
  }

  const validationError = validateRequestBody(body);
  if (validationError) {
    return jsonResponse({ error: validationError }, 400);
  }
  const { license_key, new_machine_id_hash } = body as {
    license_key: string;
    new_machine_id_hash: string;
  };
  const tokenTag = maskToken(license_key);

  // ── Env check (fail fast on misconfiguration) ───────────────────────────
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    console.error(
      "[rebind-admin-token] FATAL: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing",
    );
    return jsonResponse({ error: "service_misconfigured" }, 401);
  }

  // ── Resolve token → user via constant-time compare ──────────────────────
  const vaultJson = await fetchVaultAdminTokensJson(supabaseUrl, serviceRoleKey);
  const user = lookupVaultAdminTokenUser(license_key, vaultJson);

  if (user === null) {
    console.warn(`[rebind-admin-token] license_invalid token=${tokenTag}`);
    return jsonResponse({ error: "license_invalid" }, 401);
  }

  // ── Rebind via SECURITY DEFINER RPC ─────────────────────────────────────
  const rebound = await rebindVaultAdminMachine(
    supabaseUrl,
    serviceRoleKey,
    user,
    new_machine_id_hash,
  );

  if (!rebound) {
    console.error(
      `[rebind-admin-token] rebind_failed user=${user} token=${tokenTag}`,
    );
    return jsonResponse(
      {
        error: "rebind_failed",
        detail:
          "Vault RPC returned false (user removed concurrently, vault secret missing, or RPC error). " +
          "Contact the project owner.",
      },
      401,
    );
  }

  const reboundAt = new Date().toISOString();

  // ── Audit log (non-blocking) ────────────────────────────────────────────
  appendAdminAuthLog(supabaseUrl, serviceRoleKey, {
    admin_user: user,
    machine_id_hash: new_machine_id_hash,
    outcome: "rebind",
  }).catch(() => {/* non-blocking */});

  console.log(`[rebind-admin-token] OK user=${user} token=${tokenTag}`);

  return jsonResponse({
    success: true,
    user,
    rebound_at: reboundAt,
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Response shape reference
// ────────────────────────────────────────────────────────────────────────────
//
// 200 — rebind successful:
//   { success: true, user: "<id>", rebound_at: "ISO-8601" }
//
// 400 — malformed request:
//   { error: "license_key_invalid_format" }
//   { error: "machine_id_hash_invalid_format" }
//
// 401 — auth / config failures:
//   { error: "license_invalid" }
//   { error: "service_misconfigured" }
//   { error: "rebind_failed", detail: "..." }
//
// 405 — wrong method:
//   { error: "Method not allowed" }
