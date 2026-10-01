// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it, vi } from 'vitest';

const calls: Array<[string, unknown]> = [];
let stored: boolean | null = null;
vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(async (cmd: string, args?: { enabled?: boolean }) => {
    calls.push([cmd, args]);
    if (cmd === 'set_module_update_auto_check_enabled') {
      stored = args?.enabled ?? null;
      return undefined;
    }
    if (cmd === 'get_module_update_auto_check_enabled') return stored;
    return null;
  }),
  safeInvoke: vi.fn(),
  listen: vi.fn(async () => () => {}),
}));
vi.mock('@tauri-apps/api/app', () => ({ getVersion: vi.fn(async () => '0') }));

import {
  DEFAULT_MODULE_UPDATE_AUTO_CHECK,
  moduleUpdateAutoCheckHint,
  resolveModuleUpdateAutoCheck,
} from './module-update-autocheck';
import { PREF_LOADERS, PREF_EAGER_IPC_BUDGET, PREF_LOADER_KEYS } from './preferences/loaders';
import { setModuleUpdateAutoCheckEnabled } from './api/module_updates';

describe('module auto-check toggle', () => {
  it('renders the shipped default (ON) when the answer is missing or unreadable', () => {
    expect(DEFAULT_MODULE_UPDATE_AUTO_CHECK).toBe(true);
    expect(resolveModuleUpdateAutoCheck(null)).toBe(true);
    expect(resolveModuleUpdateAutoCheck(undefined)).toBe(true);
  });

  it('renders the stored value, false included', () => {
    expect(resolveModuleUpdateAutoCheck(false)).toBe(false);
    expect(resolveModuleUpdateAutoCheck(true)).toBe(true);
  });

  it('the hint says when an OFF takes effect', () => {
    expect(moduleUpdateAutoCheckHint(false)).toMatch(/next daily check/);
    expect(moduleUpdateAutoCheckHint(true)).toMatch(/Once a day/);
  });

  it('is ONE lazy registry entry, so it never adds to the mount cost', () => {
    const spec = PREF_LOADERS.moduleUpdateAutoCheck as { eager: boolean; slow?: boolean };
    expect(spec.eager).toBe(false);
    expect(PREF_LOADER_KEYS.filter((k) => PREF_LOADERS[k].eager).length).toBeLessThanOrEqual(
      PREF_EAGER_IPC_BUDGET,
    );
  });

  it('persists: a write survives into the next read through the registry loader', async () => {
    calls.length = 0;
    stored = null;
    await setModuleUpdateAutoCheckEnabled(false);
    const readBack = await PREF_LOADERS.moduleUpdateAutoCheck.load();
    expect(calls.map((c) => c[0])).toEqual([
      'set_module_update_auto_check_enabled',
      'get_module_update_auto_check_enabled',
    ]);
    expect(resolveModuleUpdateAutoCheck(readBack)).toBe(false);
    await setModuleUpdateAutoCheckEnabled(true);
    expect(resolveModuleUpdateAutoCheck(await PREF_LOADERS.moduleUpdateAutoCheck.load())).toBe(true);
  });
});
