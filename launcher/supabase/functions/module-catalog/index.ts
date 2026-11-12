// SPDX-License-Identifier: AGPL-3.0-or-later
//
// module-catalog — Public catalog endpoint for paid modules the launcher knows
// about. Anonymous-callable (no per-user identity required); the response
// shape is non-sensitive ("here is what exists, here is how you'd pull it if
// you were authorised"). The actual pull-token + signed-URL flows stay behind
// the existing /rl-artifact-url + /validate-tier surfaces — this function ONLY
// answers "does this module exist; if so, what's its current catalog metadata".
//
// v0.2.33 design rationale: the launcher GUI must NEVER depend on parsing a
// paid module's private manifest to render its own catalog tile. Real-user
// installs don't have the manifest on disk pre-install (the manifest only
// ships inside the private GHCR image). So the pre-install catalog row data
// comes from THIS endpoint; the post-install on-disk manifest at
// `~/.vct/modules/<id>/vct-module.json` becomes the source of truth for
// config-tab / dispatcher / db-migrations only.
//
// See `.claude/context/plans/v0.2.33-architecture-review-2026-05-24.md` §3 for
// the full response contract this implements.
//
// ───────────────────────────────────────────────────────────────────────────
//
// Wire contract:
//
// GET /functions/v1/module-catalog
//   → 200 { schema_version: 1, fetched_at: ISO-8601, modules: [...] }
//
// GET /functions/v1/module-catalog?id=<module_id>
//   → 200 { schema_version: 1, fetched_at: ISO-8601, modules: [<one>] }
//   → 404 { error: "module_not_found", id: "<requested-id>" }
//
// On bucket misconfigured / read error:
//   → 500 { error: "service_misconfigured" }
//
// ───────────────────────────────────────────────────────────────────────────

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { buildCorsHeaders, makeJsonResponse } from "../_shared/http.ts";
import { isValidModuleId } from "./validation.ts";

const SCHEMA_VERSION = 1;
const BUCKET = "paid-module-catalog";

const CORS_HEADERS = buildCorsHeaders({
  methods: "GET, OPTIONS",
  extra: {
    // Server-side cache hint: 60s. Supabase Edge Functions are stateless
    // across invocations so this lives only in client / CDN caches; the
    // launcher's 15min app_state TTL is the durable cache.
    "Cache-Control": "public, max-age=60",
  },
});
const jsonResponse = makeJsonResponse(CORS_HEADERS);

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }
  if (req.method !== "GET") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    console.error("[module-catalog] missing SUPABASE_URL / SERVICE_ROLE_KEY");
    return jsonResponse({ error: "service_misconfigured" }, 500);
  }
  const supabase = createClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false },
  });

  const url = new URL(req.url);
  const moduleId = url.searchParams.get("id");
  const fetchedAt = new Date().toISOString();

  try {
    if (moduleId) {
      // ─── Single-module fetch ──────────────────────────────────────────
      // Validate the id BEFORE it touches the storage object key (audit
      // RLS-2). Reject anything that isn't a clean lowercase-kebab slug.
      if (!isValidModuleId(moduleId)) {
        return jsonResponse({ error: "invalid_module_id", id: moduleId }, 400);
      }
      const objectPath = `${moduleId}.json`;
      const { data, error } = await supabase
        .storage
        .from(BUCKET)
        .download(objectPath);
      if (error || !data) {
        // Distinguish "object missing" (404) from "bucket / RLS broken" (500).
        // supabase-js's storage client returns `error.message === "Object not
        // found"` for the 404 case across the StorageError types we've seen.
        const msg = error?.message ?? "no data";
        if (msg.toLowerCase().includes("not found")) {
          return jsonResponse({ error: "module_not_found", id: moduleId }, 404);
        }
        console.error(
          `[module-catalog] download(${objectPath}) failed: ${msg}`,
        );
        return jsonResponse({ error: "service_misconfigured" }, 500);
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(await data.text());
      } catch (e) {
        console.error(
          `[module-catalog] JSON parse for ${objectPath}: ${String(e)}`,
        );
        return jsonResponse({ error: "service_misconfigured" }, 500);
      }
      return jsonResponse({
        schema_version: SCHEMA_VERSION,
        fetched_at: fetchedAt,
        modules: [parsed],
      });
    }

    // ─── List all modules ────────────────────────────────────────────────
    const { data: list, error: listErr } = await supabase
      .storage
      .from(BUCKET)
      .list();
    if (listErr) {
      console.error(`[module-catalog] list() failed: ${listErr.message}`);
      return jsonResponse({ error: "service_misconfigured" }, 500);
    }
    const jsonObjects = (list ?? []).filter((f) =>
      typeof f.name === "string" && f.name.endsWith(".json")
    );

    // Download each in parallel. Skip any that fail to download or parse —
    // a single malformed entry must NOT poison the whole catalog response.
    const downloads = await Promise.all(
      jsonObjects.map(async (f) => {
        try {
          const { data, error } = await supabase
            .storage
            .from(BUCKET)
            .download(f.name);
          if (error || !data) {
            console.warn(
              `[module-catalog] skipped ${f.name}: ${error?.message ?? "no data"}`,
            );
            return null;
          }
          return JSON.parse(await data.text());
        } catch (e) {
          console.warn(
            `[module-catalog] skipped ${f.name}: parse error ${String(e)}`,
          );
          return null;
        }
      }),
    );

    const modules = downloads.filter((m): m is unknown => m !== null);
    console.log(
      `[module-catalog] OK ${modules.length}/${jsonObjects.length} modules served`,
    );
    return jsonResponse({
      schema_version: SCHEMA_VERSION,
      fetched_at: fetchedAt,
      modules,
    });
  } catch (e) {
    console.error(`[module-catalog] unexpected error: ${String(e)}`);
    return jsonResponse({ error: "service_misconfigured" }, 500);
  }
});
