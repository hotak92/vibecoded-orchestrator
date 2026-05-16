// telemetry — receive batched telemetry events from the launcher /
// orchestrator clients and persist them to the `telemetry_events` table.
//
// Called by VCThelpers/telemetry/uploader.py::upload_pending() after each
// Claude turn (via .claude/hooks/session-stop-telemetry-upload.sh).
//
// Phase 2B (2026-05-16): launcher already POSTs here; until this function
// is deployed the uploader falls through to ~/.vibecoded/telemetry_pending.jsonl
// (the local fallback). Once this function is live the uploader switches
// to direct POST automatically — same DEFAULT_URL on the client side.
//
// Wire contract (matches VCThelpers/telemetry/uploader.py:211-220):
//
//   POST /functions/v1/telemetry
//   Content-Type: application/json
//   User-Agent: vibecoded-telemetry/1.0
//   { "events": [
//       {
//         "event_type": "rl_retrieval" | "session_start" | ...,
//         "created_at": "<iso-8601>",
//         // ↓ spread of TelemetryEvent envelope (collector.py:52-72)
//         "timestamp": "<iso-8601>",
//         "machine_hash": "<sha256-hex>",
//         "orchestrator_version": "...",
//         "os_name": "Linux" | "Darwin" | "Windows",
//         "os_version": "...",
//         "python_version": "...",
//         "user_id_sha256": "<sha256-hex or empty>",  // Phase 2C
//         "payload": { ... event-specific }
//       }, ...
//     ] }
//
// Response (200):
//   { "ok": true, "accepted": N }
//
// Response (400):
//   { "ok": false, "error": "invalid_json_body" | "events_required" | ... }
//
// Response (500):
//   { "ok": false, "error": "service_misconfigured" | "db_insert_failed" }
//
// Anti-abuse posture:
//   - verify_jwt = false (clients have no JWT; auth is opaque-key + machine binding)
//   - Body size cap: 5MB (reasonable for batches up to BATCH_SIZE=100 events)
//   - No PII in payloads (collector enforces — query text, paths, tokens all
//     scrubbed before enqueue)
//   - user_id_sha256 is opaque (cannot reverse without license-key DB)
//   - Rate limiting deferred to Supabase platform defaults for now;
//     production should add Hub-token rate-limiting per machine_hash.

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

const MAX_BODY_BYTES = 5 * 1024 * 1024; // 5MB
const MAX_EVENTS_PER_BATCH = 200; // collector caps at 100; defense in depth

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

interface IncomingEvent {
  event_type?: string;
  created_at?: string;
  timestamp?: string;
  machine_hash?: string;
  orchestrator_version?: string;
  os_name?: string;
  os_version?: string;
  python_version?: string;
  user_id_sha256?: string;
  payload?: Record<string, unknown>;
  // The uploader spreads payload contents at the top level too, so we
  // accept extra keys via index signature.
  [k: string]: unknown;
}

interface IncomingBatch {
  events?: IncomingEvent[];
}

