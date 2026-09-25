// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.97 — the home page's subscription-usage card.
//
// Claude Code's own Account & Usage view, with the panel pointed at the
// gateway, shows a dollar cost priced at API rates — meaningless on a
// subscription. The real windows are computed once, in the gateway
// (`model_router.usage_windows`, `GET /usage/windows`), and reach the launcher
// through `model_gateway_usage_windows` -> `python -m vco_lib.gateway_usage`.
//
// This file decides only how the card PRESENTS them, and is pure so vitest can
// reach every branch without a DOM. The rules it keeps from the backend:
//   * an unknown window stays unknown — `percent: null` renders as "unknown"
//     with its reason, never as an empty (0 %) or full (100 %) bar;
//   * a vendor with no programmatic quota shows TOKENS, labelled as tokens,
//     never a bar.

import { invoke, tauriAvailable } from '$lib/tauri';

/** Mirrors `model_router.usage_windows._window_dict`. */
export interface UsageWindow {
  id: string;
  label: string;
  kind: 'percent';
  percent: number | null;
  resets_at: string | null;
  source: string;
  fetched_at: string;
  unknown_reason: string | null;
}

/** Mirrors the `tokens` block of `UsageWindows.snapshot`. */
export interface UsageTokens {
  tokens: number;
  requests: number;
  unit: 'tokens';
  period: 'month';
  period_start: string;
  counted_since: string | null;
  source: string;
  fetched_at: string;
}

export interface UsageVendor {
  id: string;
  label: string;
  name: string;
  state: 'ok' | 'pending' | 'unconfigured' | 'unknown' | string;
  problem: string | null;
  plan: string | null;
  windows: UsageWindow[];
  tokens?: UsageTokens;
}

export interface UsageSnapshot {
  generated_at: string;
  last_refresh_at: string | null;
  refreshing: boolean;
  refresh_interval_s: number;
  vendors: UsageVendor[];
}

/** Mirrors `vco_lib.gateway_usage` / `commands::gateway_usage`. */
export type UsageBridgeResult =
  | { ok: true; port: number; snapshot: UsageSnapshot }
  | { ok: false; reason: string; message: string };

/** The card polls while mounted. The gateway refreshes its sources every
 * few minutes; polling faster only re-reads its cache. */
export const USAGE_POLL_MS = 60_000;
/** Right after the first read the gateway is usually still fetching. */
export const USAGE_RETRY_WHILE_REFRESHING_MS = 4_000;

/** The command itself failed (no interpreter, timeout, a traceback). */
export const REASON_BRIDGE_ERROR = 'bridge_error';
/** `vco_lib` / the gateway package did not import — never hidden. */
export const REASON_BROKEN_INSTALL = 'broken_install';
/** The one failure the card is QUIET about. */
export const REASON_NOT_RUNNING = 'not_running';

function errorText(err: unknown): string {
  if (err instanceof Error) return err.message;
  return typeof err === 'string' ? err : JSON.stringify(err);
}

/**
 * Ask the backend. `null` ONLY in browser mode (no backend to ask). A
 * rejected command is NOT swallowed into `null` the way `safeInvoke` would —
 * that hid the card, so a broken bridge looked exactly like "no gateway"
 * (review F4). It becomes a `bridge_error` answer carrying the error text.
 */
export async function fetchUsage(): Promise<UsageBridgeResult | null> {
  if (!tauriAvailable()) return null;
  try {
    return await invoke<UsageBridgeResult>('model_gateway_usage_windows');
  } catch (err) {
    return {
      ok: false,
      reason: REASON_BRIDGE_ERROR,
      message: `could not read subscription usage: ${errorText(err)}`,
    };
  }
}

/**
 * Whether the card renders at all. Hidden only where there is nothing to
 * say: browser mode, and a gateway that is not running (a machine that does
 * not use it gets no permanent error tile). Every other failure — a broken
 * install above all — is shown, because it is something the user can fix.
 */
export function cardVisible(result: UsageBridgeResult | null): boolean {
  if (result === null) return false;
  if (result.ok) return true;
  return result.reason !== REASON_NOT_RUNNING;
}

