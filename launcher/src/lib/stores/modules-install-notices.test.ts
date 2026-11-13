// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.106: `module://install-complete` carries `notices` (e.g. where an
// earlier registry install's data is kept, `registry_module_retirement.rs`);
// the store shows each as an info toast.

import { describe, expect, it, vi, beforeEach } from 'vitest';

const { handlers, infos } = vi.hoisted(() => ({
  handlers: new Map<string, (e: { payload: unknown }) => void>(),
  infos: [] as string[],
}));
vi.mock('$lib/tauri', () => ({
  tauriAvailable: () => true,
  listen: vi.fn(async (event: string, h: (e: { payload: unknown }) => void) => {
    handlers.set(event, h);
    return () => {};
  }),
  invoke: vi.fn(async () => null),
}));
vi.mock('$lib/stores/toast', () => ({
  toast: {
    info: (m: string) => infos.push(m),
    success: () => {},
    error: () => {},
    warning: () => {},
  },
}));

import { installNotices } from './modules';

beforeEach(() => {
  infos.length = 0;
});

describe('install-complete notices', () => {
  it('each notice becomes an info toast', () => {
    const h = handlers.get('module://install-complete');
    expect(h).toBeDefined();
    h!({ payload: { module_id: 'm', success: true, notices: ['kept at /x', 'second'] } });
    expect(infos).toEqual(['kept at /x', 'second']);
  });

  it('no notices, or a malformed field, shows nothing', () => {
    const h = handlers.get('module://install-complete')!;
    h({ payload: { module_id: 'm', success: true } });
    h({ payload: { module_id: 'm', success: true, notices: 'not a list' } });
    h({ payload: null });
    expect(infos).toEqual([]);
  });

  it('installNotices keeps non-empty strings only', () => {
    expect(installNotices({ notices: ['a', '', 3, null, 'b'] })).toEqual(['a', 'b']);
    expect(installNotices(undefined)).toEqual([]);
  });
});
