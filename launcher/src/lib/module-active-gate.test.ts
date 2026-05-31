// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.42 W6 (UX-1): unit tests for `moduleIsActive` and `moduleIsInstalled`.
//
// These tests verify the core UX gating contract:
//   - installed + running   → active (show RL controls)
//   - installed + stopped   → not active (hide RL controls)
//   - not installed         → not active
//   - paid module, no license → not active (status won't be 'running')
//
// Pure-function tests: no Tauri mock, no DOM, fast (<100 ms).

import { describe, expect, it } from 'vitest';
import {
  moduleIsActive,
  moduleIsInstalled,
  RL_RERANKER_MODULE_ID,
} from './module-active-gate';
import type { ModuleInstallRow } from '$lib/types/launcher';

/** Minimal install row factory. */
function makeRow(
  moduleId: string,
  status: ModuleInstallRow['status'],
): ModuleInstallRow {
  return {
    id: 'row-1',
    project_id: 'proj-1',
    module_id: moduleId,
    module_version: '0.2.0',
    install_path: '/tmp/install',
    status,
    enabled: true,
    installed_at: 0,
    last_started_at: null,
    last_error: null,
  };
}

const RL_ID = RL_RERANKER_MODULE_ID;

describe('moduleIsActive', () => {
  it('installed + running → active (show RL controls)', () => {
    const rows = [makeRow(RL_ID, 'running')];
    expect(moduleIsActive(RL_ID, rows)).toBe(true);
  });

  it('installed + stopped → NOT active (hide RL controls)', () => {
    const rows = [makeRow(RL_ID, 'stopped')];
    expect(moduleIsActive(RL_ID, rows)).toBe(false);
  });

  it('installed (status=installed) + not yet started → NOT active', () => {
    // 'installed' means installed but never started; container is not running.
    const rows = [makeRow(RL_ID, 'installed')];
    expect(moduleIsActive(RL_ID, rows)).toBe(false);
  });

  it('not installed (empty list) → NOT active', () => {
    expect(moduleIsActive(RL_ID, [])).toBe(false);
  });

  it('different module installed running → does NOT activate RL gate', () => {
    const rows = [makeRow('vct-other-module', 'running')];
    expect(moduleIsActive(RL_ID, rows)).toBe(false);
  });

  it('module in error state → NOT active', () => {
    const rows = [makeRow(RL_ID, 'error')];
    expect(moduleIsActive(RL_ID, rows)).toBe(false);
  });

  it('module in broken state → NOT active', () => {
    const rows = [makeRow(RL_ID, 'broken')];
    expect(moduleIsActive(RL_ID, rows)).toBe(false);
  });

  it('multiple modules: only matching running row counts', () => {
    const rows = [
      makeRow('vct-other-module', 'running'),
      makeRow(RL_ID, 'stopped'),
    ];
    expect(moduleIsActive(RL_ID, rows)).toBe(false);
  });

  it('multiple modules: matching running row returns true', () => {
    const rows = [
      makeRow('vct-other-module', 'stopped'),
      makeRow(RL_ID, 'running'),
    ];
    expect(moduleIsActive(RL_ID, rows)).toBe(true);
  });
});

describe('moduleIsInstalled', () => {
  it('status=installed → installed', () => {
    expect(moduleIsInstalled(RL_ID, [makeRow(RL_ID, 'installed')])).toBe(true);
  });

  it('status=running → installed', () => {
    expect(moduleIsInstalled(RL_ID, [makeRow(RL_ID, 'running')])).toBe(true);
  });

  it('status=stopped → installed', () => {
    expect(moduleIsInstalled(RL_ID, [makeRow(RL_ID, 'stopped')])).toBe(true);
  });

  it('status=error → NOT installed (install failed)', () => {
    expect(moduleIsInstalled(RL_ID, [makeRow(RL_ID, 'error')])).toBe(false);
  });

  it('status=broken → NOT installed', () => {
    expect(moduleIsInstalled(RL_ID, [makeRow(RL_ID, 'broken')])).toBe(false);
  });

  it('status=installing → NOT installed (in-progress)', () => {
    expect(moduleIsInstalled(RL_ID, [makeRow(RL_ID, 'installing')])).toBe(false);
  });

  it('no row → NOT installed', () => {
    expect(moduleIsInstalled(RL_ID, [])).toBe(false);
  });
});
