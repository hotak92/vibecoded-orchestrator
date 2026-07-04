// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.73 — tests for the Update-all live-progress reducer.
//
// Pins the `update_all_progress` event → progress-list state transitions the
// modal renders during phase==='running', plus the warning-disclosure
// severity classification the report's expandable warnings use. Pure-node
// vitest env (no DOM), so we exercise the extracted reducer directly rather
// than mounting the Svelte component.

import { describe, it, expect } from 'vitest';
import {
  type UpdateAllProgress,
  emptyProgressState,
  applyProgressEvent,
  progressTotal,
  progressIcon,
} from './update-all-progress';
import { isErrorWarning } from '$lib/warning-severity';

function started(
  project_id: string,
  project_name: string,
  index: number,
  total: number,
): UpdateAllProgress {
  return {
    phase: 'started',
    project_id,
    project_name,
    index,
    total,
    status: null,
    warnings_count: null,
  };
}

function finished(
  project_id: string,
  project_name: string,
  index: number,
  total: number,
  status: 'succeeded' | 'failed' | 'skipped',
  warnings_count: number,
): UpdateAllProgress {
  return {
    phase: 'finished',
    project_id,
    project_name,
    index,
    total,
    status,
    warnings_count,
  };
}

// Fold an ordered event list through the reducer (mirrors what the modal's
// listener does event-by-event).
function fold(events: UpdateAllProgress[]) {
  return events.reduce(applyProgressEvent, emptyProgressState());
}

describe('applyProgressEvent — live per-project progress', () => {
  it('starts empty', () => {
    const s = emptyProgressState();
    expect(s.rows).toEqual([]);
    expect(s.current).toBeNull();
  });

  it('a `started` event appends a running row and sets the headline', () => {
    const s = applyProgressEvent(
      emptyProgressState(),
      started('a', 'Project Alpha', 1, 3),
    );
    expect(s.rows).toHaveLength(1);
    expect(s.rows[0]).toMatchObject({
      project_id: 'a',
      project_name: 'Project Alpha',
      status: 'running',
    });
    expect(s.current).toEqual({ name: 'Project Alpha', index: 1, total: 3 });
  });

  it('a `finished` event flips the same row to its terminal status', () => {
    const s = fold([
      started('a', 'Project Alpha', 1, 3),
      finished('a', 'Project Alpha', 1, 3, 'succeeded', 2),
    ]);
    expect(s.rows).toHaveLength(1);
    expect(s.rows[0].status).toBe('succeeded');
    expect(s.rows[0].warnings_count).toBe(2);
    // Headline cleared once the current project finishes.
    expect(s.current).toBeNull();
  });

  it('keeps rows keyed and ordered across a multi-project run', () => {
    const s = fold([
      started('a', 'Alpha', 1, 3),
      finished('a', 'Alpha', 1, 3, 'succeeded', 0),
      started('b', 'Beta', 2, 3),
      finished('b', 'Beta', 2, 3, 'failed', 0),
      started('c', 'Gamma', 3, 3),
      finished('c', 'Gamma', 3, 3, 'succeeded', 1),
    ]);
    expect(s.rows.map((r) => r.project_id)).toEqual(['a', 'b', 'c']);
    expect(s.rows.map((r) => r.status)).toEqual([
      'succeeded',
      'failed',
      'succeeded',
    ]);
    // No project is "current" once the whole batch has finished.
    expect(s.current).toBeNull();
  });

  it('shows the in-flight project as current while it runs', () => {
    // Alpha finished, Beta started but not yet finished.
    const s = fold([
      started('a', 'Alpha', 1, 3),
      finished('a', 'Alpha', 1, 3, 'succeeded', 0),
      started('b', 'Beta', 2, 3),
    ]);
    expect(s.current).toEqual({ name: 'Beta', index: 2, total: 3 });
    expect(s.rows[1].status).toBe('running');
  });

  it('handles a stop_on_error skip (finished-only, no started)', () => {
    // Alpha fails, Beta + Gamma are skipped — each emits a single
    // finished/skipped event with no preceding `started`.
    const s = fold([
      started('a', 'Alpha', 1, 3),
      finished('a', 'Alpha', 1, 3, 'failed', 0),
      finished('b', 'Beta', 2, 3, 'skipped', 0),
      finished('c', 'Gamma', 3, 3, 'skipped', 0),
    ]);
    expect(s.rows.map((r) => r.status)).toEqual([
      'failed',
      'skipped',
      'skipped',
    ]);
    expect(s.rows.map((r) => r.project_id)).toEqual(['a', 'b', 'c']);
    expect(s.current).toBeNull();
  });

  it('does not mutate the input state (returns a new snapshot)', () => {
    const s0 = emptyProgressState();
    const s1 = applyProgressEvent(s0, started('a', 'Alpha', 1, 1));
    expect(s0.rows).toHaveLength(0); // original untouched
    expect(s1.rows).toHaveLength(1);
    expect(s1).not.toBe(s0);
  });
});

describe('progressTotal', () => {
  it('falls back to the hint count before any event arrives', () => {
    expect(progressTotal(emptyProgressState(), 5)).toBe(5);
  });
  it('uses the total carried by the events once they arrive', () => {
    const s = applyProgressEvent(emptyProgressState(), started('a', 'A', 1, 7));
    expect(progressTotal(s, 5)).toBe(7);
  });
});

describe('progressIcon', () => {
  it('maps each status to a distinct glyph', () => {
    expect(progressIcon('running')).toBe('⟳');
    expect(progressIcon('succeeded')).toBe('✓');
    expect(progressIcon('failed')).toBe('✗');
    expect(progressIcon('skipped')).toBe('–');
  });
});

describe('warning disclosure severity (report expandable warnings)', () => {
  // The modal tints each expanded warning info/error via isErrorWarning — pin
  // the two representative cases the disclosure relies on.
  it('classifies a genuine failure warning as error', () => {
    expect(isErrorWarning('container failed to start: exit 1')).toBe(true);
  });
  it('classifies a clean deferral warning as info (not error)', () => {
    expect(
      isErrorWarning('bootstrap deferred: collections will be created when'),
    ).toBe(false);
  });
});
