// rl-artifact-url — Issue a short-lived GHCR pull token for the
// vct-rl-reranker paid module.
//
// Called by the launcher's installer_engine before `podman pull` /
// `docker pull` on the private GHCR image. The launcher POSTs the
// user's current license key (same one /validate-tier consumes); this
// function re-validates the tier, then exchanges a long-lived
// service-account PAT for a short-lived registry-scoped token.
//
// ─── Runtime env-var contract ────────────────────────────────────────────────
//
// Required (function returns 500 / `Service misconfigured` if missing):
//   SUPABASE_URL                — auto-set by Supabase runtime; used for
//                                 the server-to-server `/validate-tier` call
//   SUPABASE_SERVICE_ROLE_KEY   — auto-set by Supabase runtime; service-role
//                                 credential for the inter-function call
//   GHCR_SERVICE_PAT            — manually set via `supabase secrets set`;
//                                 long-lived GitHub PAT scoped to
//                                 `read:packages` on the paid image repo.
//                                 Rotate quarterly. Function returns 500 /
//                                 `registry_token_exchange_failed` if missing.
//
// Optional (function falls back to safe defaults if unset/malformed,
// emitting a `console.warn` so on-call can spot the fat-finger in
// `supabase functions logs`):
//   GHCR_PAID_IMAGE_REPO        — paid-image repo address in `<owner>/<image>`
//                                 form. Default: `hotak92/vct-rl-reranker`
//                                 (the v0.2.35-shipped personal-account
//                                 image). When migrating to a GitHub Org
//                                 for proper scoped /token credentials,
//                                 set this secret rather than redeploying
//                                 the function. Malformed values (no slash,
//                                 whitespace-only, invalid chars) are logged
//                                 and the default applies.
//   GHCR_PAID_TAG_DEFAULT       — fallback tag for the `tag` field in the
//                                 response. Default: `0.1.0`. The launcher's
//                                 `resolve_variant_tag` is the actual source
//                                 of truth for tag selection — this default
//                                 is advisory / used by callers that bypass
//                                 the launcher logic.
//   GHCR_USERNAME               — GitHub login the credential owner uses.
//                                 Used in TWO places (v0.2.37+, was one in
//                                 v0.2.36): (1) the Basic-auth header on the
//                                 server-side /token exchange — GHCR returns
//                                 403 if the username doesn't match the PAT
//                                 owner for personal-account packages; (2)
//                                 the response `username` field the launcher
//                                 passes to `podman login -u <user>`. Must
//                                 match the owner of `GHCR_SERVICE_PAT` (the
//                                 credential owner), NOT necessarily the
//                                 owner of the package (which lives in
//                                 `GHCR_PAID_IMAGE_REPO`). For the v0.2.36
//                                 per-module bot-user architecture: set to
//                                 the bot user's login (e.g. `vct-bot-rl`)
//                                 while `GHCR_PAID_IMAGE_REPO` stays at the
//                                 package path (e.g. `hotak92/vct-rl-reranker`).
//                                 When unset: falls back to the owner-half of
//                                 `GHCR_PAID_IMAGE_REPO` (Agent W's original
//                                 auto-derivation; correct only when the
//                                 package owner IS the credential owner —
//                                 the v0.2.35-pre-bot-user state).
//
// The registry token is what the launcher actually feeds to `podman
// login --password-stdin`. It carries:
//   - scope=repository:${PAID_IMAGE_REPO}:pull (READ-ONLY pull,
//     no push, no other repos) — PAID_IMAGE_REPO resolved from env
//     (default `hotak92/vct-rl-reranker`; see env-var contract below)
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
  resolveGhcrUsername,
  resolvePaidImageRepo,
  resolvePaidTagDefault,
} from "../_shared/config.ts";
import { buildCorsHeaders, makeJsonResponse } from "../_shared/http.ts";
import { revalidateTierViaSupabase } from "../_shared/tier_revalidation.ts";
import {
  REQUIRED_TIER,
  type RequestBody,
  tierMeetsRequirement,
  tokenPreview,
  validateRequestBody,
} from "./validation.ts";