/** What the card renders: the answer, plus a note when it is a kept one. */
export interface UsageCardState {
  result: UsageBridgeResult | null;
  /** Set when a refresh failed transiently and the last good answer is kept. */
  warning: string | null;
}

export const INITIAL_CARD_STATE: UsageCardState = { result: null, warning: null };

/** Failures that say nothing about the numbers already on screen. */
const TRANSIENT = new Set([REASON_BRIDGE_ERROR, 'unreachable']);

/**
 * Fold one poll into the card. A transient failure after a good reading
 * keeps that reading, NAMED as not refreshed — rather than blanking the card
 * for a poll interval (review F4). A broken install, and any failure with
 * no good reading to keep, replaces what is shown so it is seen.
 */
export function settle(prev: UsageCardState, next: UsageBridgeResult | null): UsageCardState {
  if (next === null || next.ok) return { result: next, warning: null };
  if (TRANSIENT.has(next.reason) && prev.result?.ok) {
    return { result: prev.result, warning: `not refreshed — ${next.message}` };
  }
  return { result: next, warning: null };
}

export type BarTone = 'teal' | 'purple' | 'pink';

/** Teal while there is room, purple past 70 %, pink past 90 %. */
export function barTone(percent: number): BarTone {
  if (percent >= 90) return 'pink';
  if (percent >= 70) return 'purple';
  return 'teal';
}

/** CSS width for a known percent, clamped to the track. */
export function barWidth(percent: number): string {
  const clamped = Math.max(0, Math.min(100, percent));
  return `${clamped}%`;
}

/** "resets in 2h 14m" / "resets in 3d 4h" / "resets in 12m". */
export function describeCountdown(resetsAt: string | null, nowMs: number): string {
  if (!resetsAt) return '';
  const target = Date.parse(resetsAt);
  if (Number.isNaN(target)) return '';
  const minutes = Math.floor((target - nowMs) / 60_000);
  if (minutes < 1) return 'resetting now';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  if (days > 0) return `resets in ${days}d ${hours}h`;
  if (hours > 0) return `resets in ${hours}h ${mins}m`;
  return `resets in ${mins}m`;
}

const UNKNOWN_REASONS: Record<string, string> = {
  not_reported: 'not reported by the vendor',
  stale: 'last reading is too old',
  reset_passed: 'window reset since the last reading',
};

export function unknownLabel(reason: string | null): string {
  return `unknown — ${UNKNOWN_REASONS[reason ?? ''] ?? 'no reading'}`;
}

/** The one-line status beside a vendor's name when it has no numbers. */
export function vendorStatus(vendor: UsageVendor): string | null {
  switch (vendor.state) {
    case 'ok':
      return null;
    case 'pending':
      return 'reading…';
    case 'unconfigured':
      return vendor.problem ?? 'not configured';
    default:
      return vendor.problem ?? 'unknown';
  }
}

/** "1,234,567 tokens this month" (+ " since 10 Sep" when the ledger
 * does not reach back to the month start). */
export function describeTokens(tokens: UsageTokens, locale?: string): string {
  const count = tokens.tokens.toLocaleString(locale);
  if (!tokens.counted_since) return `${count} tokens this month`;
  const since = new Date(tokens.counted_since).toLocaleDateString(locale, {
    day: 'numeric',
    month: 'short',
  });
  return `${count} tokens since ${since}`;
}

/** Vendors worth a row: anything with a window, tokens, or a status to say. */
export function visibleVendors(snapshot: UsageSnapshot): UsageVendor[] {
  return snapshot.vendors.filter(
    (v) => v.windows.length > 0 || v.tokens !== undefined || v.state !== 'ok',
  );
}

/** When to poll next: soon while the gateway is still fetching. */
export function nextPollMs(result: UsageBridgeResult | null): number {
  if (result?.ok && result.snapshot.refreshing) return USAGE_RETRY_WHILE_REFRESHING_MS;
  return USAGE_POLL_MS;
}
