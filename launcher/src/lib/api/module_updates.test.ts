// SPDX-License-Identifier: AGPL-3.0-or-later
//
// V52-F (v0.2.52): tests for the api/module_updates.ts wire shape.
//
// These tests verify that the four exported wrappers serialize their
// arguments correctly into `invoke(name, args)` calls. The actual Tauri
// implementation is covered by the Rust unit tests in
// `commands/module_updates.rs::tests::*`.

import { describe, expect, it, vi, beforeEach } from 'vitest';

// Mock $lib/tauri BEFORE importing the API wrapper so the import-time
// binding to `invoke` is the mocked version.
const invokeMock = vi.fn();
vi.mock('$lib/tauri', () => ({
  invoke: (name: string, args?: Record<string, unknown>) => invokeMock(name, args),
}));

import {
  checkModuleUpdatesAvailable,
  EVENT_UPDATES_AVAILABLE,
  getModuleUpdateAutoCheckEnabled,
  setModuleUpdateAutoCheckEnabled,
  updateModuleToLatest,
  type ModuleUpdateAvailable,
  type UpdateModuleOutcome,
} from './module_updates';

beforeEach(() => {
  invokeMock.mockReset();
});

describe('checkModuleUpdatesAvailable', () => {
  it('invokes check_module_updates_available with snake_case projectId', async () => {
    const rows: ModuleUpdateAvailable[] = [
      {
        project_id: 'p-1',
        module_id: 'vct-rl-reranker',
        current_version: '0.2.7',
        available_version: '0.2.8',
      },
    ];
    invokeMock.mockResolvedValueOnce(rows);

    const got = await checkModuleUpdatesAvailable('p-1');

    expect(invokeMock).toHaveBeenCalledWith('check_module_updates_available', {
      projectId: 'p-1',
    });
    expect(got).toEqual(rows);
  });

  it('returns the empty array shape unchanged', async () => {
    invokeMock.mockResolvedValueOnce([]);
    const got = await checkModuleUpdatesAvailable('p-empty');
    expect(got).toEqual([]);
  });
});

describe('updateModuleToLatest', () => {
  it('handles already_latest outcome', async () => {
    const outcome: UpdateModuleOutcome = {
      kind: 'already_latest',
      version: '0.2.8',
    };
    invokeMock.mockResolvedValueOnce(outcome);

    const got = await updateModuleToLatest('p-1', 'vct-rl-reranker');

    expect(invokeMock).toHaveBeenCalledWith('update_module_to_latest', {
      projectId: 'p-1',
      moduleId: 'vct-rl-reranker',
    });
    expect(got).toEqual(outcome);
  });

  it('handles updated outcome with previous + new versions', async () => {
    const outcome: UpdateModuleOutcome = {
      kind: 'updated',
      previous_version: '0.2.7',
      new_version: '0.2.8',
    };
    invokeMock.mockResolvedValueOnce(outcome);

    const got = await updateModuleToLatest('p-1', 'vct-rl-reranker');
    expect(got).toEqual(outcome);
  });

  it('propagates errors from the underlying invoke', async () => {
    invokeMock.mockRejectedValueOnce(new Error('container restart timed out'));

    await expect(updateModuleToLatest('p-1', 'vct-rl-reranker')).rejects.toThrow(
      'container restart timed out',
    );
  });
});

describe('auto-check toggle wrappers', () => {
  it('getModuleUpdateAutoCheckEnabled has no args', async () => {
    invokeMock.mockResolvedValueOnce(true);
    const got = await getModuleUpdateAutoCheckEnabled();
    expect(invokeMock).toHaveBeenCalledWith(
      'get_module_update_auto_check_enabled',
      undefined,
    );
    expect(got).toBe(true);
  });

  it('setModuleUpdateAutoCheckEnabled passes the enabled flag', async () => {
    invokeMock.mockResolvedValueOnce(undefined);
    await setModuleUpdateAutoCheckEnabled(false);
    expect(invokeMock).toHaveBeenCalledWith('set_module_update_auto_check_enabled', {
      enabled: false,
    });
  });
});

describe('EVENT_UPDATES_AVAILABLE', () => {
  it('matches the Rust constant exactly', () => {
    // Rust: pub const EVENT_UPDATES_AVAILABLE: &str = "vct-module-updates-available";
    expect(EVENT_UPDATES_AVAILABLE).toBe('vct-module-updates-available');
  });
});