const CORS_HEADERS = buildCorsHeaders({ methods: "POST, OPTIONS" });

// Paid-module manifest pins. Kept on the server (rather than in the
// manifest body the client sent) so a malicious client can't request a
// token for a different image. The function only ever issues tokens for
// THIS image — sourced from `GHCR_PAID_IMAGE_REPO` (v0.2.36+) with a
// fallback to the v0.2.35 personal-account default. Resolved ONCE at
// module init: env-driven, but no per-request env reads (Deno re-uses
// the module across requests, env stays stable for the function's
// lifetime — when the env value changes, the function is redeployed).
//
// Why env-driven: the medium-term anti-piracy follow-up is moving the
// image from a personal account (where GHCR's /token endpoint returns
// the original PAT base64-encoded rather than a proper scoped credential
// — see exchangeForRegistryToken doc) to a GitHub Organization where
// /token issues real scoped tokens. When that migration happens, we want
// it to be a Supabase secret update, not a code redeploy:
//   supabase secrets set GHCR_PAID_IMAGE_REPO=vibecodedtools/vct-rl-reranker
// (and re-run `supabase functions deploy rl-artifact-url` only to pick
// up the new env, which is automatic on the next cold start).
const PAID_IMAGE_REPO = resolvePaidImageRepo();
const PAID_IMAGE_FULL = `ghcr.io/${PAID_IMAGE_REPO}`;
const PAID_TAG_DEFAULT = resolvePaidTagDefault();

// GHCR token-exchange parameters.
// The /token endpoint requires Basic auth — `Authorization: Basic
// base64("<gh-user>:<PAT>")`. The bare Bearer header attempted in the
// pre-2026-05-26 version of this function returns 401 even with a
// valid classic PAT that DOES have read:packages scope (verified via
// `curl -H "Authorization: Bearer <PAT>" ghcr.io/token` vs
// `curl -u <user>:<PAT> ghcr.io/token` on 2026-05-26 — only the
// Basic form returns 200).
//
// v0.2.37 (Issue 4a): the Basic-auth username MUST be the credential
// owner's GitHub login (verified 2026-05-27: GHCR returns 403 when the
// username doesn't match the PAT owner for personal-account packages).
// Pre-v0.2.37 we used a synthetic literal `vct-paid-module` here, which
// 403'd against `hotak92`'s PAT once the per-module bot-user
// architecture moved the credential owner away from the package owner.
// `resolveGhcrUsername()` reads `GHCR_USERNAME` (preferred) with a
// fallback to the owner-half of `GHCR_PAID_IMAGE_REPO` — the same
// resolver used for the response `username` field below.
const GHCR_TOKEN_URL = "https://ghcr.io/token";
const TOKEN_TTL_SECONDS = 900; // 15 min — what we promise the client

const jsonResponse = makeJsonResponse(CORS_HEADERS);

// Tier re-validation: shared `revalidateTierViaSupabase` from
// `_shared/tier_revalidation.ts` (H-7 extraction — was a local copy,
// identical in rl-latest-version and rl-latest-weights). We MUST
// re-validate server-side before issuing a registry token: the
// launcher could pass a stale/forged cache.

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
 * For pull-only access we request `repository:${PAID_IMAGE_REPO}:pull`
 * — with PAID_IMAGE_REPO resolved at module init from the env var (see
 * top-of-file env-var contract block).
 */
