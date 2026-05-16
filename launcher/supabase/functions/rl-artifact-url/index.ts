// rl-artifact-url — Issue a short-lived GHCR pull token for the
// vct-rl-reranker paid module.
//
// Called by the launcher's installer_engine before `podman pull` /
// `docker pull` on the private GHCR image. The launcher POSTs the
// user's current license key (same one /validate-tier consumes); this
// function re-validates the tier, then exchanges a long-lived
// service-account PAT for a short-lived registry-scoped token.
//
// The registry token is what the launcher actually feeds to `podman
// login --password-stdin`. It carries:
//   - scope=repository:hotak92/vct-rl-reranker:pull (READ-ONLY pull,
//     no push, no other repos)
//   - 15-minute TTL (or whatever GHCR returns — typically 5-30 min)
//
// Why GHCR's token-exchange instead of returning the service-account
// PAT directly:
//   1. Limits blast radius. A leaked exchange token is read-scoped
//      AND short-lived; a leaked service PAT would give attackers
//      org-wide package read access for its full lifetime.
//   2. Defense in depth — even if our Supabase env leaks, the
//      service-account PAT alone doesn't enable image pulls (the
//      exchange step is server-side gated on tier validation).
//
// Request:
//   POST /functions/v1/rl-artifact-url
//   Body: {
//     license_key: string (UUID — same shape as /validate-tier),
//     machine_id_hash: string (sha256 hex — bound to current install)
//   }
//
// Response (200, success):
//   {
//     image: "ghcr.io/hotak92/vct-rl-reranker",
//     tag: "0.1.0",
//     registry: "ghcr.io",
//     pull_token: "<short-lived-registry-token>",
//     expires_in_s: 900,
//     expires_at: "2026-05-16T20:00:00.000Z"
//   }
//
// Response (401):
//   { error: "tier_insufficient", required_tier: "pro", got: "free" }
//   { error: "license_invalid" }
//   { error: "license_expired" }
//
// Response (500):
//   { error: "Service misconfigured" } — runtime env missing
//   { error: "registry_token_exchange_failed", detail: "..." } — GHCR API blew up
//
// ────────────────────────────────────────────────────────────────────────────

import {
  type OrchestratorTier,
} from "../_shared/variant_map.ts";
import {
  REQUIRED_TIER,
  type RequestBody,
  tierMeetsRequirement,
  tokenPreview,
  validateRequestBody,
} from "./validation.ts";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// Paid-module manifest pins. Kept here (rather than in the manifest body
// the client sent) so a malicious client can't request a token for a
// different image. The function only ever issues tokens for THIS image.
const PAID_IMAGE_REPO = "hotak92/vct-rl-reranker";
const PAID_IMAGE_FULL = `ghcr.io/${PAID_IMAGE_REPO}`;
const PAID_TAG_DEFAULT = "0.1.0"; // bumped by CD on each release

// GHCR token-exchange parameters.
const GHCR_TOKEN_URL = "https://ghcr.io/token";
const GHCR_AUTH_HEADER_PREFIX = "Bearer ";
const TOKEN_TTL_SECONDS = 900; // 15 min — what we promise the client

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

/**
 * Re-validate the license via Supabase. We trust /validate-tier to be
 * the single source of truth for tier mapping — calling its function
 * directly (server-to-server) avoids duplicating the Lemon Squeezy
 * logic here. The launcher could in theory pass us a stale cache
 * (this WOULD bypass /validate-tier on the client side), so we MUST
 * re-call /validate-tier from inside this function before issuing a
 * registry token.
 */
async function revalidateTierViaSupabase(
  body: RequestBody,
): Promise<{ valid: boolean; tier: OrchestratorTier; reason?: string }> {
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return { valid: false, tier: "free", reason: "service_misconfigured" };
  }

  const url = `${supabaseUrl}/functions/v1/validate-tier`;
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        // Service-role key authorizes us to call validate-tier
        // server-side without needing a publishable anon key.
        Authorization: `Bearer ${serviceRoleKey}`,
      },
      body: JSON.stringify({
        license_key: body.license_key,
        machine_id_hash: body.machine_id_hash,
      }),
      signal: AbortSignal.timeout(8000),
    });
  } catch (e) {
    return {
      valid: false,
      tier: "free",
      reason: `validate-tier_unreachable: ${String(e).slice(0, 200)}`,
    };
  }

  if (!resp.ok) {
    return {
      valid: false,
      tier: "free",
      reason: `validate-tier_${resp.status}`,
    };
  }

  let parsed: { valid?: boolean; tier?: OrchestratorTier };
  try {
    parsed = await resp.json();
  } catch (e) {
    return {
      valid: false,
      tier: "free",
      reason: `validate-tier_parse: ${String(e).slice(0, 200)}`,
    };
  }

  if (!parsed.valid || !parsed.tier) {
    return { valid: false, tier: "free", reason: "validate-tier_rejected" };
  }
  return { valid: true, tier: parsed.tier };
}

