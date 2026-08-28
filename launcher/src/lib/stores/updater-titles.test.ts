// SPDX-License-Identifier: AGPL-3.0-or-later
//
// P2-M7 (v0.2.91 wave 5): unit tests for the two pure helpers extracted
// into `stores/updater.ts` so the "resume/Finish-Update" title decision
// lives in exactly one place instead of being mirrored between
// `UpdateBadge.svelte`'s popover copy and
// `OrchestratorUpdateProgressModal.svelte`'s overlay title.
//
// Contract under test:
//   - isAutostashPopResume(): 'autostash-pop' -> true; anything else
//     (undefined, null, other resume_operation strings) -> false.
//   - titleForUpdateKind(): correct title per UpdateKind, with
//     'merge_resolved_incomplete' branching on resumeOperation via
//     isAutostashPopResume — this is the regression guard for the actual
//     bug (the modal used to have no arm for this kind at all and fell
//     through to the generic "Updating orchestrator").

import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

// localStorage polyfill — updater.ts pulls in install-state-store which
// reads localStorage synchronously at store construction (loadSeen()).
beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const s = new Map<string, string>();
    (globalThis as { localStorage?: Storage }).localStorage = {
      get length() { return s.size; },
      clear() { s.clear(); },
      getItem(k: string) { return s.has(k) ? (s.get(k) as string) : null; },
      key(i: number) { return Array.from(s.keys())[i] ?? null; },
      removeItem(k: string) { s.delete(k); },
      setItem(k: string, v: string) { s.set(k, String(v)); },
    } as Storage;
  }
});

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
  tauriAvailable: () => false,
}));

vi.mock('./orchestrator', () => ({
  orchestrator: { subscribe: () => () => {} },
  cancelScheduledRetry: vi.fn(),
}));

afterEach(() => {
  vi.clearAllMocks();
});

describe('isAutostashPopResume', () => {
  it('is true only for the exact autostash-pop sentinel', async () => {
    const { isAutostashPopResume } = await import('./updater');
    expect(isAutostashPopResume('autostash-pop')).toBe(true);
  });

  it('is false for undefined, null, and other resume_operation values', async () => {
    const { isAutostashPopResume } = await import('./updater');
    expect(isAutostashPopResume(undefined)).toBe(false);
    expect(isAutostashPopResume(null)).toBe(false);
    expect(isAutostashPopResume('update')).toBe(false);
    expect(isAutostashPopResume('')).toBe(false);
  });
});

describe('titleForUpdateKind', () => {
  it('titles the non-merge kinds without needing resumeOperation', async () => {
    const { titleForUpdateKind } = await import('./updater');
    expect(titleForUpdateKind('binary_stale')).toBe('Restarting launcher');
    expect(titleForUpdateKind('install_stale')).toBe('Installing update');
    expect(titleForUpdateKind('remote_ahead')).toBe('Updating orchestrator');
    expect(titleForUpdateKind(null)).toBe('Updating orchestrator');
  });

  it('titles merge_resolved_incomplete as "Finishing update" for the autostash-pop story (P2-M7 regression guard)', async () => {
    const { titleForUpdateKind } = await import('./updater');
    expect(titleForUpdateKind('merge_resolved_incomplete', 'autostash-pop')).toBe(
      'Finishing update',
    );
  });

  it('titles merge_resolved_incomplete as "Resuming update" for a plain halted-merge resume', async () => {
    const { titleForUpdateKind } = await import('./updater');
    expect(titleForUpdateKind('merge_resolved_incomplete', 'update')).toBe('Resuming update');
    expect(titleForUpdateKind('merge_resolved_incomplete', undefined)).toBe('Resuming update');
  });

  it('never falls through to the generic title for merge_resolved_incomplete (the actual P2-M7 bug)', async () => {
    const { titleForUpdateKind } = await import('./updater');
    const genericFallback = titleForUpdateKind('remote_ahead');
    const mergeTitle = titleForUpdateKind('merge_resolved_incomplete', undefined);
    expect(mergeTitle).not.toBe(genericFallback);
  });
});