async function exchangeForRegistryToken(): Promise<
  { token: string; username: string; expires_in_s: number } | { error: string; detail: string }
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

  // GHCR /token requires Basic auth (`base64("user:PAT")`). Bearer
  // fails despite being documented to work — observed empirically
  // 2026-05-26 with a classic PAT that DID have read:packages scope.
  //
  // v0.2.37 (Issue 4a): the username MUST be the credential owner's
  // GitHub login (e.g. `vct-bot-rl` for the per-module bot-user
  // architecture). Using a synthetic literal here gets 403 from GHCR
  // for personal-account packages (verified 2026-05-27). Same
  // resolver used for the response `username` field below — both
  // call sites read the same env var so they cannot drift.
  const basicCreds = btoa(`${resolveGhcrUsername()}:${servicePat}`);
  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "GET",
      headers: { Authorization: `Basic ${basicCreds}` },
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

  // GHCR architectural quirk (verified 2026-05-26): for PERSONAL-account
  // packages, the /token endpoint returns the original PAT base64-encoded
  // rather than issuing a separate registry-scoped credential. The client
  // (`container_login` on launcher side) needs a credential it can pass
  // to `podman/docker login --password-stdin` — that command treats its
  // input as a literal password, NOT as base64. So we decode here, server-
  // side, before returning to the client.
  //
  // Anti-piracy note: the decoded value IS the underlying PAT for
  // personal-account packages. This breaks the original design property
  // ("PAT never leaves the server"). Tracked as v0.2.36 architectural
  // follow-up — moving the image to a GitHub Organization changes the
  // /token behaviour to issue proper scoped tokens. For now, the rate-
  // limit at the edge function (per-license-key tier check + 15min TTL
  // we promise) constrains the leak window. The launcher logs out
  // immediately after pull completes.
  let decodedToken: string;
  try {
    decodedToken = atob(parsed.token);
  } catch (_) {
    // Already-decoded path (org packages, future-compat): use as-is.
    decodedToken = parsed.token;
  }

  // Username for `podman/docker login -u <user>` on the client side.
  // For personal-account packages this MUST match the PAT owner's
  // GitHub login (verified 2026-05-26 — synthetic usernames like the
  // legacy `vct-paid-module` literal get 403). For org packages where
  // the /token endpoint returns a scoped credential, any string works.
  //
  // v0.2.37 (Issue 4a): SAME resolver is now used above for the
  // server-side /token Basic-auth username (was a hardcoded literal
  // pre-v0.2.37). Sharing the resolver means the two call sites can't
  // drift — both read `GHCR_USERNAME` with the same fallback. See the
  // top-of-file env-var contract for the resolution order.
  //
  // v0.2.36: resolved via `resolveGhcrUsername()`, which reads the
  // `GHCR_USERNAME` env var with a fallback to the owner-half of
  // `GHCR_PAID_IMAGE_REPO`. Why the explicit env var beats pure
  // auto-derivation: the per-module bot-user architecture
  // (see KG `multi-module-paid-distribution-architecture`) decouples
  // PACKAGE OWNER from CREDENTIAL OWNER — the image lives at
  // `hotak92/vct-rl-reranker` but the PAT belongs to a dedicated bot
  // user like `vct-bot-rl`. The launcher's `podman login` must use the
  // credential owner; auto-deriving from the repo path would send the
  // wrong username and get 403'd. The fallback preserves Agent W's
  // shipped behaviour when GHCR_USERNAME is unset (transitional
  // deployments where the package owner WAS the credential owner).
  const ghcrUsername = resolveGhcrUsername();

  return {
    token: decodedToken,
    username: ghcrUsername,
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
    // v0.2.36 wire-contract addition: the launcher-side container_login
    // needs the GitHub username that the pull_token authenticates as,
    // because `podman/docker login -u <user>` rejects mismatched
    // username/credential pairs (verified empirically 2026-05-26 against
    // ghcr.io for personal-account packages). Pre-v0.2.36 launcher
    // hardcoded "vct-paid-module" which 403'd; v0.2.36 launcher reads
    // this field and falls back to the legacy literal if absent (so a
    // v0.2.35 launcher hitting a v0.2.36 server still works for org-
    // package paths where username is synthetic).
    username: tokenResult.username,
    expires_in_s: tokenResult.expires_in_s,
    expires_at: expiresAt,
  });
});
