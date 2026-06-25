// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Defect B (v0.2.68): unit tests for the project-setup store's pure
// `mergeSetupProgress` reducer.
//
// Why a pure-reducer test (not a store-mount test)
// ────────────────────────────────────────────────
// The store wires its `project://setup-progress` listener only inside a
// Tauri host (`tauriAvailable()` is false under vitest), so the listener
// body never runs here. The merge is extracted to the pure
// `mergeSetupProgress` helper precisely so it's testable without mocking
// Tauri — same convention as `modules-install-progress.test.ts`.
//
// The reducer's job: fold an incoming setup-progress event into the active
// view-model, preserving `observed_at` across phase transitions for the
// SAME project (so the banner's elapsed timer doesn't reset on each phase)
// and resetting it when a DIFFERENT project's setup takes over the banner.

import { describe, expect, it } from 'vitest';
import { mergeSetupProgress, type ActiveSetup } from './project-setup';
import type { SetupProgressEvent } from '$lib/types/launcher';

function ev(over: Partial<SetupProgressEvent> = {}): SetupProgressEvent {
  return {
    project_id: 'proj-1',
    project_name: 'Alpha',
    status: 'running',
    phase: 'bootstrap',
    warnings: [],
    error: null,
    ...over,
  };
}

describe('mergeSetupProgress', () => {
  it('builds an active view-model from a running event', () => {
    const next = mergeSetupProgress(null, ev({ phase: 'bundle' }), 1_000);
    expect(next).toEqual<ActiveSetup>({
      project_id: 'proj-1',
      project_name: 'Alpha',
      status: 'running',
      phase: 'bundle',
      warnings: [],
      error: null,
      observed_at: 1_000,
    });
  });

  it('preserves observed_at across phase transitions for the same project', () => {
    const first = mergeSetupProgress(null, ev({ phase: 'bootstrap' }), 1_000);
    const second = mergeSetupProgress(first, ev({ phase: 'bundle' }), 5_000);
    const third = mergeSetupProgress(second, ev({ phase: 'post_bundle' }), 9_000);
    // observed_at stays at the FIRST sighting so the elapsed timer is
    // monotonic for the whole setup.
    expect(third.observed_at).toBe(1_000);
    expect(third.phase).toBe('post_bundle');
  });

  it('resets observed_at when a different project takes over the banner', () => {
    const first = mergeSetupProgress(null, ev({ project_id: 'proj-1' }), 1_000);
    const second = mergeSetupProgress(
      first,
      ev({ project_id: 'proj-2', project_name: 'Beta' }),
      8_000,
    );
    expect(second.project_id).toBe('proj-2');
    expect(second.observed_at).toBe(8_000);
  });

  it('carries classified warnings + error on a terminal failed event', () => {
    const next = mergeSetupProgress(
      null,
      ev({
        status: 'failed',
        phase: null,
        error: 'bundle install crashed',
        warnings: [
          { message: 'bootstrap-collections error on X', severity: 'error' },
        ],
      }),
      2_000,
    );
    expect(next.status).toBe('failed');
    expect(next.error).toBe('bundle install crashed');
    expect(next.warnings).toHaveLength(1);
    expect(next.warnings[0].severity).toBe('error');
  });

  it('carries amber/info warnings on a terminal deferred event', () => {
    const next = mergeSetupProgress(
      null,
      ev({
        status: 'deferred',
        phase: null,
        warnings: [
          { message: 'Weaviate collection bootstrap deferred', severity: 'info' },
        ],
      }),
      3_000,
    );
    expect(next.status).toBe('deferred');
    expect(next.warnings[0].severity).toBe('info');
  });

  it('does not mutate the previous view-model', () => {
    const before = mergeSetupProgress(null, ev(), 1_000);
    const snapshot = { ...before };
    mergeSetupProgress(before, ev({ phase: 'post_bundle' }), 2_000);
    expect(before).toEqual(snapshot);
  });
});
