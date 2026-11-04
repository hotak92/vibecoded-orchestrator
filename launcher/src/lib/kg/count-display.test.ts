import { describe, expect, it } from 'vitest';
import { formatCount, formatCountAge, sumCounts } from './count-display';

describe('formatCount', () => {
  it('renders a counted number as-is', () => {
    expect(formatCount(0)).toBe('0');
    expect(formatCount(4242)).toBe('4242');
  });

  it('renders never-counted as an em dash', () => {
    expect(formatCount(null)).toBe('—');
    // undefined arrives from optional JSON fields — same state.
    expect(formatCount(undefined)).toBe('—');
  });
});

describe('formatCountAge', () => {
  const now = 1_700_000_000_000;

  it('is empty when there is no timestamp', () => {
    expect(formatCountAge(null, now)).toBe('');
    expect(formatCountAge(undefined, now)).toBe('');
  });

  it('buckets the age humanly', () => {
    expect(formatCountAge(now - 5_000, now)).toBe('counted just now');
    expect(formatCountAge(now - 59_000, now)).toBe('counted just now');
    expect(formatCountAge(now - 60_000, now)).toBe('counted 1 min ago');
    expect(formatCountAge(now - 12 * 60_000, now)).toBe('counted 12 min ago');
    expect(formatCountAge(now - 3 * 3_600_000, now)).toBe('counted 3 h ago');
    expect(formatCountAge(now - 2 * 86_400_000, now)).toBe('counted 2 d ago');
  });

  it('never reports a future count as negative', () => {
    // A clock skew between the launcher and the renderer must not produce
    // "counted -5 min ago".
    expect(formatCountAge(now + 5_000, now)).toBe('counted just now');
  });
});

describe('sumCounts', () => {
  it('sums fully-counted parts', () => {
    expect(sumCounts([1, 2, 3, 0, 4])).toBe(10);
  });

  it('refuses a partial sum: any never-counted class makes the whole total unknown', () => {
    expect(sumCounts([1, null, 3])).toBeNull();
    expect(sumCounts([null])).toBeNull();
  });

  it('sums an empty list to 0', () => {
    expect(sumCounts([])).toBe(0);
  });
});
