// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-08, L3-F04 / L2-F05 + the L3 dead-event census): the app
// shell's routing of backend events (`stores/ui.ts`).
//
// Contract under test:
//   - `vct-tray-action {kind:'check_updates'}` navigates to the Updates page
//     AND runs the one check; `open_project` navigates to `/project/<id>`
//     (id URL-encoded, carried as data); `about` opens the about/changelog UI;
//     anything else does nothing;
//   - `registerShellListeners` registers exactly ONE `vct-tray-action`
//     listener plus one listener per formerly-dead notice event, and a
//     delivered event reaches the routing (driven through a fake `listen`,
//     not a source scan);
//   - each notice event tells the user something specific (error toasts are
//     keyed so the bell inbox dedups them);
//   - the teardown unlistens everything.

import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

vi.mock('$lib/onboarding', () => ({ clearOnboardingComplete: async () => {} }));

type UiModule = typeof import('./ui');
let M: UiModule;

type Spy = {
  goto: Mock<(path: string) => Promise<void>>;
  manualCheck: Mock<() => Promise<void>>;
  showAbout: Mock<() => void>;
  refreshUpdateStatus: Mock<() => Promise<void>>;
  notifyInfo: Mock<(message: string) => void>;
  notifyError: Mock<(message: string, key: string) => void>;
};
let actions: Spy;
let order: string[];

beforeEach(async () => {
  vi.resetModules();
  M = await import('./ui');
  order = [];
  actions = {
    goto: vi.fn(async (p: string) => void order.push(`goto:${p}`)),
    manualCheck: vi.fn(async () => void order.push('check')),
    showAbout: vi.fn(() => void order.push('about')),
    refreshUpdateStatus: vi.fn(async () => void order.push('refresh')),
    notifyInfo: vi.fn((_m: string) => {}),
    notifyError: vi.fn((_m: string, _k: string) => {}),
  };
});

describe('routeTrayAction', () => {
  it('check_updates → the Updates page, THEN the one check', async () => {
    await M.routeTrayAction({ kind: 'check_updates', project_id: null }, actions);
    expect(order).toEqual(['goto:/preferences/updates', 'check']);
  });

  it('open_project → /project/<id>, the id treated as data', async () => {
    await M.routeTrayAction({ kind: 'open_project', project_id: "ab'c/../x" }, actions);
    expect(order).toEqual([`goto:/project/${encodeURIComponent("ab'c/../x")}`]);
  });

  it('about → the about/changelog UI', async () => {
    await M.routeTrayAction({ kind: 'about', project_id: null }, actions);
    expect(order).toEqual(['about']);
  });

  it.each([null, undefined, 'check_updates', {}, { kind: 'nope' }, { kind: 'open_project' }, { kind: 'open_project', project_id: '' }])(
    'ignores %o',
    async (payload) => {
      await M.routeTrayAction(payload, actions);
      expect(order).toEqual([]);
    },
  );
});

describe('registerShellListeners', () => {
  type Handler = (e: { payload: unknown }) => void;
  let handlers: Map<string, Handler[]>;
  let unlistened: string[];
  const fakeListen = async <T>(event: string, h: (e: { payload: T }) => void) => {
    const list = handlers.get(event) ?? [];
    list.push(h as Handler);
    handlers.set(event, list);
    return () => void unlistened.push(event);
  };
  const emit = (event: string, payload: unknown) => {
    for (const h of handlers.get(event) ?? []) h({ payload });
  };

  beforeEach(() => {
    handlers = new Map();
    unlistened = [];
  });

  it('registers ONE tray listener and one per notice event', async () => {
    await M.registerShellListeners(fakeListen as never, actions);
    expect(handlers.get(M.TRAY_ACTION_EVENT)).toHaveLength(1);
    expect(M.TRAY_ACTION_EVENT).toBe('vct-tray-action');
    for (const ev of [
      'vct-hub-stopped',
      'services_watcher_alert',
      'vct-codegraph-extras-progress',
      'vct-openai-key-re-register-failed',
      'module://container-start-failed',
      'module://db-migration-failed',
      'vct-launcher-update-available',
    ]) {
      expect(handlers.get(ev), ev).toHaveLength(1);
    }
  });

  it('a delivered tray action reaches navigation + check', async () => {
    await M.registerShellListeners(fakeListen as never, actions);
    emit(M.TRAY_ACTION_EVENT, { kind: 'check_updates', project_id: null });
    await vi.waitFor(() => expect(order).toEqual(['goto:/preferences/updates', 'check']));
  });

  it('each notice event tells the user something specific', async () => {
    await M.registerShellListeners(fakeListen as never, actions);
    emit('services_watcher_alert', { service: 'weaviate', kind: 'max_attempts_reached', attempts: 3 });
    emit('services_watcher_alert', { service: 'ollama', kind: 'stuck_transient_state', error: 'timeout' });
    emit('vct-openai-key-re-register-failed', { reason: 'invalid_api_key', http_status: 401 });
    emit('module://container-start-failed', { module_id: 'rl-reranker', error: 'port busy' });
    emit('module://db-migration-failed', { module_id: 'rl-reranker', errors: ['m1 failed', 'm2 failed'] });
    emit('module://db-migration-failed', { module_id: 'mao', error: 'db locked' });
    const errs = actions.notifyError.mock.calls;
    expect(errs[0][0]).toMatch(/weaviate stopped .* after 3 attempts/);
    expect(errs[0][1]).toBe('services-watcher:weaviate');
    expect(errs[1][0]).toMatch(/ollama was stuck .*timeout/);
    expect(errs[2][0]).toMatch(/HTTP 401.*invalid_api_key/);
    expect(errs[3]).toEqual([
      'Module rl-reranker: its container failed to start — port busy',
      'module:rl-reranker:container-start',
    ]);
    expect(errs[4][0]).toBe('Module rl-reranker: database migration failed — m1 failed; m2 failed');
    expect(errs[5][0]).toBe('Module mao: database migration failed — db locked');
  });

  it('extras progress notifies only when a run finishes; hub stop informs; update-available refreshes', async () => {
    await M.registerShellListeners(fakeListen as never, actions);
    emit('vct-codegraph-extras-progress', { label: 'public-clone', progress: 0.4, message: 'x' });
    expect(actions.notifyInfo).not.toHaveBeenCalled();
    emit('vct-codegraph-extras-progress', { label: 'public-clone', progress: 1, message: 'done' });
    expect(actions.notifyInfo).toHaveBeenCalledWith('Code graph: extra path "public-clone" indexed.');
    emit('vct-hub-stopped', {});
    expect(actions.notifyInfo).toHaveBeenLastCalledWith(expect.stringMatching(/^vct-hub stopped/));
    emit('vct-launcher-update-available', { available: true });
    expect(actions.refreshUpdateStatus).toHaveBeenCalledTimes(1);
  });

  it('a listener that fails to register does not stop the others', async () => {
    const flaky = async <T>(event: string, h: (e: { payload: T }) => void) => {
      if (event === 'services_watcher_alert') throw new Error('no ipc');
      return fakeListen(event, h);
    };
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    await M.registerShellListeners(flaky as never, actions);
    expect(handlers.get(M.TRAY_ACTION_EVENT)).toHaveLength(1);
    expect(handlers.get('vct-hub-stopped')).toHaveLength(1);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });

  it('the teardown unlistens every registered event', async () => {
    const stop = await M.registerShellListeners(fakeListen as never, actions);
    stop();
    expect(new Set(unlistened)).toEqual(new Set(handlers.keys()));
  });
});
