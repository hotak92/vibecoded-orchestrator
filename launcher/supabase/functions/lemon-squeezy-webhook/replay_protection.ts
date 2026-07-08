// Replay / idempotency protection for lemon-squeezy-webhook (E-7, v0.2.75).
//
// HMAC signature verification (index.ts) proves a payload came from Lemon
// Squeezy — but a captured SIGNED body can be re-POSTed verbatim, and the
// pre-fix handler re-ran the grant path on every delivery. Two layers close
// that:
//
//   1. Idempotency ledger — `processed_webhooks` (migration
//      20260708000000) keyed on `meta.event_id`. A replayed body carries
//      the same event_id (changing it would invalidate the HMAC), so an
//      insert-or-conflict claim short-circuits duplicates with 200 and no
//      re-grant. Claims are RELEASED on failed processing so Lemon
//      Squeezy's retries can re-process (dedup guards only SUCCESSFUL
//      handling).
//   2. Freshness window — events whose payload timestamp is older than
//      `WEBHOOK_MAX_AGE_HOURS` (default 24) are rejected 4xx: even if the
//      ledger row was pruned, a long-delayed replay of an old capture is
//      refused.
//
// Fail-open notes (deliberate, payments-first):
//   * A payload WITHOUT meta.event_id is processed (with a console.warn) —
//     an attacker cannot strip the field (HMAC covers the body), so
//     absence means Lemon Squeezy genuinely didn't send one.
//   * A ledger INSERT error other than a duplicate (DB hiccup) processes
//     anyway with a loud log: a broken dedup table must not stall
//     customers' purchases; the grant path itself is tier-idempotent.
//   * An absent/unparseable timestamp is "no opinion" — processed.

/** Minimal structural slice of the supabase-js client this module needs —
 * lets tests inject an in-memory fake (same pattern as user_lookup.ts's
 * FetchPage injection). */
export interface ProcessedWebhookDb {
  from(table: string): {
    insert(row: Record<string, unknown>): PromiseLike<{ error: { code?: string; message?: string } | null }>;
    delete(): {
      eq(column: string, value: string): PromiseLike<{ error: { message?: string } | null }>;
    };
  };
}

export const PROCESSED_WEBHOOKS_TABLE = "processed_webhooks";
export const DEFAULT_MAX_AGE_HOURS = 24;

/** Postgres unique-violation SQLSTATE — the "already processed" signal. */
const UNIQUE_VIOLATION = "23505";

/** Extract `meta.event_id` as a string. Numbers are stringified; other
 * shapes (absent, null, objects) → null. */
// deno-lint-ignore no-explicit-any
export function extractEventId(payload: any): string | null {
  const raw = payload?.meta?.event_id;
  if (typeof raw === "string" && raw.trim() !== "") return raw.trim();
  if (typeof raw === "number" && Number.isFinite(raw)) return String(raw);
  return null;
}

/** Best event-time proxy in a Lemon Squeezy payload:
 * `meta.created_at` (if LS ever sends it) → `data.attributes.updated_at`
 * (bumped on each lifecycle transition — the event time for
 * subscription_* events, whose `created_at` is the SUBSCRIPTION's
 * creation and may be years old) → `data.attributes.created_at`
 * (order_created, where created≈event time). Absent → null. */
// deno-lint-ignore no-explicit-any
export function extractEventTimestamp(payload: any): string | null {
  for (const candidate of [
    payload?.meta?.created_at,
    payload?.data?.attributes?.updated_at,
    payload?.data?.attributes?.created_at,
  ]) {
    if (typeof candidate === "string" && candidate.trim() !== "") {
      return candidate.trim();
    }
  }
  return null;
}

/** True ONLY when `timestamp` parses to a valid instant more than
 * `maxAgeHours` before `nowMs`. Absent / unparseable / future → false
 * (no opinion — never reject a purchase on a missing field). */
export function isStale(
  timestamp: string | null,
  nowMs: number,
  maxAgeHours: number,
): boolean {
  if (!timestamp) return false;
  const parsed = Date.parse(timestamp);
  if (Number.isNaN(parsed)) return false;
  return nowMs - parsed > maxAgeHours * 3600 * 1000;
}

/** Resolve the freshness window: `WEBHOOK_MAX_AGE_HOURS` env (positive
 * finite number) → default 24. */
export function maxAgeHoursFromEnv(raw: string | undefined): number {
  if (!raw) return DEFAULT_MAX_AGE_HOURS;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return DEFAULT_MAX_AGE_HOURS;
  return parsed;
}

export type ClaimResult = "claimed" | "duplicate" | "error";

/** Claim `eventId` in the ledger BEFORE processing. `duplicate` means a
 * prior delivery already processed this event successfully — the caller
 * must short-circuit 200 without re-granting. `error` = ledger
 * unavailable — the caller processes anyway (fail-open, logged). */
export async function claimEvent(
  db: ProcessedWebhookDb,
  eventId: string,
  eventName: string,
): Promise<ClaimResult> {
  const { error } = await db.from(PROCESSED_WEBHOOKS_TABLE).insert({
    event_id: eventId,
    event_name: eventName,
  });
  if (!error) return "claimed";
  if (error.code === UNIQUE_VIOLATION) return "duplicate";
  console.error(
    `[replay] ledger claim failed for event_id=${eventId}: ${error.message ?? error.code} — processing WITHOUT dedup`,
  );
  return "error";
}

/** Release a claim after FAILED processing so Lemon Squeezy's retry can
 * re-process. Best-effort: a failed release only risks a stuck claim,
 * which support can clear; it never breaks the response. */
export async function releaseClaim(
  db: ProcessedWebhookDb,
  eventId: string,
): Promise<void> {
  const { error } = await db
    .from(PROCESSED_WEBHOOKS_TABLE)
    .delete()
    .eq("event_id", eventId);
  if (error) {
    console.error(
      `[replay] ledger release failed for event_id=${eventId}: ${error.message} — a retry of this event will be deduplicated as if it had succeeded; clear the row manually if the grant is missing`,
    );
  }
}
