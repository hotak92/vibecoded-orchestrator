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
  type KgSyncProgress,
  type CodeGraphBuildProgress,
  type BackgroundActivity,
  type ProgressRow,
  emptyProgressState,
  applyProgressEvent,
  applySubProgress,
  kgSyncSubLabel,
  codeGraphSubLabel,
  emptyBackgroundActivity,
  applyBackgroundActivity,
  backgroundActivityLines,
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

function kgSync(
  overrides: Partial<KgSyncProgress> & { project_id: string },
): KgSyncProgress {
  return {
    status: 'running',
    kg_total: 0,
    kg_succeeded: 0,
    docs_total: 0,
    docs_succeeded: 0,
    current_phase: null,
    ...overrides,
  };
}

function codeGraph(
  overrides: Partial<CodeGraphBuildProgress> & { project_id: string },
): CodeGraphBuildProgress {
  return {
    status: 'running',
    files_analyzed: 0,
    current_phase: null,
    ...overrides,
  };
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

describe('kgSyncSubLabel — condensed KG-sync sub-progress label', () => {
  it('shows the running docs count for the docs phase (the "looks frozen" tail)', () => {
    expect(
      kgSyncSubLabel(
        kgSync({
          project_id: 'a',
          current_phase: 'docs',
          docs_total: 35,
          docs_succeeded: 35,
        }),
      ),
    ).toBe('syncing docs 35/35');
  });

  it('shows the running knowledge count for the knowledge phase', () => {
    expect(
      kgSyncSubLabel(
        kgSync({
          project_id: 'a',
          current_phase: 'knowledge',
          kg_total: 10,
          kg_succeeded: 4,
        }),
      ),
    ).toBe('syncing knowledge 4/10');
  });

  it('labels the scan phase', () => {
    expect(
      kgSyncSubLabel(kgSync({ project_id: 'a', current_phase: 'scan' })),
    ).toBe('scanning knowledge graph…');
  });

  it('falls back to a generic-but-alive line when running with no usable count', () => {
    expect(kgSyncSubLabel(kgSync({ project_id: 'a' }))).toBe(
      'syncing knowledge graph…',
    );
  });

  it('returns null for terminal statuses (clears the sub-line)', () => {
    for (const status of ['success', 'failed', 'skipped', 'pending']) {
      expect(
        kgSyncSubLabel(
          kgSync({ project_id: 'a', status, current_phase: 'docs', docs_total: 5 }),
        ),
      ).toBeNull();
    }
  });
});

describe('codeGraphSubLabel — condensed codegraph sub-progress label', () => {
  it('labels the weaviate-upload phase', () => {
    expect(
      codeGraphSubLabel(
        codeGraph({ project_id: 'a', current_phase: 'weaviate-upload' }),
      ),
    ).toBe('uploading code graph…');
  });

  it('shows the files-analysed count during a language phase', () => {
    expect(
      codeGraphSubLabel(
        codeGraph({
          project_id: 'a',
          current_phase: 'python',
          files_analyzed: 42,
        }),
      ),
    ).toBe('building code graph (42 files)…');
  });

  it('falls back to a generic-but-alive line when running with no count', () => {
    expect(codeGraphSubLabel(codeGraph({ project_id: 'a' }))).toBe(
      'building code graph…',
    );
  });

  it('returns null for terminal statuses (clears the sub-line)', () => {
    for (const status of ['success', 'partial', 'failed', 'skipped', 'pending']) {
      expect(
        codeGraphSubLabel(
          codeGraph({ project_id: 'a', status, files_analyzed: 9 }),
        ),
      ).toBeNull();
    }
  });
});

describe('applySubProgress — fold a sub-label into the running row', () => {
  it('folds a label into the matching running row', () => {
    const s0 = applyProgressEvent(
      emptyProgressState(),
      started('a', 'Alpha', 1, 3),
    );
    const s1 = applySubProgress(s0, 'a', 'syncing docs 5/10');
    expect(s1.rows[0].sub).toBe('syncing docs 5/10');
    expect(s1.rows[0].status).toBe('running');
  });

  it('ignores an unknown project_id (no matching row → no-op)', () => {
    const s0 = applyProgressEvent(
      emptyProgressState(),
      started('a', 'Alpha', 1, 3),
    );
    const s1 = applySubProgress(s0, 'does-not-exist', 'syncing docs 5/10');
    expect(s1).toBe(s0); // identity — nothing changed
    expect(s1.rows[0].sub).toBeNull();
  });

  it('ignores a late event for an already-terminal row', () => {
    // Row 'a' finished; a stray sub-event must not regrow a sub-line under it.
    const s0 = fold([
      started('a', 'Alpha', 1, 3),
      finished('a', 'Alpha', 1, 3, 'succeeded', 0),
    ]);
    expect(s0.rows[0].status).toBe('succeeded');
    const s1 = applySubProgress(s0, 'a', 'syncing docs 9/10');
    expect(s1).toBe(s0); // identity — terminal row untouched
    expect(s1.rows[0].sub).toBeNull();
  });

  it('clears the sub-line when the label is null', () => {
    const s0 = applySubProgress(
      applyProgressEvent(emptyProgressState(), started('a', 'Alpha', 1, 3)),
      'a',
      'syncing docs 5/10',
    );
    expect(s0.rows[0].sub).toBe('syncing docs 5/10');
    const s1 = applySubProgress(s0, 'a', null);
    expect(s1.rows[0].sub).toBeNull();
    expect(s1).not.toBe(s0); // did change
  });

  it('is an identity no-op when the label is unchanged', () => {
    const s0 = applySubProgress(
      applyProgressEvent(emptyProgressState(), started('a', 'Alpha', 1, 3)),
      'a',
      'syncing docs 5/10',
    );
    const s1 = applySubProgress(s0, 'a', 'syncing docs 5/10');
    expect(s1).toBe(s0); // same object — no reactive churn on a repeat event
  });

  it('does not mutate the input state (returns a new snapshot when it changes)', () => {
    const s0 = applyProgressEvent(
      emptyProgressState(),
      started('a', 'Alpha', 1, 3),
    );
    const s1 = applySubProgress(s0, 'a', 'syncing docs 5/10');
    expect(s0.rows[0].sub).toBeNull(); // original untouched
    expect(s1.rows[0].sub).toBe('syncing docs 5/10');
    expect(s1).not.toBe(s0);
    expect(s1.rows).not.toBe(s0.rows);
  });

  it('folds into the correct row when several projects are present', () => {
    const s0 = fold([
      started('a', 'Alpha', 1, 3),
      finished('a', 'Alpha', 1, 3, 'succeeded', 0),
      started('b', 'Beta', 2, 3),
    ]);
    const s1 = applySubProgress(s0, 'b', 'building code graph (7 files)…');
    expect(s1.rows[0].sub).toBeNull(); // Alpha (terminal) untouched
    expect(s1.rows[1].sub).toBe('building code graph (7 files)…');
  });

  it('a `finished` event clears a lingering sub-detail on that row', () => {
    // Sub-detail set while running, then the project finishes → sub forced null
    // so a late fire-and-forget sub-event cannot leave a stale line.
    const withSub = applySubProgress(
      applyProgressEvent(emptyProgressState(), started('a', 'Alpha', 1, 3)),
      'a',
      'syncing docs 8/10',
    );
    expect(withSub.rows[0].sub).toBe('syncing docs 8/10');
    const done = applyProgressEvent(
      withSub,
      finished('a', 'Alpha', 1, 3, 'succeeded', 0),
    );
    expect(done.rows[0].status).toBe('succeeded');
    expect(done.rows[0].sub).toBeNull();
  });

  it('a `started` event initialises the row sub to null', () => {
    const s = applyProgressEvent(
      emptyProgressState(),
      started('a', 'Alpha', 1, 3),
    );
    expect(s.rows[0].sub).toBeNull();
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

// ---------------------------------------------------------------------------
// v0.2.89 review MAJOR-2 — background-activity footer. kg-sync/codegraph are
// fire-and-forget spawns that mostly emit AFTER a project's row goes terminal;
// applySubProgress (correctly) drops those events for the ROW, so the footer
// map is the second surface that keeps post-`finished` embeds visible.
// ---------------------------------------------------------------------------

function row(overrides: Partial<ProgressRow> & Pick<ProgressRow, 'project_id' | 'project_name' | 'status'>): ProgressRow {
  return {
    index: 1,
    total: 2,
    warnings_count: null,
    sub: null,
    ...overrides,
  };
}

describe('applyBackgroundActivity', () => {
  it('records a label for a project', () => {
    const next = applyBackgroundActivity(emptyBackgroundActivity(), 'p1', 'syncing docs 3/10');
    expect(next).toEqual({ p1: 'syncing docs 3/10' });
  });

  it('is an identity no-op when the label is unchanged', () => {
    const state: BackgroundActivity = { p1: 'syncing docs 3/10' };
    expect(applyBackgroundActivity(state, 'p1', 'syncing docs 3/10')).toBe(state);
  });

  it('clears the entry on null (terminal sub-event)', () => {
    const state: BackgroundActivity = { p1: 'syncing docs 3/10', p2: 'building code graph…' };
    const next = applyBackgroundActivity(state, 'p1', null);
    expect(next).toEqual({ p2: 'building code graph…' });
  });

  it('is an identity no-op when clearing an absent entry', () => {
    const state: BackgroundActivity = { p2: 'x' };
    expect(applyBackgroundActivity(state, 'p1', null)).toBe(state);
  });

  it('never mutates the input state', () => {
    const state: BackgroundActivity = { p1: 'a' };
    const frozen = Object.freeze({ ...state });
    applyBackgroundActivity(state, 'p1', 'b');
    applyBackgroundActivity(state, 'p1', null);
    expect(state).toEqual(frozen);
  });
});

describe('backgroundActivityLines', () => {
  const rows: ProgressRow[] = [
    row({ project_id: 'done1', project_name: 'Alpha', status: 'succeeded' }),
    row({ project_id: 'run1', project_name: 'Beta', status: 'running' }),
    row({ project_id: 'fail1', project_name: 'Gamma', status: 'failed' }),
  ];

  it('renders terminal-row activity with the project name', () => {
    const bg: BackgroundActivity = { done1: 'syncing docs 35/120' };
    expect(backgroundActivityLines(bg, rows)).toEqual(['Alpha — syncing docs 35/120']);
  });

  it('skips running rows (their own sub-line already covers them)', () => {
    const bg: BackgroundActivity = { run1: 'syncing docs 1/5' };
    expect(backgroundActivityLines(bg, rows)).toEqual([]);
  });

  it('skips unknown project_ids (sub-event outside this run)', () => {
    const bg: BackgroundActivity = { stranger: 'syncing docs 1/5' };
    expect(backgroundActivityLines(bg, rows)).toEqual([]);
  });

  it('renders failed rows too (a failed bundle update can still have a live embed)', () => {
    const bg: BackgroundActivity = { fail1: 'building code graph (42 files)…' };
    expect(backgroundActivityLines(bg, rows)).toEqual(['Gamma — building code graph (42 files)…']);
  });
});

describe('terminal-row sub-event: row untouched, background line updated', () => {
  it('post-finished kg-sync event feeds the footer without dirtying the row', () => {
    const rows: ProgressRow[] = [
      row({ project_id: 'p1', project_name: 'Alpha', status: 'succeeded' }),
    ];
    const state = { rows, current: null };
    const label = 'syncing docs 35/120';

    // The ROW rule: applySubProgress must NOT resurrect a terminal row.
    const afterRow = applySubProgress(state, 'p1', label);
    expect(afterRow).toBe(state);

    // The FOOTER rule: the same event updates the background map and renders.
    const bg = applyBackgroundActivity(emptyBackgroundActivity(), 'p1', label);
    expect(backgroundActivityLines(bg, rows)).toEqual([`Alpha — ${label}`]);
  });
});
