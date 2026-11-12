// SPDX-License-Identifier: AGPL-3.0-or-later
// Shared HTTP response helpers for VCO Supabase edge functions.
//
// v0.2.54 Track H (H-7): before this module, the `CORS_HEADERS` constant
// and the `jsonResponse` helper were copy-pasted into SEVEN functions
// (rl-artifact-url, rl-latest-version, rl-latest-weights, module-catalog,
// telemetry, rebind-admin-token, validate-tier) with small per-function
// variations (allowed methods, allowed headers, cache hints, preflight
// max-age). Any security fix to the response path needed 7 applications.
//
// Design: parametric rather than uniform. The per-function differences
// are INTENTIONAL wire contracts (module-catalog sends a Cache-Control
// hint, validate-tier/rebind-admin-token send a preflight max-age and a
// narrower header allowlist), so the builder takes them as options and
// each function declares its exact previous headers — byte-for-byte
// wire-compatible with the pre-extraction copies.

/** The allow-list most functions used (Supabase client conventions). */
export const DEFAULT_ALLOW_HEADERS =
  "authorization, x-client-info, apikey, content-type";

/**
 * Build a CORS header map. `Access-Control-Allow-Origin` is always `*`:
 * every edge function here authenticates via request-body credentials
 * (license_key) or none at all, and callers are desktop launchers with
 * arbitrary origins — wildcard is the deliberate policy (see the
 * validate-tier comment that documented this pre-extraction).
 */
export function buildCorsHeaders(opts: {
  /** e.g. "POST, OPTIONS" or "GET, OPTIONS". */
  methods: string;
  /** Override the allowed request headers (default: DEFAULT_ALLOW_HEADERS). */
  allowHeaders?: string;
  /** Extra response headers merged in verbatim (e.g. Cache-Control,
   *  Access-Control-Max-Age). */
  extra?: Record<string, string>;
}): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": opts.allowHeaders ?? DEFAULT_ALLOW_HEADERS,
    "Access-Control-Allow-Methods": opts.methods,
    ...(opts.extra ?? {}),
  };
}

/**
 * Build the `jsonResponse(body, status?)` helper every function used,
 * closed over its CORS headers. Same body as all 7 pre-extraction
 * copies: JSON-stringified body, given status, CORS + JSON content type.
 */
export function makeJsonResponse(
  corsHeaders: Record<string, string>,
): (body: unknown, status?: number) => Response {
  return (body: unknown, status = 200): Response =>
    new Response(JSON.stringify(body), {
      status,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
}
