// Orchestrator tier additions for lemon-squeezy-webhook
//
// This file is a STAGING module. Its contents must be merged into
// `index.ts` after the parallel security PR (signature verification +
// webhook secret hard-fail) lands. See MERGE_INSTRUCTIONS.md.
//
// Why a separate file: a parallel agent is editing index.ts at the same
// time. Keeping the orchestrator additions isolated avoids merge
// conflicts on the signature/secret block.

import type { SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2";
import { lookupVariant, VARIANT_MAP, type VariantMapping } from "../_shared/variant_map.ts";
import { findUserIdByEmail } from "./user_lookup.ts";

export { lookupVariant, VARIANT_MAP };
export type { VariantMapping };

// ────────────────────────────────────────────────────────────────────────────
// Lifecycle event handling
// ────────────────────────────────────────────────────────────────────────────

/**
 * Lemon Squeezy webhook event names we care about.
 * Anything else is ignored (returns 200 with `Ignored event` message).
 */
export const HANDLED_EVENTS = new Set<string>([
  "order_created",
  "subscription_cancelled",
  "subscription_expired",
  "subscription_payment_failed",
  "order_refunded",
]);

// findUserIdByEmail is defined in ./user_lookup.ts (bounded pagination over
// auth.admin.listUsers — audit N1-1). Re-exported here so existing importers
// of this module keep working unchanged.
export { findUserIdByEmail };

/**
 * Add `appId` to profiles.apps (idempotent) and, if mapping has a tier and
 * targets "orchestrator", set profiles.orchestrator_tier.
 *
 * Used by order_created handler. Service role bypasses RLS.
 */
export async function activateAppForUser(
  supabase: SupabaseClient,
  userId: string,
  mapping: VariantMapping,
): Promise<{ activated: boolean; tierChanged: boolean }> {
  const { data: profile } = await supabase
    .from("profiles")
    .select("apps, orchestrator_tier")
    .eq("id", userId)
    .single();

  const currentApps: string[] = profile?.apps ?? [];
  const currentTier: string = profile?.orchestrator_tier ?? "free";

  const wantTier = mapping.tier && mapping.appId === "orchestrator";
  const apps = currentApps.includes(mapping.appId)
    ? currentApps
    : [...currentApps, mapping.appId];

  const update: Record<string, unknown> = { apps };
  if (wantTier) {
    update.orchestrator_tier = mapping.tier;
    // Clear any scheduled downgrade — fresh purchase resets the lifecycle.
    update.orchestrator_tier_ends_at = null;
  }

  const { error } = await supabase
    .from("profiles")
    .update(update)
    .eq("id", userId);

  if (error) throw error;

  return {
    activated: !currentApps.includes(mapping.appId),
    tierChanged: wantTier ? currentTier !== mapping.tier : false,
  };
}

/**
 * subscription_cancelled — user cancelled the subscription, but tier remains
 * active until the period end. We record `orchestrator_tier_ends_at` and log;
 * downgrade itself happens on subscription_expired.
 */
export async function handleSubscriptionCancelled(
  supabase: SupabaseClient,
  payload: any,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const attrs = payload.data?.attributes ?? {};
  const email: string | undefined = attrs.user_email;
  const endsAt: string | undefined = attrs.ends_at; // ISO 8601

  if (!email) {
    return { status: 400, body: { error: "Missing user_email" } };
  }

  const userId = await findUserIdByEmail(supabase, email);
  if (!userId) {
    console.log(`[cancelled] No user for ${email} — ignoring`);
    return { status: 200, body: { message: "User not found, ignored." } };
  }

  await supabase
    .from("profiles")
    .update({ orchestrator_tier_ends_at: endsAt ?? null })
    .eq("id", userId);

  console.log(`[cancelled] ${email} → tier remains active until ${endsAt ?? "unknown"}`);
  return { status: 200, body: { success: true, ends_at: endsAt } };
}

/**
 * subscription_expired — period ended without renewal. Downgrade to free.
 */
export async function handleSubscriptionExpired(
  supabase: SupabaseClient,
  payload: any,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const email: string | undefined = payload.data?.attributes?.user_email;
  if (!email) return { status: 400, body: { error: "Missing user_email" } };

  const userId = await findUserIdByEmail(supabase, email);
  if (!userId) {
    console.log(`[expired] No user for ${email} — ignoring`);
    return { status: 200, body: { message: "User not found, ignored." } };
  }

  const { error } = await supabase
    .from("profiles")
    .update({
      orchestrator_tier: "free",
      orchestrator_tier_ends_at: null,
    })
    .eq("id", userId);

  if (error) {
    console.error("[expired] Failed to downgrade:", error);
    return { status: 500, body: { error: "Failed to downgrade tier" } };
  }

  console.log(`[expired] Downgraded ${email} to free tier`);
  return { status: 200, body: { success: true, tier: "free" } };
}

/**
 * subscription_payment_failed — durable audit + log. We insert a row into
 * `payment_alerts` so a downstream notifier (email/Telegram/webhook
 * worker, wired in a later polish) can poll `WHERE notified_at IS NULL`
 * and dispatch. Edge Function logs roll off after 7 days on free tier;
 * the table is the contract.
 *
 * We do NOT downgrade tier here — LS retries on its own, and if retries
 * ultimately fail, `subscription_expired` fires. This handler is purely
 * a "heads-up the user's payment didn't go through" signal.
 *
 * Returns 200 even when the audit insert errors, because LS will retry
 * the webhook on non-2xx and a retry storm on transient DB errors is
 * worse than a missed log line. The `audit_row_inserted` boolean in the
 * response body lets callers / tests assert insert success without
 * affecting the webhook contract.
 *
 * (v0.2.31 #24: was previously a stub that returned 200 with only a
 * console.warn. Pre-fix, payment failures only surfaced in 7-day-rolling
 * function logs — a churn-risk signal nobody could action.)
 */
export async function handlePaymentFailed(
  supabase: SupabaseClient,
  payload: any,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const email: string | undefined = payload.data?.attributes?.user_email;
  const subId: string | undefined = payload.data?.id;
  console.warn(`[payment_failed] subscription=${subId} email=${email ?? "?"}`);

  let auditOk = false;
  try {
    const { error } = await supabase.from("payment_alerts").insert({
      alert_kind: "payment_failed",
      subscription_id: subId,
      user_email: email,
      payload: payload,
    });
    if (error) {
      // DB-level error (constraint violation, schema drift, RLS surprise).
      // Log but don't propagate — LS retries on non-2xx and we don't
      // want to retry-storm a transient DB issue.
      console.error(`[payment_failed] audit insert error: ${error.message}`);
    } else {
      auditOk = true;
    }
  } catch (e) {
    // Network / client-construction-time throw (e.g. supabase mocked
    // in a test). Treat the same as a DB error — log, return 200.
    console.error(`[payment_failed] audit insert threw: ${e}`);
  }

  // TODO(notifications-transport): wire email/Telegram dispatch when
  // transport is configured. Until then, the payment_alerts table is
  // the durable record a downstream notifier polls.
  return { status: 200, body: { logged: true, audit_row_inserted: auditOk } };
}

/**
 * order_refunded — refund issued. Downgrade orchestrator_tier to free
 * immediately and clear ends_at. Also remove appId from profiles.apps so
 * the user loses access at next validate-tier call.
 */
export async function handleOrderRefunded(
  supabase: SupabaseClient,
  payload: any,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const attrs = payload.data?.attributes ?? {};
  const email: string | undefined = attrs.user_email;
  const firstItem = attrs.first_order_item;
  const variantId = String(firstItem?.variant_id ?? "");
  const refundReason: string | undefined = attrs.refunded_reason;

  if (!email) return { status: 400, body: { error: "Missing user_email" } };

  const mapping = lookupVariant(variantId);
  if (!mapping) {
    console.log(`[refunded] Unknown variant ${variantId} for ${email}`);
    return { status: 200, body: { message: "Unknown variant, ignored" } };
  }

  const userId = await findUserIdByEmail(supabase, email);
  if (!userId) return { status: 200, body: { message: "User not found, ignored" } };

  const { data: profile } = await supabase
    .from("profiles")
    .select("apps")
    .eq("id", userId)
    .single();
  const apps: string[] = (profile?.apps ?? []).filter(
    (a: string) => a !== mapping.appId,
  );

  const update: Record<string, unknown> = { apps };
  if (mapping.appId === "orchestrator") {
    update.orchestrator_tier = "free";
    update.orchestrator_tier_ends_at = null;
  }

  const { error } = await supabase.from("profiles").update(update).eq("id", userId);
  if (error) {
    console.error("[refunded] Failed to revoke:", error);
    return { status: 500, body: { error: "Failed to process refund" } };
  }

  console.log(
    `[refunded] ${email} variant=${variantId} reason=${refundReason ?? "n/a"} → access revoked`,
  );
  return { status: 200, body: { success: true, refunded: mapping.appId } };
}

/**
 * Dispatcher used by index.ts after signature verification + payload parse.
 * Returns { status, body } that the handler turns into a Response.
 *
 * The order_created branch is intentionally NOT handled here — index.ts
 * already has that logic; this module only adds the new lifecycle events
 * + the tier-upgrade hook (`activateAppForUser`) used inside order_created.
 */
export async function dispatchLifecycleEvent(
  supabase: SupabaseClient,
  eventName: string,
  payload: any,
): Promise<{ status: number; body: Record<string, unknown> } | null> {
  switch (eventName) {
    case "subscription_cancelled":
      return handleSubscriptionCancelled(supabase, payload);
    case "subscription_expired":
      return handleSubscriptionExpired(supabase, payload);
    case "subscription_payment_failed":
      return handlePaymentFailed(supabase, payload);
    case "order_refunded":
      return handleOrderRefunded(supabase, payload);
    default:
      return null; // not ours — caller should fall through (e.g., order_created)
  }
}
