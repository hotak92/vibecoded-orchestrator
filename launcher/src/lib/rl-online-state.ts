// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106 (owner rulings 2026-11-12): what the RL panel says about a
// project's reranking — per-project online learning, opt-in, and a serving
// gate judged on the project's CUMULATIVE PREQUENTIAL record: every query is
// scored by the model in place when it arrived, before the model trained on
// it, and the k-th check of the whole record runs at confidence
// 1 - 0.10 / (k (k+1)) (a 10% total error budget over all checks). The model
// reranks only while the lower bounds show NDCG@10 >= +2% over plain cosine
// AND the cited node's MRR@10 not below cosine's; no verdict before the record
// holds `min_queries` queries and `min_cited_queries` citing ones.
//
// Inputs (both read-only):
//   * whether RL scoring is ON for the project — the enable cascade
//     (`module_enable_state`, `effective`) with the lock on top
//     (`lock_reason`): OFF means the container is never called;
//   * the container's `rl_online_state` row (module DB, written after every
//     online round by `rl_server._push_rl_online_state`; migrations
//     `claude_mcp_servers/rl_reranker/db/0007_rl_online_state.sql`,
//     `0008_rl_online_state_bounds.sql` and `0009_rl_online_state_prequential.sql`).
//
// Pure (no invoke, no DOM) so every branch is unit-testable; the panel
// (`RlRerankerStatusPanel.svelte`) only renders what this returns.

/** The `rl_online_state` row (column names of migrations 0007 + 0008 + 0009). */
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
  serving_turned_off?: number | null;
  /** 0007, since 0009 about the whole prequential record. */
  last_eval_at?: number | null;
  ndcg_gain?: number | null;
  ndcg?: number | null;
  cos_ndcg?: number | null;
  mrr?: number | null;
  cos_mrr?: number | null;
  cited_queries?: number | null;
  /** 0008: the last check's bounds and confidence (null = undefined / no check). */
  ndcg_gain_lb?: number | null;
  mrr_diff_lb?: number | null;
  gate_confidence?: number | null;
  /** 0009: the record's size, the verdict minimums, the check count, the served searches. */
  record_queries?: number | null;
  min_queries?: number | null;
  min_cited_queries?: number | null;
  checks?: number | null;
  error_budget?: number | null;
  served_queries?: number | null;
  served_cited_queries?: number | null;
  served_ndcg_gain?: number | null;
  served_mrr?: number | null;
  served_cos_mrr?: number | null;
}

/** The fields the panel reads (passed to `module_db_read_row`). */
export const RL_ONLINE_STATE_FIELDS: readonly string[] = [
  'embedding_source', 'refreshed_at', 'serving', 'reason', 'training_enabled',
  'events_seen', 'queries_trained', 'train_steps', 'rounds', 'serving_turned_off',
  'last_eval_at', 'ndcg_gain', 'ndcg', 'cos_ndcg', 'mrr', 'cos_mrr', 'cited_queries',
  'ndcg_gain_lb', 'mrr_diff_lb', 'gate_confidence',
  'record_queries', 'min_queries', 'min_cited_queries', 'checks', 'error_budget',
  'served_queries', 'served_cited_queries', 'served_ndcg_gain', 'served_mrr', 'served_cos_mrr',
];

export type RlOnlineKey = 'unknown' | 'off' | 'locked' | 'waiting' | 'not_serving' | 'serving';

export interface RlOnlineSummary {
  key: RlOnlineKey;
  /** One line: is the project's model reranking its searches, and why. */
  headline: string;
  /** Detail lines (counters, the record, the last check, the served searches). */
  details: string[];
}

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function n(v: number | null | undefined): number {
  return isNum(v) ? v : 0;
}

function pct(v: number | null | undefined): string {
  if (!isNum(v)) return 'n/a';
  const s = (100 * v).toFixed(2);
  return `${v >= 0 ? '+' : ''}${s}%`;
}

function num(v: number | null | undefined, digits = 3): string {
  return isNum(v) ? v.toFixed(digits) : 'n/a';
}

function signed(v: number | null | undefined, digits = 3): string {
  if (!isNum(v)) return 'n/a';
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`;
}

/**
 * A check's confidence: `95.0%` while its error rate is at least 0.1%, else
 * `1 - 3.1e-8` (late checks run at tiny error rates; a percentage would read
 * 100.0% and hide that).
 */
export function confidenceText(c: number | null | undefined): string {
  if (!isNum(c)) return 'n/a';
  const alpha = 1 - c;
  if (alpha >= 1e-3) return `${(100 * c).toFixed(1)}%`;
  return `1 - ${alpha.toExponential(1)}`;
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
    `Record (each query scored before the model trained on it): ${n(row.record_queries)} ` +
      `queries, ${n(row.cited_queries)} of them name a node — a verdict needs ` +
      `${n(row.min_queries)} and ${n(row.min_cited_queries)}` +
      (isNum(row.ndcg_gain)
        ? `; NDCG@10 ${pct(row.ndcg_gain)} vs plain cosine, cited-node MRR@10 ` +
          `${num(row.mrr)} vs ${num(row.cos_mrr)}`
        : ''),
  );
  const checks = n(row.checks);
  if (checks > 0) {
    const budget = isNum(row.error_budget) ? `${Math.round(100 * row.error_budget)}%` : 'n/a';
    details.push(
      `Check ${checks} (lower bounds at confidence ${confidenceText(row.gate_confidence)} ` +
        `decide; ${budget} total error budget over all checks): NDCG@10 gain ` +
        `${pct(row.ndcg_gain_lb)} (needs +2.00%), cited-node MRR@10 difference ` +
        `${signed(row.mrr_diff_lb)} (must not be below 0)`,
    );
  }
  if (n(row.served_queries) > 0) {
    details.push(
      `Searches the model served: ${n(row.served_queries)} ` +
        `(${n(row.served_cited_queries)} name a node) — NDCG@10 ${pct(row.served_ndcg_gain)} ` +
        `vs plain cosine, cited-node MRR@10 ${num(row.served_mrr)} vs ${num(row.served_cos_mrr)}`,
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
  // No check yet = the record is still below the verdict minimums.
  const waiting = checks === 0;
  return {
    key: waiting ? 'waiting' : 'not_serving',
    headline:
      "On — searches keep their plain order: this project's model is not served " +
      `(${row.reason || 'it has not passed its check'}).`,
    details,
  };
}
