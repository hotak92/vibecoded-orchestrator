// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.100: the `vct-module-updates-available` listener. Nothing listened to
// the 24 h poll's event before, so what it discovered never reached the
// screen. The store now (1) keeps the payload for the Sidebar count badge and
// (2) re-reads the catalog so the tiles' "update available" state matches.

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// `vi.hoisted`: the mock factory runs while `./modules` is being imported,
// before plain top-level consts are initialised.
const { handlers, invokeCalls } = vi.hoisted(() => ({
  handlers: new Map<string, (e: { payload: unknown }) => void>(),
  invokeCalls: [] as string[],
}));
vi.mock('$lib/tauri', () => ({
  tauriAvailable: () => true,
  listen: vi.fn(async (event: string, h: (e: { payload: unknown }) => void) => {
    handlers.set(event, h);
    return () => {};
  }),
  invoke: vi.fn(async (cmd: string) => {
    invokeCalls.push(cmd);
    if (cmd === 'list_module_catalog') {
      return { modules: [], l0_status: null, parse_errors: [], dev_affordance_hint: null };
    }
    if (cmd === 'update_module_for_project') return { module_id: 'mod-a', project_id: 'p1' };
    return null;
  }),
}));

import { get } from 'svelte/store';
import { modules, moduleUpdateCount, countModulesWithUpdates, withoutUpdated } from './modules';
import { EVENT_UPDATES_AVAILABLE, type ModuleUpdateAvailable } from '$lib/api/module_updates';

const u = (module_id: string, project_id = 'p1'): ModuleUpdateAvailable => ({
  module_id,
  project_id,
  current_version: '0.1.0',
  available_version: '0.2.0',
});

beforeEach(() => {
  invokeCalls.length = 0;
});

describe('vct-module-updates-available', () => {
  it('the store subscribes to exactly the event the Rust poll emits', () => {
    expect(EVENT_UPDATES_AVAILABLE).toBe('vct-module-updates-available');
    expect(handlers.has(EVENT_UPDATES_AVAILABLE)).toBe(true);
  });

  it('refreshes the catalog and stores the list on the event', async () => {
    handlers.get(EVENT_UPDATES_AVAILABLE)!({ payload: [u('mod-a'), u('mod-b', 'p2')] });
    expect(get(moduleUpdateCount)).toBe(2);
    await vi.waitFor(() => expect(invokeCalls).toContain('list_module_catalog'));
  });

  it('counts one per module, not per project', () => {
    handlers.get(EVENT_UPDATES_AVAILABLE)!({ payload: [u('mod-a', 'p1'), u('mod-a', 'p2'), u('mod-b', '')] });
    expect(get(moduleUpdateCount)).toBe(2);
  });

  it('a later event replaces the list wholesale (the payload is the full summary)', () => {
    handlers.get(EVENT_UPDATES_AVAILABLE)!({ payload: [u('mod-a')] });
    handlers.get(EVENT_UPDATES_AVAILABLE)!({ payload: [] });
    expect(get(moduleUpdateCount)).toBe(0);
  });

  it('a malformed payload clears to zero rather than throwing', () => {
    handlers.get(EVENT_UPDATES_AVAILABLE)!({ payload: null });
    expect(get(moduleUpdateCount)).toBe(0);
  });

  it('updating a module drops it from the count', async () => {
    handlers.get(EVENT_UPDATES_AVAILABLE)!({ payload: [u('mod-a', 'p1'), u('mod-b', 'p1')] });
    await modules.update('p1', 'mod-a');
    expect(get(moduleUpdateCount)).toBe(1);
    expect(get(modules).updatesAvailable.map((x) => x.module_id)).toEqual(['mod-b']);
  });
});

describe('pure helpers', () => {
  it('withoutUpdated matches a global row (project_id "") for any project, leaves others', () => {
    const list = [u('mod-a', ''), u('mod-a', 'p2'), u('mod-b', 'p1')];
    expect(withoutUpdated(list, 'p1', 'mod-a').map((x) => `${x.module_id}:${x.project_id}`)).toEqual([
      'mod-a:p2',
      'mod-b:p1',
    ]);
    expect(countModulesWithUpdates([])).toBe(0);
  });
});

describe('the Sidebar renders the badge from the store', () => {
  const src = readFileSync(
    fileURLToPath(new URL('../components/Sidebar.svelte', import.meta.url)),
    'utf8',
  );
  it('the Modules entry is the one carrying the moduleUpdates badge', () => {
    expect(src).toMatch(/href: '\/modules',[\s\S]{0,260}badge: 'moduleUpdates'/);
    expect(src).toContain("item.badge === 'moduleUpdates' && $moduleUpdateCount > 0");
  });
});
