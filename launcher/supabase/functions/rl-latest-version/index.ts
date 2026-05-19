// rl-latest-version — Tell the launcher whether a newer weights snapshot
// exists for a paid module (default: vct-rl-reranker) at a given
// embedding source (default: qwen3), and if so, issue a short-lived
// signed Storage URL to the .pt file.
//
// Called by the launcher's Stream B Rust poller. The poller already
// knows what weights version it currently has on disk; it POSTs that
// version plus the license key, and this function answers:
//
//   - has_update=false  → no-op, current version is the head.
//   - has_update=true   → here is the latest version + a signed URL.
//
// Wire contract:
//
// Request:
//   POST /functions/v1/rl-latest-version
//   Body: {
//     license_key: string (UUID v4),
//     machine_id_hash: string (sha256 hex, ≥16 chars),
//     current_weights_version: string,      // "" for never-fetched
//     embedding_source?: string,             // "qwen3" | "arctic" | "openai" | …
//                                            // defaults to "qwen3"; data-driven, not enum
//     module_id?: string                     // defaults to "vct-rl-reranker"
//   }
//
// Response 200:
//   {
//     has_update: boolean,
//     latest_version: string,
//     embedding_source: string,              // echoed back
//     download_url: string,                  // signed URL when has_update; "" otherwise
//     download_url_expires_at: string,       // ISO-8601 or ""
//     sha256: string,                        // checksum of the .pt when has_update; "" otherwise
//     released_at: string,                   // ISO-8601
//     notes: string                          // ≤500 chars markdown
//   }
//
// Response 400:
//   { error: "invalid_request_body" | "unsupported_embedding_source", detail?: "..." }
// Response 401:
//   { error: "tier_insufficient" | "license_invalid" | "license_expired", ... }
// Response 500:
//   { error: "service_misconfigured" | "release_lookup_failed" | "signed_url_generation_failed" }
//
// Why we don't trust the launcher's tier cache:
//   Same posture as rl-artifact-url. The launcher caches /validate-tier
//   for 3 days for offline operation of ALREADY-pulled artifacts, but
//   new pulls go through fresh server-side tier re-validation. A user
//   whose subscription lapsed mid-cache shouldn't be able to pull fresh
//   weights through the 3-day grace window — that's the moat.
//
// Why server-side embedding_source discovery:
//   Hard-coding the list of allowed embedding sources in the function
//   means every time we ship a new RL model variant (e.g. arctic →
//   matryoshka), we'd have to redeploy the edge function. Instead the
//   table IS the source of truth: an unrecognized source returns a 400
//   that lists the discovered (module_id, embedding_source) pairs for
//   the requested module, so the client can recover or surface a
//   useful error.
//
// ────────────────────────────────────────────────────────────────────────────

