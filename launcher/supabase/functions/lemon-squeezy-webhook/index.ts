import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import {
  assertNoPlaceholderKeysInProduction,
  lookupVariant,
} from "../_shared/variant_map.ts";
import {
  HANDLED_EVENTS,
  activateAppForUser,
  dispatchLifecycleEvent,
} from "./orchestrator_additions.ts";
import { findUserIdByEmail } from "./user_lookup.ts";
import {
  claimEvent,
  extractEventId,
  extractEventTimestamp,
  isStale,
  maxAgeHoursFromEnv,
  releaseClaim,
} from "./replay_protection.ts";

// Pre-flight: hard-fail at module init if VARIANT_MAP still ships
// placeholder keys in a production deployment. Prevents the silent
// failure where every Pro purchase falls through to "unknown variant"
// and never sets the customer's tier. See variant_map.ts JSDoc for the
// full failure-mode rationale (audit blocker #3, 2026-05-07).
assertNoPlaceholderKeysInProduction();

// Constant-time string comparison. Avoids leaking signature bytes via
// short-circuit timing of `===`. Both inputs are hex strings of equal length
// when valid; mismatched lengths fail fast (length isn't secret).
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

// HMAC-SHA256 using Web Crypto API (no external deps)
async function verifySignature(secret: string, body: string, signature: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  const hex = Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return timingSafeEqual(hex, signature);
}

Deno.serve(async (req: Request) => {
  // Only accept POST
  if (req.method !== "POST") {
    return new Response("Method not allowed", { status: 405 });
  }

  const body = await req.text();

  // Signature verification is MANDATORY. If the secret is not configured we
  // refuse the request — accepting unsigned events would let anyone POST a
  // forged `order_created` and grant any app to any email.
  const secret = Deno.env.get("LEMON_SQUEEZY_WEBHOOK_SECRET");
  if (!secret) {
    console.error("FATAL: LEMON_SQUEEZY_WEBHOOK_SECRET not configured");
    return new Response("Webhook misconfigured", { status: 500 });
  }

  const signature = req.headers.get("x-signature");
  if (!signature) {
    return new Response("Missing signature", { status: 401 });
  }

  const valid = await verifySignature(secret, body, signature);
  if (!valid) {
    return new Response("Invalid signature", { status: 401 });
  }

  const payload = JSON.parse(body);
  const eventName = payload.meta?.event_name;

  // Gate unhandled event types early.
  if (!HANDLED_EVENTS.has(eventName)) {
    return new Response(
      JSON.stringify({ message: `Ignored event: ${eventName}` }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }

  // Init Supabase with service role key (bypasses RLS)
  const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
  const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
  const supabase = createClient(supabaseUrl, supabaseServiceKey);

  // ── E-7 (v0.2.75): replay protection ─────────────────────────────────
  // The HMAC above proves origin, not one-time-ness: a captured signed
  // body replays verbatim. Freshness first (stale replays are refused
  // even if the ledger row was pruned), then an insert-or-conflict claim
  // on meta.event_id (a replay necessarily carries the same event_id —
  // changing it would break the HMAC). See replay_protection.ts for the
  // fail-open rationale on absent ids/timestamps and ledger errors.
  const eventTimestamp = extractEventTimestamp(payload);
  const maxAgeHours = maxAgeHoursFromEnv(Deno.env.get("WEBHOOK_MAX_AGE_HOURS"));
  if (isStale(eventTimestamp, Date.now(), maxAgeHours)) {
    return new Response(
      JSON.stringify({
        error: `Stale webhook: event timestamp ${eventTimestamp} is older than ${maxAgeHours}h`,
      }),
      { status: 400, headers: { "Content-Type": "application/json" } },
    );
  }

  const eventId = extractEventId(payload);
  let claimed = false;
  if (eventId) {
    const claim = await claimEvent(supabase, eventId, eventName);
    if (claim === "duplicate") {
      // Already processed successfully — acknowledge without re-granting.
      console.log(`[replay] duplicate event_id=${eventId} (${eventName}); skipping`);
      return new Response(
        JSON.stringify({ message: `Duplicate event ${eventId}: already processed`, deduplicated: true }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    claimed = claim === "claimed";
  } else {
    console.warn(
      `[replay] payload for ${eventName} carries no meta.event_id — processing without dedup`,
    );
  }

  // Dedup guards only SUCCESSFUL processing: any non-2xx response below
  // releases the claim so a Lemon Squeezy retry (or the user retrying
  // after registering) can re-process instead of being swallowed as a
  // "duplicate" of a failure.
  const respond = async (resp: Response): Promise<Response> => {
    if (claimed && eventId && resp.status >= 400) {
      await releaseClaim(supabase, eventId);
    }
    return resp;
  };

  // Lifecycle events (cancel / expire / refund / payment_failed) are handled
  // in the orchestrator_additions module. Only order_created flows through
  // the activation block below.
  if (eventName !== "order_created") {
    const result = await dispatchLifecycleEvent(supabase, eventName, payload);
    if (result) {
      return respond(
        new Response(JSON.stringify(result.body), {
          status: result.status,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }
    // Fall-through shouldn't happen given HANDLED_EVENTS gate above.
    return new Response(
      JSON.stringify({ message: `Unhandled: ${eventName}` }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  }

  // ── order_created ────────────────────────────────────────────────────────
  const email = payload.data?.attributes?.user_email;
  const firstItem = payload.data?.attributes?.first_order_item;
  const variantId = String(firstItem?.variant_id ?? "");

  if (!email || !variantId) {
    return respond(
      new Response(
        JSON.stringify({ error: "Missing email or variant_id" }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      ),
    );
  }

  const mapping = lookupVariant(variantId);
  if (!mapping) {
    console.log(`Unknown variant_id: ${variantId}`);
    return respond(
      new Response(
        JSON.stringify({ error: `Unknown variant_id: ${variantId}` }),
        { status: 400, headers: { "Content-Type": "application/json" } },
      ),
    );
  }

  // Resolve email → user id via bounded pagination (audit N1-1). The prior
  // single-page listUsers().find() silently failed for any paying customer
  // beyond the first 50 registered users. A thrown error here is an admin-API
  // failure (→ 500), distinct from a genuinely-absent user (null → 404).
  let userId: string | null;
  try {
    userId = await findUserIdByEmail(supabase, email);
  } catch (e) {
    console.error("Error listing users:", e);
    return respond(
      new Response(
        JSON.stringify({ error: "Failed to look up user" }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      ),
    );
  }

  if (!userId) {
    console.log(`No user found with email: ${email}`);
    // respond() releases the dedup claim: when the user registers and
    // Lemon Squeezy retries (or support re-sends the event), the retry
    // must process, not short-circuit as a duplicate of this failure.
    return respond(
      new Response(
        JSON.stringify({ error: "User not found. They must register first." }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );
  }

  try {
    const { activated, tierChanged } = await activateAppForUser(supabase, userId, mapping);
    console.log(
      `[order_created] ${email} appId=${mapping.appId}` +
        (mapping.tier ? ` tier=${mapping.tier}` : "") +
        ` activated=${activated} tierChanged=${tierChanged}`,
    );
  } catch (e) {
    console.error("Error activating app:", e);
    return respond(
      new Response(
        JSON.stringify({ error: "Failed to activate app" }),
        { status: 500, headers: { "Content-Type": "application/json" } },
      ),
    );
  }

  return new Response(
    JSON.stringify({
      success: true,
      appId: mapping.appId,
      tier: mapping.tier ?? null,
      email,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
});
