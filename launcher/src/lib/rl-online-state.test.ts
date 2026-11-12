// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106 — `summarizeOnlineState`: the RL panel's per-project reranking
// line (opt-in toggle, serving gate, last held-out check).

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { RL_ONLINE_STATE_FIELDS, summarizeOnlineState } from './rl-online-state';

const ROW = {
  embedding_source: 'qwen3',
  serving: 0,
  reason: 'NDCG@10 +0.80% vs plain cosine; needs +2.00%',
  training_enabled: 1,
  events_seen: 900,
  queries_trained: 520,
  train_steps: 65,
  rounds: 28,
  holdout_queries: 300,
  holdout_target: 300,
  serving_turned_off: 0,
  ndcg_gain: 0.008,
  mrr: 0.31,
  cos_mrr: 0.33,
  cited_queries: 14,
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

  it('on, not served: the reason and the last check are shown', () => {
    const s = summarizeOnlineState(true, null, ROW);
    expect(s.key).toBe('not_serving');
    expect(s.headline).toContain('plain order');
    expect(s.headline).toContain(ROW.reason);
    expect(s.details.join('\n')).toMatch(/NDCG@10 \+0\.80% vs plain cosine \(needs \+2\.00%\)/);
    expect(s.details.join('\n')).toMatch(/MRR@10 0\.310 vs 0\.330/);
    expect(s.details.join('\n')).toMatch(/900 citation events received, 520 trained on/);
  });

  it('on, window not full: waiting', () => {
    const s = summarizeOnlineState(true, null, { ...ROW, holdout_queries: 120 });
    expect(s.key).toBe('waiting');
    expect(s.details.join('\n')).toContain('120/300');
  });

  it('on, served: the model reranks; stop count shown', () => {
    const s = summarizeOnlineState(true, null, {
      ...ROW, serving: 1, reason: 'NDCG@10 +2.40% over plain cosine', ndcg_gain: 0.024,
      serving_turned_off: 2,
    });
    expect(s.key).toBe('serving');
    expect(s.headline).toContain("this project's model reranks its searches");
    expect(s.details.join('\n')).toMatch(/stopped serving the model 2 time/);
  });

  it('training off for the project is said', () => {
    const s = summarizeOnlineState(true, null, { ...ROW, training_enabled: 0 });
    expect(s.details[0]).toMatch(/online training is off for this project/);
  });
});

describe('the fields read match the migration that creates the table', () => {
  it('every field is a column of rl_online_state', () => {
    const sql = readFileSync(
      fileURLToPath(new URL(
        '../../../claude_mcp_servers/rl_reranker/db/0007_rl_online_state.sql', import.meta.url)),
      'utf8',
    );
    for (const f of RL_ONLINE_STATE_FIELDS) {
      expect(sql).toMatch(new RegExp(`^\\s+${f}\\s`, 'm'));
    }
  });
});