/**
 * Exchange the GHCR service-account PAT for a short-lived,
 * repository-scoped pull token.
 *
 * The /token endpoint accepts Basic auth (PAT as password, username
 * ignored for the *_FORTHCOMING flow) and returns:
 *   { "token": "<jwt-like>", "expires_in": 300, "issued_at": "..." }
 *
 * Scope syntax per the Docker registry v2 spec:
 *   repository:<owner>/<image>:<actions>
 * For pull-only access we request `repository:hotak92/vct-rl-reranker:pull`.
 */
async function exchangeForRegistryToken(): Promise<
  { token: string; expires_in_s: number } | { error: string; detail: string }
> {
  const servicePat = Deno.env.get("GHCR_SERVICE_PAT");
  if (!servicePat) {
    return {
      error: "registry_token_exchange_failed",
      detail: "GHCR_SERVICE_PAT not configured in edge function env",
    };
  }

  const scope = `repository:${PAID_IMAGE_REPO}:pull`;
  const url =
    `${GHCR_TOKEN_URL}?service=ghcr.io&scope=${encodeURIComponent(scope)}`;

  // GHCR accepts the PAT directly via Authorization: Bearer.
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "GET",
      headers: { Authorization: `${GHCR_AUTH_HEADER_PREFIX}${servicePat}` },
      signal: AbortSignal.timeout(8000),
    });
  } catch (e) {
    return {
      error: "registry_token_exchange_failed",
      detail: `fetch: ${String(e).slice(0, 200)}`,
    };
  }

  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    return {
      error: "registry_token_exchange_failed",
      detail: `ghcr ${resp.status}: ${body.slice(0, 200)}`,
    };
  }

  type GhcrTokenResp = { token?: string; expires_in?: number };
  let parsed: GhcrTokenResp;
  try {
    parsed = await resp.json();
  } catch (e) {
    return {
      error: "registry_token_exchange_failed",
      detail: `parse: ${String(e).slice(0, 200)}`,
    };
  }

  if (!parsed.token) {
    return {
      error: "registry_token_exchange_failed",
      detail: "ghcr response missing 'token' field",
    };
  }

  return {
    token: parsed.token,
    // GHCR may return a different TTL; we cap our promise at our
    // policy minimum (15min) but accept anything ≥30s as usable.
    expires_in_s: Math.min(parsed.expires_in ?? TOKEN_TTL_SECONDS, TOKEN_TTL_SECONDS),
  };
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  let rawBody: unknown;
  try {
    rawBody = await req.json();
  } catch (_) {
    return jsonResponse({ error: "invalid_json_body" }, 400);
  }

  const validationError = validateRequestBody(rawBody);
  if (validationError !== null) {
    return jsonResponse({ error: validationError }, 400);
  }
  const body = rawBody as RequestBody;

  // ─── Re-validate tier ──────────────────────────────────────────────────
  const tierCheck = await revalidateTierViaSupabase(body);
  if (!tierCheck.valid) {
    console.log(
      `[rl-artifact-url] tier check failed: ${tierCheck.reason ?? "unknown"}`,
    );
    return jsonResponse(
      {
        error: "license_invalid",
        detail: tierCheck.reason,
      },
      401,
    );
  }
  if (!tierMeetsRequirement(tierCheck.tier, REQUIRED_TIER)) {
    return jsonResponse(
      {
        error: "tier_insufficient",
        required_tier: REQUIRED_TIER,
        got: tierCheck.tier,
      },
      401,
    );
  }

  // ─── Issue registry-scoped pull token ──────────────────────────────────
  const tokenResult = await exchangeForRegistryToken();
  if ("error" in tokenResult) {
    console.error(
      `[rl-artifact-url] token exchange failed: ${tokenResult.detail}`,
    );
    return jsonResponse(
      {
        error: tokenResult.error,
        detail: tokenResult.detail,
      },
      500,
    );
  }

  const now = Date.now();
  const expiresAt = new Date(now + tokenResult.expires_in_s * 1000)
    .toISOString();

  // Log success WITHOUT the token contents (PII / secret). Token tag
  // is a deterministic 8-char prefix of the SHA-256 — enough for
  // observability cross-correlation without leaking the token itself.
  const tokenTag = await tokenPreview(tokenResult.token);
  console.log(
    `[rl-artifact-url] OK tier=${tierCheck.tier} token=${tokenTag}* ` +
      `ttl=${tokenResult.expires_in_s}s expires_at=${expiresAt}`,
  );

  return jsonResponse({
    image: PAID_IMAGE_FULL,
    tag: PAID_TAG_DEFAULT,
    registry: "ghcr.io",
    pull_token: tokenResult.token,
    expires_in_s: tokenResult.expires_in_s,
    expires_at: expiresAt,
  });
});
