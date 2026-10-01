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

  it('singular, and old snapshots without the total field do not crash', () => {
    expect(summarizeCollection({ recent_events_count: 1, total_events_count: 1 })?.headline).toBe(
      '1 event collected (1 in the last 24 h)',
    );
    expect(summarizeCollection({ recent_events_count: 5 })?.key).toBe('none');
  });
});
