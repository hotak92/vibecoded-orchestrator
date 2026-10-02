// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from 'vitest';
import { summarizeCollection } from './rl-collection-stat';

describe('summarizeCollection (the RL "is data being collected" figure)', () => {
  it('null before the read lands renders nothing', () => {
    expect(summarizeCollection(null)).toBeNull();
  });

  it('zero events says so plainly', () => {
    const r = summarizeCollection({ recent_events_count: 0, total_events_count: 0 });
    expect(r?.key).toBe('none');
    expect(r?.headline).toMatch(/No events collected yet/);
  });

  it('shows the real total and the last-24h count', () => {
    const r = summarizeCollection({ recent_events_count: 37, total_events_count: 1204 });
    expect(r?.key).toBe('collecting');
    expect(r?.headline).toBe('1,204 events collected (37 in the last 24 h)');
  });

  it('W5R-13: breaks the figure down by embedding source (is arctic being saved?)', () => {
    const r = summarizeCollection({
      recent_events_count: 3,
      total_events_count: 1806,
      events_by_embedding_source: [
        { embedding_source: 'arctic', retrieval: 451, citation: 151, total: 602 },
        { embedding_source: 'qwen3', retrieval: 904, citation: 300, total: 1204 },
      ],
    });
    expect(r?.bySource).toEqual([
      'arctic: 602 (retrieval 451, citation 151)',
      'qwen3: 1,204 (retrieval 904, citation 300)',
    ]);
    // Other event types are named, not hidden in the total.
    expect(
      summarizeCollection({
        total_events_count: 5,
        events_by_embedding_source: [{ embedding_source: 'codesage', retrieval: 2, citation: 1, total: 5 }],
      })?.bySource,
    ).toEqual(['codesage: 5 (retrieval 2, citation 1, other 2)']);
    // Pre-v0.2.100 payloads carry no breakdown.
    expect(summarizeCollection({ recent_events_count: 1, total_events_count: 1 })?.bySource).toEqual([]);
  });

  it('singular, and old snapshots without the total field do not crash', () => {
    expect(summarizeCollection({ recent_events_count: 1, total_events_count: 1 })?.headline).toBe(
      '1 event collected (1 in the last 24 h)',
    );
    expect(summarizeCollection({ recent_events_count: 5 })?.key).toBe('none');
  });
});
