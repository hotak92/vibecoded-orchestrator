// Unit tests for webhook replay protection (E-7, v0.2.75).
//
// Run with:
//   deno test --no-check \
//     launcher/supabase/functions/lemon-squeezy-webhook/replay_protection_test.ts
//
// Same harness style as user_lookup_test.ts — no Supabase project, no
// network. The ledger claim/release logic is exercised against an
// in-memory ProcessedWebhookDb fake; the extraction/staleness helpers are
// pure.

import {
  assert,
  assertEquals,
} from "https://deno.land/std@0.224.0/assert/mod.ts";
import {
  claimEvent,
  DEFAULT_MAX_AGE_HOURS,
  extractEventId,
  extractEventTimestamp,
  isStale,
  maxAgeHoursFromEnv,
  type ProcessedWebhookDb,
  releaseClaim,
} from "./replay_protection.ts";

// ─── In-memory ledger fake ──────────────────────────────────────────────────

/** Fake with a Set-backed unique key; `failInserts` simulates a DB
 * outage (non-23505 error). */
function fakeLedger(opts: { failInserts?: boolean } = {}) {
  const rows = new Set<string>();
  const db: ProcessedWebhookDb = {
    from(_table: string) {
      return {
        insert(row: Record<string, unknown>) {
          if (opts.failInserts) {
            return Promise.resolve({
              error: { code: "XX000", message: "synthetic outage" },
            });
          }
          const id = String(row.event_id);
          if (rows.has(id)) {
            return Promise.resolve({
              error: { code: "23505", message: "duplicate key value" },
            });
          }
          rows.add(id);
          return Promise.resolve({ error: null });
        },
        delete() {
          return {
            eq(_column: string, value: string) {
              rows.delete(value);
              return Promise.resolve({ error: null });
            },
          };
        },
      };
    },
  };
  return { db, rows };
}

// ─── extractEventId ─────────────────────────────────────────────────────────

Deno.test("extractEventId: string, number, absent, blank", () => {
  assertEquals(extractEventId({ meta: { event_id: "evt_123" } }), "evt_123");
  assertEquals(extractEventId({ meta: { event_id: 42 } }), "42");
  assertEquals(extractEventId({ meta: {} }), null);
  assertEquals(extractEventId({}), null);
  assertEquals(extractEventId({ meta: { event_id: "   " } }), null);
  assertEquals(extractEventId({ meta: { event_id: { nested: true } } }), null);
});

// ─── extractEventTimestamp ──────────────────────────────────────────────────

Deno.test("extractEventTimestamp: meta.created_at wins, then updated_at, then created_at", () => {
  assertEquals(
    extractEventTimestamp({
      meta: { created_at: "2026-07-08T00:00:00Z" },
      data: { attributes: { updated_at: "x", created_at: "y" } },
    }),
    "2026-07-08T00:00:00Z",
  );
  assertEquals(
    extractEventTimestamp({
      data: {
        attributes: {
          updated_at: "2026-07-08T01:00:00Z",
          created_at: "2020-01-01T00:00:00Z",
        },
      },
    }),
    "2026-07-08T01:00:00Z",
  );
  assertEquals(
    extractEventTimestamp({
      data: { attributes: { created_at: "2026-07-08T02:00:00Z" } },
    }),
    "2026-07-08T02:00:00Z",
  );
  assertEquals(extractEventTimestamp({}), null);
});

// ─── isStale ────────────────────────────────────────────────────────────────

Deno.test("isStale: stale beyond window, fresh inside, no-opinion on absent/garbage", () => {
  const now = Date.parse("2026-07-08T12:00:00Z");
  // 25h old with a 24h window → stale.
  assert(isStale("2026-07-07T11:00:00Z", now, 24));
  // 23h old → fresh.
  assert(!isStale("2026-07-07T13:00:00Z", now, 24));
  // Future timestamps are not stale.
  assert(!isStale("2026-07-09T00:00:00Z", now, 24));
  // Absent / unparseable → no opinion (process).
  assert(!isStale(null, now, 24));
  assert(!isStale("not-a-timestamp", now, 24));
});

Deno.test("maxAgeHoursFromEnv: default, valid override, junk falls back", () => {
  assertEquals(maxAgeHoursFromEnv(undefined), DEFAULT_MAX_AGE_HOURS);
  assertEquals(maxAgeHoursFromEnv("48"), 48);
  assertEquals(maxAgeHoursFromEnv("0"), DEFAULT_MAX_AGE_HOURS);
  assertEquals(maxAgeHoursFromEnv("-5"), DEFAULT_MAX_AGE_HOURS);
  assertEquals(maxAgeHoursFromEnv("banana"), DEFAULT_MAX_AGE_HOURS);
});

// ─── claimEvent / releaseClaim ──────────────────────────────────────────────

Deno.test("claimEvent: first claim wins, duplicate short-circuits — single grant per event_id", async () => {
  const { db } = fakeLedger();
  assertEquals(await claimEvent(db, "evt_1", "order_created"), "claimed");
  // The replayed delivery (same signed body, same event_id) must be
  // recognised as a duplicate → the caller returns 200 WITHOUT a
  // second grant.
  assertEquals(await claimEvent(db, "evt_1", "order_created"), "duplicate");
  // A different event id claims independently.
  assertEquals(await claimEvent(db, "evt_2", "order_created"), "claimed");
});

Deno.test("claimEvent: ledger outage is fail-open ('error', caller processes)", async () => {
  const { db } = fakeLedger({ failInserts: true });
  assertEquals(await claimEvent(db, "evt_1", "order_created"), "error");
});

Deno.test("releaseClaim: a released event can be claimed again (failed processing is retryable)", async () => {
  const { db, rows } = fakeLedger();
  assertEquals(await claimEvent(db, "evt_1", "order_created"), "claimed");
  await releaseClaim(db, "evt_1");
  assertEquals(rows.has("evt_1"), false);
  // The Lemon Squeezy retry after a 4xx/5xx must be able to re-process.
  assertEquals(await claimEvent(db, "evt_1", "order_created"), "claimed");
});