function isValidEvent(e: IncomingEvent): boolean {
  if (typeof e !== "object" || e === null) return false;
  if (typeof e.event_type !== "string" || e.event_type.length === 0) return false;
  // event_type allowlist — paranoia. Add new types here as collectors land.
  const KNOWN_EVENT_TYPES = new Set([
    "session_start",
    "rl_retrieval",
    "qlearning_routing",
    "instinct_event",
    "hardware",
  ]);
  if (!KNOWN_EVENT_TYPES.has(e.event_type)) return false;
  // Light shape checks — server-side schema enforcement happens at the
  // DB layer (column types). We just want to reject the obvious garbage
  // before paying the round-trip to PG.
  if (typeof e.machine_hash !== "string") return false;
  return true;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return jsonResponse({ ok: false, error: "method_not_allowed" }, 405);
  }

  // Body-size guard before parsing JSON (avoids OOM-via-huge-body attacks).
  const contentLength = parseInt(req.headers.get("content-length") ?? "0", 10);
  if (contentLength > MAX_BODY_BYTES) {
    return jsonResponse(
      { ok: false, error: "body_too_large", limit_bytes: MAX_BODY_BYTES },
      413,
    );
  }

  let batch: IncomingBatch;
  try {
    batch = await req.json();
  } catch (_) {
    return jsonResponse({ ok: false, error: "invalid_json_body" }, 400);
  }

  if (!Array.isArray(batch.events)) {
    return jsonResponse({ ok: false, error: "events_required" }, 400);
  }
  if (batch.events.length === 0) {
    return jsonResponse({ ok: true, accepted: 0 });
  }
  if (batch.events.length > MAX_EVENTS_PER_BATCH) {
    return jsonResponse(
      {
        ok: false,
        error: "batch_too_large",
        limit: MAX_EVENTS_PER_BATCH,
        got: batch.events.length,
      },
      400,
    );
  }

  // Drop invalid events silently — better than 400-ing the whole batch
  // because clients can't easily retry individual events. The uploader's
  // batch is opaque to it after enqueue; if we 400, all 100 events
  // would be marked failed and retried, including the valid ones.
  const validEvents = batch.events.filter(isValidEvent);

  if (validEvents.length === 0) {
    return jsonResponse({ ok: true, accepted: 0, rejected: batch.events.length });
  }

  // ─── Persist to PG ─────────────────────────────────────────────────────
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (!supabaseUrl || !serviceRoleKey) {
    return jsonResponse({ ok: false, error: "service_misconfigured" }, 500);
  }

  // Bulk-insert via PostgREST. The `telemetry_events` table is created
  // by the migration at supabase/migrations/20260516_telemetry_events.sql.
  // Each row: event_type, created_at (client TS), timestamp (client TS),
  // machine_hash, os_*, python_version, user_id_sha256, payload (jsonb).
  const rows = validEvents.map((e) => ({
    event_type: e.event_type,
    client_created_at: e.created_at ?? e.timestamp ?? new Date().toISOString(),
    client_timestamp: e.timestamp ?? null,
    machine_hash: e.machine_hash,
    orchestrator_version: e.orchestrator_version ?? "",
    os_name: e.os_name ?? "",
    os_version: e.os_version ?? "",
    python_version: e.python_version ?? "",
    user_id_sha256: e.user_id_sha256 ?? "", // Phase 2C
    payload: e.payload ?? {},
  }));

  let pgResp: Response;
  try {
    pgResp = await fetch(`${supabaseUrl}/rest/v1/telemetry_events`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        apikey: serviceRoleKey,
        Authorization: `Bearer ${serviceRoleKey}`,
        // Don't return the inserted rows — we just need the count.
        Prefer: "return=minimal",
      },
      body: JSON.stringify(rows),
      signal: AbortSignal.timeout(10_000),
    });
  } catch (e) {
    console.error(
      `[telemetry] PG insert fetch failed: ${String(e).slice(0, 300)}`,
    );
    return jsonResponse(
      { ok: false, error: "db_insert_failed", detail: "upstream_unreachable" },
      500,
    );
  }

  if (!pgResp.ok) {
    const errBody = await pgResp.text().catch(() => "");
    console.error(
      `[telemetry] PG insert returned ${pgResp.status}: ${errBody.slice(0, 300)}`,
    );
    return jsonResponse(
      {
        ok: false,
        error: "db_insert_failed",
        detail: `pg_${pgResp.status}`,
      },
      500,
    );
  }

  // Log success without the user_id_sha256 (still opaque but treated
  // as a sensitive field to avoid log-aggregator analytics on user
  // behavior). Just count + event-type breakdown.
  const typeBreakdown: Record<string, number> = {};
  for (const ev of validEvents) {
    typeBreakdown[ev.event_type!] = (typeBreakdown[ev.event_type!] ?? 0) + 1;
  }
  console.log(
    `[telemetry] OK accepted=${validEvents.length} ` +
      `rejected=${batch.events.length - validEvents.length} ` +
      `breakdown=${JSON.stringify(typeBreakdown)}`,
  );

  return jsonResponse({
    ok: true,
    accepted: validEvents.length,
    rejected: batch.events.length - validEvents.length,
  });
});
