// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106 — `summarizeOnlineState`: the RL panel's per-project reranking
// line (opt-in toggle, serving gate on the prequential record, last check,
// served searches).

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  RL_ONLINE_STATE_FIELDS,
  confidenceText,
  summarizeOnlineState,
} from './rl-online-state';

const ROW = {
  embedding_source: 'qwen3',
  serving: 0,
  reason:
    'check 7: NDCG@10 +0.80% vs plain cosine on 900 queries scored before training, ' +
    'lower bound -1.20% at confidence 99.8%; the bound needs +2.00%',
  training_enabled: 1,
  events_seen: 900,
  queries_trained: 880,
  train_steps: 110,
  rounds: 28,
  serving_turned_off: 0,
  ndcg_gain: 0.008,
  mrr: 0.31,
  cos_mrr: 0.33,
  cited_queries: 124,
  ndcg_gain_lb: -0.012,
  mrr_diff_lb: -0.08,
  gate_confidence: 1 - 0.1 / 56,
  record_queries: 900,
  min_queries: 300,
  min_cited_queries: 50,
  checks: 7,
  error_budget: 0.1,
  served_queries: 0,
  served_cited_queries: 0,
  served_ndcg_gain: null,
  served_mrr: null,
  served_cos_mrr: null,
};

describe('summarizeOnlineState', () => {
  it('a set lock wins over everything', () => {
    const s = summarizeOnlineState(true, 'locked for now', ROW);
    expect(s.key).toBe('locked');
    expect(s.headline).toContain('locked for now');
  });

  it('toggle off: no container call, plain order (the opt-in default)', () => {
    const s = summarizeOnlineState(false, null, ROW);
    expect(s.key).toBe('off');
    expect(s.headline).toMatch(/nothing is sent to the reranker/);
  });

  it('toggle not loaded: unknown, never a guess', () => {
    expect(summarizeOnlineState(undefined, null, ROW).key).toBe('unknown');
  });

  it('on, no row yet: waiting for the first round', () => {
    expect(summarizeOnlineState(true, null, null).key).toBe('waiting');
    expect(summarizeOnlineState(true, null, undefined).key).toBe('unknown');
  });

  it('on, not served: the reason, the record and the last check are shown', () => {
    const s = summarizeOnlineState(true, null, ROW);
    expect(s.key).toBe('not_serving');
    expect(s.headline).toContain('plain order');
    expect(s.headline).toContain(ROW.reason);
    const d = s.details.join('\n');
    expect(d).toMatch(/900 citation events received, 880 trained on/);
    expect(d).toMatch(
      /Record \(each query scored before the model trained on it\): 900 queries, 124 of them name a node — a verdict needs 300 and 50; NDCG@10 \+0\.80% vs plain cosine, cited-node MRR@10 0\.310 vs 0\.330/,
    );
    expect(d).toMatch(/Check 7 \(lower bounds at confidence 99\.8% decide; 10% total error budget/);
    expect(d).toMatch(/NDCG@10 gain -1\.20% \(needs \+2\.00%\), cited-node MRR@10 difference -0\.080/);
    expect(d).not.toMatch(/Searches the model served/);
  });

  it('on, below the verdict minimums: waiting, no check line', () => {
    const s = summarizeOnlineState(true, null, {
      ...ROW, checks: 0, record_queries: 120, cited_queries: 30, ndcg_gain_lb: null,
      mrr_diff_lb: null, gate_confidence: null,
      reason: 'collecting queries scored before training (120/300 queries, 30/50 citing a node)',
    });
    expect(s.key).toBe('waiting');
    const d = s.details.join('\n');
    expect(d).toContain('120 queries, 30 of them name a node — a verdict needs 300 and 50');
    expect(d).not.toMatch(/Check \d/);
  });

  it('on, served: the model reranks; served searches and stop count shown', () => {
    const s = summarizeOnlineState(true, null, {
      ...ROW, serving: 1, reason: 'check 12: NDCG@10 +3.40% over plain cosine', ndcg_gain: 0.034,
      checks: 12, gate_confidence: 1 - 0.1 / 156, ndcg_gain_lb: 0.025, mrr_diff_lb: 0.04,
      serving_turned_off: 2, served_queries: 640, served_cited_queries: 81,
      served_ndcg_gain: 0.031, served_mrr: 0.52, served_cos_mrr: 0.4,
    });
    expect(s.key).toBe('serving');
    expect(s.headline).toContain("this project's model reranks its searches");
    const d = s.details.join('\n');
    expect(d).toMatch(
      /Searches the model served: 640 \(81 name a node\) — NDCG@10 \+3\.10% vs plain cosine, cited-node MRR@10 0\.520 vs 0\.400/,
    );
    expect(d).toMatch(/stopped serving the model 2 time/);
  });

  it('a passing point estimate with a failing bound is shown as the bound', () => {
    // measured MRR above cosine, bound below 0: the bound decides
    const s = summarizeOnlineState(true, null, {
      ...ROW, ndcg_gain: 0.03, ndcg_gain_lb: 0.021, mrr: 0.36, cos_mrr: 0.33, mrr_diff_lb: -0.04,
      reason: "check 7: NDCG@10 +3.00% (bound +2.10%) but the cited node's MRR@10 ...: below 0",
    });
    expect(s.key).toBe('not_serving');
    expect(s.details.join('\n')).toMatch(/cited-node MRR@10 difference -0\.040 \(must not be below 0\)/);
  });

  it('an undefined bound reads n/a, never a number', () => {
    const s = summarizeOnlineState(true, null, { ...ROW, mrr_diff_lb: null });
    expect(s.details.join('\n')).toMatch(/MRR@10 difference n\/a/);
  });

  it('training off for the project is said', () => {
    const s = summarizeOnlineState(true, null, { ...ROW, training_enabled: 0 });
    expect(s.details[0]).toMatch(/online training is off for this project/);
  });
});

describe('confidenceText', () => {
  it('a percentage while the error rate is >= 0.1%, else the error rate itself', () => {
    expect(confidenceText(0.95)).toBe('95.0%');
    expect(confidenceText(1 - 0.1 / 6)).toBe('98.3%');
    expect(confidenceText(1 - 0.1 / (1800 * 1801))).toBe('1 - 3.1e-8');
    expect(confidenceText(null)).toBe('n/a');
  });
});

describe('the fields read match the migrations that shape the table', () => {
  it('every field is a column of rl_online_state after 0007 + 0008 + 0009', () => {
    const read = (f: string) => readFileSync(
      fileURLToPath(new URL(`../../../claude_mcp_servers/rl_reranker/db/${f}`, import.meta.url)),
      'utf8',
    );
    const create = read('0007_rl_online_state.sql');
    const alters = read('0008_rl_online_state_bounds.sql') + read('0009_rl_online_state_prequential.sql');
    for (const f of RL_ONLINE_STATE_FIELDS) {
      const inCreate = new RegExp(`^\\s+${f}\\s`, 'm').test(create);
      const added = new RegExp(`ADD COLUMN ${f}\\s`).test(alters);
      const dropped = new RegExp(`DROP COLUMN ${f};`).test(alters);
      expect((inCreate || added) && !dropped, f).toBe(true);
    }
  });
});
