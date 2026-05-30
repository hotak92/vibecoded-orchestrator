// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.40 H2 — unit tests for `summarizeRlFlags`.
//
// Pure-function tests for the RL flag → human-readable status mapping
// used by `RlRerankerStatusPanel.svelte`. Covers the 8 combinations of
// the three booleans + verifies the stable `*Key` discriminants the
// component uses for CSS hooks and `data-testid` selectors.

import { describe, expect, it } from 'vitest';
import { summarizeRlFlags } from './rl-settings-summary';

describe('summarizeRlFlags', () => {
  it('all flags false ⇒ local-active + not contributing', () => {
    const r = summarizeRlFlags(false, false, false);
    expect(r.trainingModeKey).toBe('local-active');
    expect(r.globalSourceKey).toBe('not-contributing');
    expect(r.trainingMode).toContain('Online training active');
    expect(r.globalSource).toContain('Not contributing');
  });

  it('use_global=true alone ⇒ read-only-global', () => {
    const r = summarizeRlFlags(true, false, false);
    expect(r.trainingModeKey).toBe('read-only-global');
    expect(r.trainingMode).toContain('Read-only');
  });

  it('online_disabled=true alone ⇒ frozen', () => {
    const r = summarizeRlFlags(false, true, false);
    expect(r.trainingModeKey).toBe('frozen');
    expect(r.trainingMode).toContain('Frozen');
    expect(r.trainingMode).toContain('events logged');
  });

  it('online_disabled wins over use_global when both true ⇒ frozen-and-global', () => {
    const r = summarizeRlFlags(true, true, false);
    expect(r.trainingModeKey).toBe('frozen-and-global');
    expect(r.trainingMode).toContain('Frozen');
    expect(r.trainingMode).toContain('read-only global');
  });

  it('global_source_flag=true ⇒ contributing (independent of training mode)', () => {
    const r1 = summarizeRlFlags(false, false, true);
    const r2 = summarizeRlFlags(true, true, true);
    expect(r1.globalSourceKey).toBe('contributing');
    expect(r2.globalSourceKey).toBe('contributing');
    expect(r1.globalSource).toContain('Contributing');
    expect(r2.globalSource).toContain('Contributing');
  });

  it('global_source_flag=false ⇒ not-contributing (every combination)', () => {
    for (const useGlobal of [false, true]) {
      for (const onlineDisabled of [false, true]) {
        const r = summarizeRlFlags(useGlobal, onlineDisabled, false);
        expect(r.globalSourceKey).toBe('not-contributing');
      }
    }
  });

  it('returned strings are non-empty for every combination', () => {
    for (const useGlobal of [false, true]) {
      for (const onlineDisabled of [false, true]) {
        for (const globalSource of [false, true]) {
          const r = summarizeRlFlags(useGlobal, onlineDisabled, globalSource);
          expect(r.trainingMode.length).toBeGreaterThan(0);
          expect(r.globalSource.length).toBeGreaterThan(0);
        }
      }
    }
  });
});
