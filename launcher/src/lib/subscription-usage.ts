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
//   * a vendor with no programmatic quota has no row here: the gateway's own
//     token ledger stays in the snapshot (still collected) but is never
//     rendered — it measures what this gateway routed, not plan usage;
//   * the one exception is NAMED as one: Kimi's 5h window, when its live
//     reading is unknown, may carry a ledger ESTIMATE (`source:
//     'ledger_estimate'`, v0.2.104) — shown as `~N%` with what it counted,
//     never as a plain vendor reading.

import { invoke, tauriAvailable } from '$lib/tauri';

/** Mirrors the `estimate` block of `UsageWindows._estimate_dict`. */
export interface UsageEstimate {
  tokens: number;
  requests: number;
  budget: number;
  window_start: string;
  counted: string[];
  /** Always `this_gateway`: only this machine's gateway traffic is counted. */
  scope: string;
}

/** Mirrors `model_router.usage_windows._window_dict` (and `_estimate_dict`). */
export interface UsageWindow {
  id: string;
  label: string;
  kind: 'percent';
  percent: number | null;
  resets_at: string | null;
  source: string;
  fetched_at: string;
  unknown_reason: string | null;
  /** Present only on a ledger estimate (`source: 'ledger_estimate'`). */
  estimate?: UsageEstimate;
}

/** Mirrors `model_router.usage_windows.SOURCE_LEDGER_ESTIMATE`. */
export const SOURCE_LEDGER_ESTIMATE = 'ledger_estimate';

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

/**
 * Mirrors one row of the `agents` block of `GET /usage/windows`
 * (`UsageLedger.agent_rows`, v0.2.102 G3): the model that ANSWERED that
 * (session, agent) identity's last turn. `agent` is `null` for the main
 * chat, which is an identity like any other.
 */
export interface UsageAgentRow {
  session: string | null;
  agent: string | null;
  parent_agent: string | null;
  model: string;
  route: string;
  fallback_from: string;
  /**
   * v0.2.104: how the answering route authenticated — `oauth` (Claude
   * login), `api_key` (Claude Console API key: the `claude-gw/api/` route or
   * the opt-in subscription fallback) or `vendor_key`. Absent from an older
   * gateway.
   */
  auth?: string;
  at: string;
}

export interface UsageSnapshot {
  generated_at: string;
  last_refresh_at: string | null;
  refreshing: boolean;
  refresh_interval_s: number;
  vendors: UsageVendor[];
  /** Absent from a gateway older than v0.2.102 — rendered as nothing. */
  agents?: UsageAgentRow[];
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
/** The running gateway serves older code than the checkout (`vco_lib.gateway_usage.REASON_OUTDATED`). */
export const REASON_OUTDATED_GATEWAY = 'outdated_gateway';

/**
 * Show the "Restart gateway…" affordance? ONLY for an outdated gateway
 * (v0.2.101, P299-A2) — the one usage failure a restart actually fixes.
 * Every other failure (no token, unreachable, a broken install) would make
 * the button a no-op at best, and at worst end live agent sessions for
 * nothing: the restart it offers kills every chat routed through the
 * gateway. One home so the card's markup and the tests read the same rule.
 */
export function restartOffered(result: UsageBridgeResult | null): boolean {
  return !!result && !result.ok && result.reason === REASON_OUTDATED_GATEWAY;
}

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

/** Is this window the gateway's own estimate rather than the vendor's reading? */
export function isEstimate(window: UsageWindow): boolean {
  return window.source === SOURCE_LEDGER_ESTIMATE;
}

/**
 * `37%` for a vendor reading, `~37%` for an estimate — the same `~` the
 * gateway's status line and picker label use (`usage_windows._window_text`).
 */
export function percentText(window: UsageWindow & { percent: number }): string {
  return `${isEstimate(window) ? '~' : ''}${Math.round(window.percent)}%`;
}

/** What an estimate is made of, for the row's note; `null` for a reading. */
export function estimateNote(window: UsageWindow): string | null {
  if (!isEstimate(window)) return null;
  const est = window.estimate;
  if (!est) return "estimate from this machine's gateway traffic";
  const n = (value: number) => value.toLocaleString('en-US');
  return (
    `estimate: ${n(est.tokens)} of ${n(est.budget)} tokens routed by this ` +
    "machine's gateway — not the vendor's reading"
  );
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

/** Vendors worth a row: anything with a window or a status to say. A
 * tokens-only vendor is not one — the ledger total is collected but never
 * rendered (owner 2026-09-28). */
export function visibleVendors(snapshot: UsageSnapshot): UsageVendor[] {
  return snapshot.vendors.filter((v) => v.windows.length > 0 || v.state !== 'ok');
}

/** When to poll next: soon while the gateway is still fetching. */
export function nextPollMs(result: UsageBridgeResult | null): number {
  if (result?.ok && result.snapshot.refreshing) return USAGE_RETRY_WHILE_REFRESHING_MS;
  return USAGE_POLL_MS;
}

/**
 * The identities worth a row in the card's attribution list (v0.2.102, G3).
 * The harness's agents GUI shows the SESSION model for every lane
 * (anthropics/claude-code#93324), so this is where VCO says which model
 * actually answered each. A gateway older than v0.2.102 sends no `agents`
 * block — that reads as an empty list, never as an error.
 *
 * Main-chat rows (null agent) are hidden unless a fallback served the turn
 * (owner 2026-10-15): the user already knows their chat's model, and every
 * session's background Haiku calls produced one row each — dozens of
 * identical "main chat / claude-haiku-…" lines that buried the subagent rows
 * this list exists for. A fallback is the one main-chat fact worth showing.
 */
export function visibleAgents(snapshot: UsageSnapshot): UsageAgentRow[] {
  // v0.2.104: a main-chat turn billed to the Claude API key is shown too —
  // money left a different pool, which the user must be able to see.
  return (snapshot.agents ?? []).filter(
    (row) => row.agent !== null || row.fallback_from || row.auth === 'api_key',
  );
}

/** The identity's name: the agent id, or "main chat" for the null agent. */
export function agentIdentity(row: UsageAgentRow): string {
  return row.agent ?? 'main chat';
}

/**
 * What answered, and how it was reached: the ANSWERING model — never the
 * requested id — with the fallback escape beside it when the owner's ladder
 * served the turn (no hidden remaps).
 */
export function agentAnswer(row: UsageAgentRow): string {
  const via = row.auth === 'api_key' ? ' via Claude API key' : '';
  return row.fallback_from
    ? `${row.model}${via} (fallback from ${row.fallback_from})`
    : `${row.model}${via}`;
}
