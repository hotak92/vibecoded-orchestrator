// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106 (owner rulings 2026-11-12): what the RL panel says about a
// project's reranking — per-project online learning, opt-in, and a serving
// gate (the project's model reranks only while it beats plain cosine on the
// project's own held-out queries: NDCG@10 >= +2% AND the cited node's
// MRR@10 not below cosine's).
//
// Inputs (both read-only):
//   * whether RL scoring is ON for the project — the enable cascade
//     (`module_enable_state`, `effective`) with the lock on top
//     (`lock_reason`): OFF means the container is never called;
//   * the container's `rl_online_state` row (module DB, written after every
//     online round by `rl_server._push_rl_online_state`; migration
//     `claude_mcp_servers/rl_reranker/db/0007_rl_online_state.sql`).
//
// Pure (no invoke, no DOM) so every branch is unit-testable; the panel
// (`RlRerankerStatusPanel.svelte`) only renders what this returns.

/** The `rl_online_state` row (column names of migration 0007). */
export interface RlOnlineStateRow {
  embedding_source?: string | null;
  refreshed_at?: number | null;
  serving?: number | boolean | null;
  reason?: string | null;
  training_enabled?: number | boolean | null;
  events_seen?: number | null;
  queries_trained?: number | null;
  train_steps?: number | null;
  rounds?: number | null;
  holdout_queries?: number | null;
  holdout_target?: number | null;
  serving_turned_off?: number | null;
  last_eval_at?: number | null;
  ndcg_gain?: number | null;
  ndcg?: number | null;
  cos_ndcg?: number | null;
  mrr?: number | null;
  cos_mrr?: number | null;
  cited_queries?: number | null;
}

/** The fields the panel reads (passed to `module_db_read_row`). */
export const RL_ONLINE_STATE_FIELDS: readonly string[] = [
  'embedding_source', 'refreshed_at', 'serving', 'reason', 'training_enabled',
  'events_seen', 'queries_trained', 'train_steps', 'rounds', 'holdout_queries',
  'holdout_target', 'serving_turned_off', 'last_eval_at', 'ndcg_gain', 'ndcg',
  'cos_ndcg', 'mrr', 'cos_mrr', 'cited_queries',
];

export type RlOnlineKey = 'unknown' | 'off' | 'locked' | 'waiting' | 'not_serving' | 'serving';

export interface RlOnlineSummary {
  key: RlOnlineKey;
  /** One line: is the project's model reranking its searches, and why. */
  headline: string;
  /** Detail lines (counters, the last check's numbers). */
  details: string[];
}

function n(v: number | null | undefined): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0;
}

function pct(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return 'n/a';
  const s = (100 * v).toFixed(2);
  return `${v >= 0 ? '+' : ''}${s}%`;
}

function num(v: number | null | undefined, digits = 3): string {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : 'n/a';
}

function truthy(v: number | boolean | null | undefined): boolean {
  return v === true || v === 1;
}

/**
 * The panel's reranking line.
 *
 * - `scoringOn`: the project's RL scoring state (`undefined` = not loaded).
 * - `lockReason`: the shipped lock's reason (`null` = not locked).
 * - `row`: the container's `rl_online_state` row (`null` = no row yet,
 *   `undefined` = not loaded / read failed).
 */
export function summarizeOnlineState(
  scoringOn: boolean | undefined,
  lockReason: string | null | undefined,
  row: RlOnlineStateRow | null | undefined,
): RlOnlineSummary {
  if (lockReason) {
    return { key: 'locked', headline: `Off — RL scoring is locked: ${lockReason}`, details: [] };
  }
  if (scoringOn === undefined) {
    return { key: 'unknown', headline: 'Reranking state unavailable.', details: [] };
  }
  if (!scoringOn) {
    return {
      key: 'off',
      headline:
        'Off for this project — searches keep their plain order and nothing is sent to ' +
        'the reranker. Turn it on in Modules → RL Reranker.',
      details: [],
    };
  }
  if (row === undefined) {
    return {
      key: 'unknown',
      headline: 'On — the reranker has not reported this project yet (container not running?).',
      details: [],
    };
  }
  if (row === null) {
    return {
      key: 'waiting',
      headline:
        "On — learning has not started: no citation round yet. Searches keep their plain " +
        "order until this project's model beats plain search on its own check.",
      details: [],
    };
  }
  const details: string[] = [];
  details.push(
    `${n(row.events_seen)} citation events received, ${n(row.queries_trained)} trained on ` +
      `(${n(row.train_steps)} steps in ${n(row.rounds)} rounds)` +
      (truthy(row.training_enabled) ? '' : ' — online training is off for this project'),
  );
  details.push(
    `Held-out check: ${n(row.holdout_queries)}/${n(row.holdout_target)} recent queries, ` +
      `${n(row.cited_queries)} of them name a node`,
  );
  if (typeof row.ndcg_gain === 'number') {
    details.push(
      `Last check: NDCG@10 ${pct(row.ndcg_gain)} vs plain cosine (needs +2.00%); cited-node ` +
        `MRR@10 ${num(row.mrr)} vs ${num(row.cos_mrr)} (must not be lower)`,
    );
  }
  if (n(row.serving_turned_off) > 0) {
    details.push(`The check has stopped serving the model ${n(row.serving_turned_off)} time(s).`);
  }
  if (truthy(row.serving)) {
    return {
      key: 'serving',
      headline: `On — this project's model reranks its searches (${row.reason || 'passes its check'}).`,
      details,
    };
  }
  const waiting = n(row.holdout_queries) < n(row.holdout_target);
  return {
    key: waiting ? 'waiting' : 'not_serving',
    headline:
      "On — searches keep their plain order: this project's model is not served " +
      `(${row.reason || 'it has not passed its check'}).`,
    details,
  };
}
