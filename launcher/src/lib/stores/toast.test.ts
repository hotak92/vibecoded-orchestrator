// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.103 (field, 2026-10-14) — toast dwell policy tests.
//
// THE DEFECT: every toast auto-dismissed after a flat 4 s. Outcome toasts
// (a code-graph walk that HAD started) vanished before they could be
// read, so a started walk looked like a failure and got re-clicked.
//
// Pins the policy that now lives in ONE place (stores/toast.ts):
//   * error ≥ 15 s, success/info ≥ 8 s;
//   * long messages dwell proportionally longer;
//   * hovering pauses dismissal; leaving re-arms the computed dwell.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { get } from 'svelte/store';
import { toast, toastDwellMs, TOAST_MIN_DWELL_MS } from './toast';

function liveIds(): number[] {
  return get(toast).map((t) => t.id);
}

function lastId(): number {
  const ids = liveIds();
  if (!ids.length) throw new Error('no live toast');
  return ids[ids.length - 1];
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  // Drain everything so module state does not leak across tests.
  for (const id of liveIds()) toast.dismiss(id);
  vi.useRealTimers();
});

describe('toastDwellMs — the one policy (pure)', () => {
  it('errors dwell at least 15 s', () => {
    expect(TOAST_MIN_DWELL_MS.error).toBeGreaterThanOrEqual(15_000);
    expect(toastDwellMs('error', 'boom')).toBeGreaterThanOrEqual(15_000);
  });

  it('success and info dwell at least 8 s', () => {
    expect(toastDwellMs('success', 'ok')).toBeGreaterThanOrEqual(8_000);
    expect(toastDwellMs('info', 'note')).toBeGreaterThanOrEqual(8_000);
  });

  it('errors dwell longer than successes of the same length', () => {
    expect(toastDwellMs('error', 'x'.repeat(120)))
      .toBeGreaterThan(toastDwellMs('success', 'x'.repeat(120)));
  });

  it('long messages dwell proportionally longer', () => {
    const short = toastDwellMs('info', 'x'.repeat(20));
    const long = toastDwellMs('info', 'x'.repeat(220));
    expect(long).toBeGreaterThan(short + 5_000);
  });
});

describe('store — auto-dismiss follows the policy', () => {
  it('a success toast is still on screen at 7.9 s, gone at 8 s', () => {
    toast.success('Code graph rebuild started');
    const id = lastId();
    vi.advanceTimersByTime(7_999);
    expect(liveIds()).toContain(id);
    vi.advanceTimersByTime(1);
    expect(liveIds()).not.toContain(id);
  });

  it('an error toast survives well past the old flat 4 s', () => {
    toast.error('Code graph rebuild failed');
    const id = lastId();
    vi.advanceTimersByTime(4_000);
    expect(liveIds()).toContain(id);
    vi.advanceTimersByTime(10_999);
    expect(liveIds()).toContain(id);
    vi.advanceTimersByTime(1);
    expect(liveIds()).not.toContain(id);
  });
});

describe('store — hover pauses dismissal', () => {
  it('a hovered toast stays past its dwell and re-arms when unhovered', () => {
    toast.success('started');
    const id = lastId();
    toast.setHovered(id, true);
    vi.advanceTimersByTime(60_000); // dwell long since fired — paused
    expect(liveIds()).toContain(id);
    toast.setHovered(id, false); // re-arms the full computed dwell
    vi.advanceTimersByTime(7_999);
    expect(liveIds()).toContain(id);
    vi.advanceTimersByTime(1);
    expect(liveIds()).not.toContain(id);
  });

  it('manual dismiss wins over a pending pause', () => {
    toast.info('note');
    const id = lastId();
    toast.setHovered(id, true);
    toast.dismiss(id);
    expect(liveIds()).not.toContain(id);
    // Leaving hover after dismissal must not resurrect anything.
    toast.setHovered(id, false);
    expect(liveIds()).not.toContain(id);
  });
});