import { createClient, type SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  type OrchestratorTier,
} from "../_shared/variant_map.ts";
import {
  REQUIRED_TIER,
  type RequestBody,
  compareVersions,
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

// Storage bucket name for the paid-module weights. Private bucket;
// rows are accessible only via signed URLs generated server-side.
const WEIGHTS_BUCKET_DEFAULT = "paid-module-weights";

// Signed-URL TTL. 15 minutes mirrors rl-artifact-url's policy floor —
// long enough for a slow connection to fetch a 1-2 GB weights file,
// short enough that a leaked URL is useless within the same coffee
// break.
const SIGNED_URL_TTL_SECONDS = 15 * 60;

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
 * signed URL.
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

interface ReleaseRow {
  version: string;
  storage_path: string;
  sha256: string;
  released_at: string;
  notes: string;
}

/**
 * Look up the latest release row for (module_id, embedding_source).
 *
 * Returns:
 *   - { ok: true, row } on hit (single row from the partial index).
 *   - { ok: false, kind: "unsupported" } if no row exists for that
 *     pair — caller should also fetch the discovered sources list and
 *     respond with 400 unsupported_embedding_source.
 *   - { ok: false, kind: "error", detail } on DB error.
 */
async function lookupLatestRelease(
  supabase: SupabaseClient,
  moduleId: string,
  embeddingSource: string,
): Promise<
  | { ok: true; row: ReleaseRow }
  | { ok: false; kind: "unsupported" }
  | { ok: false; kind: "error"; detail: string }
> {
  const { data, error } = await supabase
    .from("paid_module_releases")
    .select("version, storage_path, sha256, released_at, notes")
    .eq("module_id", moduleId)
    .eq("embedding_source", embeddingSource)
    .eq("is_latest", true)
    .limit(1);

  if (error) {
    return { ok: false, kind: "error", detail: error.message };
  }
  if (!data || data.length === 0) {
    return { ok: false, kind: "unsupported" };
  }
  const row = data[0] as ReleaseRow;
  return { ok: true, row };
}

/**
 * Discover the embedding sources we ship for a given module_id. Used
 * to construct a helpful 400 response when the client asks for an
 * unknown (module_id, embedding_source) pair. Server-side discovery
 * keeps the vocabulary data-driven rather than baking an enum into
 * the function source.
 */
async function discoverSupportedSources(
  supabase: SupabaseClient,
  moduleId: string,
): Promise<string[]> {
  const { data, error } = await supabase
    .from("paid_module_releases")
    .select("embedding_source")
    .eq("module_id", moduleId)
    .eq("is_latest", true);

  if (error || !data) return [];
  // Deduplicate (defensive — the unique index per version already keeps
  // this small, but is_latest could in theory be inconsistent during a
  // mid-write window).
  const seen = new Set<string>();
  for (const r of data as { embedding_source: string }[]) {
    if (typeof r.embedding_source === "string") seen.add(r.embedding_source);
  }
  return Array.from(seen).sort();
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
    return jsonResponse({ error: "invalid_request_body" }, 400);
  }

  const validation = validateRequestBody(rawBody);
  if (!validation.ok) {
    return jsonResponse(
      { error: "invalid_request_body", detail: validation.error },
      400,
    );
  }
  const body = validation.body;

  // ─── Re-validate tier ──────────────────────────────────────────────────
  const tierCheck = await revalidateTierViaSupabase(body);
  if (!tierCheck.valid) {
    console.log(
      `[rl-latest-version] tier check failed: ${tierCheck.reason ?? "unknown"}`,
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

  // ─── Set up Supabase client (service role) ─────────────────────────────
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const bucket = Deno.env.get("WEIGHTS_BUCKET") ?? WEIGHTS_BUCKET_DEFAULT;
  if (!supabaseUrl || !serviceRoleKey) {
    return jsonResponse({ error: "service_misconfigured" }, 500);
  }
  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false },
  });

  // ─── Look up latest release ────────────────────────────────────────────
  const lookup = await lookupLatestRelease(
    supabase,
    body.module_id,
    body.embedding_source,
  );
  if (!lookup.ok && lookup.kind === "error") {
    console.error(
      `[rl-latest-version] release lookup failed: ${lookup.detail}`,
    );
    return jsonResponse(
      { error: "release_lookup_failed", detail: lookup.detail },
      500,
    );
  }
  if (!lookup.ok && lookup.kind === "unsupported") {
    const supported = await discoverSupportedSources(supabase, body.module_id);
    return jsonResponse(
      {
        error: "unsupported_embedding_source",
        detail:
          `No release found for module_id='${body.module_id}', ` +
          `embedding_source='${body.embedding_source}'`,
        module_id: body.module_id,
        supported_embedding_sources: supported,
      },
      400,
    );
  }
  if (!lookup.ok) {
    // Exhaustiveness guard — TypeScript should already prove this
    // unreachable, but keep the explicit fail-safe.
    return jsonResponse({ error: "release_lookup_failed" }, 500);
  }
  const row = lookup.row;

  // ─── Compare versions ──────────────────────────────────────────────────
  const hasUpdate = compareVersions(body.current_weights_version, row.version);

  if (!hasUpdate) {
    console.log(
      `[rl-latest-version] OK tier=${tierCheck.tier} module=${body.module_id} ` +
        `source=${body.embedding_source} version=${row.version} has_update=false`,
    );
    return jsonResponse({
      has_update: false,
      latest_version: row.version,
      embedding_source: body.embedding_source,
      download_url: "",
      download_url_expires_at: "",
      sha256: row.sha256,
      released_at: row.released_at,
      notes: row.notes,
    });
  }

  // ─── Generate signed URL ───────────────────────────────────────────────
  const { data: signed, error: signedErr } = await supabase
    .storage
    .from(bucket)
    .createSignedUrl(row.storage_path, SIGNED_URL_TTL_SECONDS);

  if (signedErr || !signed || !signed.signedUrl) {
    console.error(
      `[rl-latest-version] signed-url generation failed: ` +
        `${signedErr?.message ?? "no url returned"}`,
    );
    return jsonResponse(
      {
        error: "signed_url_generation_failed",
        detail: signedErr?.message ?? "no url returned",
      },
      500,
    );
  }

  const expiresAt = new Date(Date.now() + SIGNED_URL_TTL_SECONDS * 1000)
    .toISOString();

  // Log success WITHOUT the signed URL (it's a bearer credential).
  // The 8-char preview is enough for cross-correlation with the
  // corresponding Storage access audit log without leaking the URL.
  const urlTag = await tokenPreview(signed.signedUrl);
  console.log(
    `[rl-latest-version] OK tier=${tierCheck.tier} module=${body.module_id} ` +
      `source=${body.embedding_source} version=${row.version} has_update=true ` +
      `url=${urlTag}* ttl=${SIGNED_URL_TTL_SECONDS}s expires_at=${expiresAt}`,
  );

  return jsonResponse({
    has_update: true,
    latest_version: row.version,
    embedding_source: body.embedding_source,
    download_url: signed.signedUrl,
    download_url_expires_at: expiresAt,
    sha256: row.sha256,
    released_at: row.released_at,
    notes: row.notes,
  });
});
