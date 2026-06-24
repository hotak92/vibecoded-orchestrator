// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.67: unit tests for the modules store's install-progress reducer.
//
// Why a pure-reducer test (not a store-mount test)
// ────────────────────────────────────────────────
// The modules store wires its `module://install-progress` listener only
// inside a Tauri host (`tauriAvailable()` is false in the node test env),
// so the listener body never runs under vitest. The merge logic is
// extracted to the pure `mergeInstallProgress` helper precisely so the
// reducer is testable without mocking Tauri — same convention as
// `modules-per-project.test.ts` (pure helper, no Svelte runtime).
//
// These tests pin the regression that motivated the v0.2.67 UX fix: the
// store used to ignore every stage except `variant_fallback`, so a tile
// showed a static "Installing…" spinner across the whole pull and never
// surfaced the fast 401 → `failed` transition.

import { describe, expect, it } from 'vitest';
import { mergeInstallProgress, type ModuleInstallProgress } from './modules';

function ev(over: Partial<{
  project_id: string;
  module_id: string;
  stage: string;
  step_index: number;
  step_total: number;
  percent: number;
  message: string;
}> = {}) {
  return {
    project_id: 'proj-1',
    module_id: 'vct-mod',
    stage: 'pulling',
    step_index: 1,
    step_total: 2,
    percent: 40,
    message: 'Pulling image',
    ...over,
  };
}

describe('mergeInstallProgress', () => {
  it('records a non-variant_fallback stage (the old listener dropped these)', () => {
    const next = mergeInstallProgress({}, ev({ stage: 'clone', message: 'Fetching source', percent: 0 }));
    expect(next['vct-mod']).toEqual<ModuleInstallProgress>({
      stage: 'clone',
      percent: 0,
      message: 'Fetching source',
      failed: false,
    });
  });

  it('marks failed=true on a terminal failed stage (the fast-401 case)', () => {
    const next = mergeInstallProgress({}, ev({ stage: 'failed', message: 'unauthorized', percent: 0 }));
    expect(next['vct-mod'].failed).toBe(true);
  });

  it('latest event wins per module; other modules are untouched', () => {
    const start = mergeInstallProgress({}, ev({ stage: 'clone', percent: 0 }));
    const withOther = mergeInstallProgress(start, ev({ module_id: 'other-mod', stage: 'pulling', percent: 50 }));
    const final = mergeInstallProgress(withOther, ev({ stage: 'pulling', percent: 90 }));

    expect(final['vct-mod']).toEqual<ModuleInstallProgress>({
      stage: 'pulling',
      percent: 90,
      message: 'Pulling image',
      failed: false,
    });
    // The other module's entry survives the update.
    expect(final['other-mod'].percent).toBe(50);
  });

  it('does not mutate the input map (returns a fresh object)', () => {
    const before: Record<string, ModuleInstallProgress> = {};
    const after = mergeInstallProgress(before, ev());
    expect(before).toEqual({});
    expect(after).not.toBe(before);
  });
});
