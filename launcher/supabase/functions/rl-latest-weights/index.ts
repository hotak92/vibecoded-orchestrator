// rl-latest-weights — Issue a signed download URL for the latest "default
// weights" bundle for a given (module_id, embedding_source) pair.
//
// Differs from rl-latest-version (which returns version metadata + a
// signed URL when a NEWER snapshot exists than the client's current
// version) by ALWAYS returning the latest version + a signed URL — the
// fast path for the launcher's "Download default weights" manifest
// button on first install, when the user has never had a .pt before
// and there is no `current_weights_version` to compare against.
//
// Both endpoints query the SAME `paid_module_releases` table and reuse
// the SAME private storage bucket. The split exists because the two
// callers have different intents and contracts:
//
//   rl-latest-version:  POST {license_key, machine_id_hash,
//                            current_weights_version, embedding_source?,
//                            module_id?}
//                       → {has_update, latest_version, download_url, ...}
//                       Driven by the launcher's daily Stream B poller.
//
//   rl-latest-weights:  POST {license_key, machine_id_hash,
//                            embedding_source?, module_id?}
//                       → {download_url, version, sha256, expires_at}
//                       Driven by the v0.2.32 paid-module manifest
//                       button (`commands::module_default_weights::
//                       module_download_default_weights`).
//
// Anti-piracy posture (same as rl-latest-version):
//   - Re-validates license tier via /validate-tier server-to-server
//     (refuses to trust the launcher's 3-day tier cache for new pulls)
//   - Signed URLs are short-lived (15 min) and scoped to the specific
//     .pt object — a leaked URL reveals exactly ONE version, not the
//     whole private bucket
//   - Refuses free-tier users (returns 401 tier_insufficient)
//
// Contract (caller is launcher/src-tauri/src/commands/module_default_weights.rs):
//
// Request:
//   POST /functions/v1/rl-latest-weights
//   Headers: Authorization: Bearer <license_key>  (redundant w/ body;
//                                                  the caller sets both
//                                                  for compatibility
//                                                  with both edge
//                                                  variants)
//   Body: {
//     license_key: string (UUID v4),
//     machine_id_hash: string (sha256 hex, ≥16 chars),
//     embedding_source?: string,             // "qwen3" | "arctic" | …
//                                            // defaults to "qwen3";
//                                            // data-driven, not enum
//     module_id?: string                     // defaults to "vct-rl-reranker"
//   }
//
// Response 200:
//   {
//     download_url: string,                  // signed URL to the .pt
//     version: string,                       // e.g. "arctic-2026-05-19"
//     sha256: string,                        // hex64 — empty allowed if
//                                            // not recorded on the row
//     expires_at: string                     // ISO-8601 download_url
//                                            // expiry (15 min from now)
//   }
//
// Response 400:
//   { error: "invalid_request_body", detail: "<validation code>" }
//   { error: "unsupported_embedding_source", detail: "…",
//     module_id, supported_embedding_sources: string[] }
// Response 401:
//   { error: "tier_insufficient", required_tier, got }
//   { error: "license_invalid", detail }
// Response 404:
//   (subsumed by 400 unsupported_embedding_source — see "Why 400 not
//   404" below)
// Response 405:
//   { error: "method_not_allowed" }
// Response 500:
//   { error: "service_misconfigured" }
//   { error: "release_lookup_failed", detail }
//   { error: "signed_url_generation_failed", detail }
//
// Why 400 (not 404) for missing-embedding-source:
//   The caller asked for a specific (module_id, embedding_source) pair.
//   When no row matches, the failure is NOT "the resource doesn't exist
//   at this URL" (404) — it's "the request named a vocabulary item we
//   don't ship yet" (a client-side issue). Returning 400 with a
//   discovery list (`supported_embedding_sources`) lets the launcher
//   recover by re-prompting the user or auto-picking the closest match.
//   This mirrors rl-latest-version's posture exactly; the caller's 404
//   handling path documented in module_default_weights.rs is reached
//   via the 400 with a clear "no default weights for embedding_source
//   X" detail string.
//
// ────────────────────────────────────────────────────────────────────────────

import { createClient, type SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";
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

// Storage bucket name for the paid-module weights. Same private bucket
// rl-latest-version uses — sharing the bucket means a new release
// inserted by the operator runbook is immediately available via both
// endpoints. Override via `WEIGHTS_BUCKET` env (only useful when
// running against a staging bucket).
const WEIGHTS_BUCKET_DEFAULT = "paid-module-weights";

// Signed-URL TTL. 15 minutes mirrors rl-latest-version + rl-artifact-url's
// policy floor — long enough for a slow connection to fetch a 1-2 GB
// weights file, short enough that a leaked URL is useless within the
// same coffee break.
const SIGNED_URL_TTL_SECONDS = 15 * 60;

const jsonResponse = makeJsonResponse(CORS_HEADERS);

// Tier re-validation: shared `revalidateTierViaSupabase` from
// `_shared/tier_revalidation.ts` (H-7 extraction). The pre-extraction
// per-copy comment claimed Supabase functions couldn't share code
// beyond `_shared/` — which is precisely where this now lives; each
// function remains independently deployable because the deploy bundler
// vendors `_shared/` imports. We MUST re-validate server-side before
// issuing a signed URL: the launcher could pass a stale/forged cache
// (the Authorization header could even be forged by a malicious
// launcher).

interface ReleaseRow {
  version: string;
  storage_path: string;
  sha256: string;
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
    .select("version, storage_path, sha256")
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
 * the function source — a new embedding source becomes live the
 * moment a row is inserted; no function redeploy required.
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
      `[rl-latest-weights] tier check failed: ${tierCheck.reason ?? "unknown"}`,
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
      `[rl-latest-weights] release lookup failed: ${lookup.detail}`,
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
          `No default weights bundle for module_id='${body.module_id}', ` +
          `embedding_source='${body.embedding_source}' yet — ` +
          `${
            supported.length === 0
              ? "no embedding sources are shipped for this module yet."
              : `available sources: ${supported.join(", ")}.`
          }`,
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

  // ─── Generate signed URL ───────────────────────────────────────────────
  // Unlike rl-latest-version we ALWAYS generate the signed URL — this
  // endpoint has no "no-update needed" branch. The client asked for
  // the head; we give them the head.
  const { data: signed, error: signedErr } = await supabase
    .storage
    .from(bucket)
    .createSignedUrl(row.storage_path, SIGNED_URL_TTL_SECONDS);

  if (signedErr || !signed || !signed.signedUrl) {
    console.error(
      `[rl-latest-weights] signed-url generation failed: ` +
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
    `[rl-latest-weights] OK tier=${tierCheck.tier} module=${body.module_id} ` +
      `source=${body.embedding_source} version=${row.version} ` +
      `url=${urlTag}* ttl=${SIGNED_URL_TTL_SECONDS}s expires_at=${expiresAt}`,
  );

  return jsonResponse({
    download_url: signed.signedUrl,
    version: row.version,
    sha256: row.sha256,
    expires_at: expiresAt,
  });
});
