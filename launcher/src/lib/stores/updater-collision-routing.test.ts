// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.88 (DEFECT 1 + 2 + 3): tests for the updater store's `runUpdate()`
// error routing. The inline update pull can now fail with THREE structured,
// actionable events; `runUpdate()` must route each to its own store field
// (untrackedCollision / autostashPop) instead of a dead-end raw error, and must
// re-check status after any resume-style honest error so the badge re-shows.
//
// Contract under test:
//   - update_orchestrator rejects with `orchestrator_untracked_collision`
//     (resolvable) ⇒ store.untrackedCollision is set, error is null.
//   - update_orchestrator rejects with `orchestrator_autostash_pop_conflict`
//     ⇒ store.autostashPop is set, error is null.
//   - update_orchestrator rejects with `orchestrator_update_non_ff`
//     ⇒ store.nonFf is set (unchanged behavior; regression guard).
//   - update_orchestrator rejects with a plain (non-JSON) error
//     ⇒ store.error is the raw string, all three payload fields null.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { get, writable } from 'svelte/store';

// localStorage polyfill — updater.ts reads localStorage synchronously at
// store construction (loadSeen()).
beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const s = new Map<string, string>();
    (globalThis as { localStorage?: Storage }).localStorage = {
      get length() {
        return s.size;
      },
      clear() {
        s.clear();
      },
      getItem(k: string) {
        return s.has(k) ? (s.get(k) as string) : null;
      },
      key(i: number) {
        return Array.from(s.keys())[i] ?? null;
      },
      removeItem(k: string) {
        s.delete(k);
      },
      setItem(k: string, v: string) {
        s.set(k, String(v));
      },
    } as Storage;
  }
});

type OrchValue = {
  status: string;
  version: string;
  installPath: string;
  updateStatus: Record<string, unknown> | null;
  lastCheckFailed: boolean | null;
};

const orchStore = writable<OrchValue>({
  status: 'installed',
  version: '0.2.87',
  installPath: '/install/root',
  updateStatus: null,
  lastCheckFailed: null,
});

// update_orchestrator is the call runUpdate() awaits. We control its rejection
// per-test to exercise the routing.
let updateOrchestratorReject: unknown = null;
const updateOrchestratorMock = vi.fn(async () => {
  if (updateOrchestratorReject !== null) {
    throw updateOrchestratorReject instanceof Error
      ? updateOrchestratorReject
      : new Error(String(updateOrchestratorReject));
  }
});
const checkStatusMock = vi.fn(async () => {});

let tauriIsAvailable = true;

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
  tauriAvailable: () => tauriIsAvailable,
}));

vi.mock('./orchestrator', () => ({
  orchestrator: {
    subscribe: orchStore.subscribe,
    checkStatus: checkStatusMock,
    update_orchestrator: updateOrchestratorMock,
  },
  cancelScheduledRetry: () => {},
}));

let updater: typeof import('./updater').updater;

beforeEach(async () => {
  vi.resetModules();
  updateOrchestratorMock.mockClear();
  checkStatusMock.mockClear();
  tauriIsAvailable = true;
  updateOrchestratorReject = null;
  orchStore.set({
    status: 'installed',
    version: '0.2.87',
    installPath: '/install/root',
    updateStatus: null,
    lastCheckFailed: null,
  });
  const mod = await import('./updater');
  updater = mod.updater;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('runUpdate() error routing (v0.2.88)', () => {
  it('routes an untracked-collision payload to store.untrackedCollision', async () => {
    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_untracked_collision',
      operation: 'merge',
      branch: 'main',
      resolvable: true,
      identical_files: ['vco_lib/x.py'],
      divergent_files: ['docs/SPEC.md'],
      git_stderr: 'error: The following untracked working tree files...',
    });

    await updater.runUpdate();

    const s = get(updater);
    expect(s.untrackedCollision).not.toBeNull();
    expect(s.untrackedCollision?.event).toBe('orchestrator_untracked_collision');
    expect(s.untrackedCollision?.identical_files).toEqual(['vco_lib/x.py']);
    expect(s.untrackedCollision?.divergent_files).toEqual(['docs/SPEC.md']);
    expect(s.autostashPop).toBeNull();
    expect(s.nonFf).toBeNull();
    expect(s.error).toBeNull();
    expect(s.updating).toBe(false);
  });

  it('routes an autostash-pop payload to store.autostashPop (NOT a merge failure)', async () => {
    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_autostash_pop_conflict',
      branch: 'main',
      conflicted_files: ['launcher/dist/linux-x64/vct-hub'],
      git_stderr: "Merge made by the 'ort' strategy.\nApplying autostash resulted in conflicts.",
    });

    await updater.runUpdate();

    const s = get(updater);
    expect(s.autostashPop).not.toBeNull();
    expect(s.autostashPop?.event).toBe('orchestrator_autostash_pop_conflict');
    expect(s.autostashPop?.conflicted_files).toEqual(['launcher/dist/linux-x64/vct-hub']);
    expect(s.untrackedCollision).toBeNull();
    expect(s.nonFf).toBeNull();
    expect(s.error).toBeNull();
  });

  it('still routes a non-FF divergence payload to store.nonFf (regression guard)', async () => {
    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_update_non_ff',
      branch: 'main',
      local_sha: 'aaa',
      remote_sha: 'bbb',
      diverged_files: ['CLAUDE.md'],
      git_stderr: 'Not possible to fast-forward',
    });

    await updater.runUpdate();

    const s = get(updater);
    expect(s.nonFf).not.toBeNull();
    expect(s.nonFf?.event).toBe('orchestrator_update_non_ff');
    expect(s.untrackedCollision).toBeNull();
    expect(s.autostashPop).toBeNull();
    expect(s.error).toBeNull();
  });

  it('surfaces a plain non-JSON error as store.error with all payload fields null', async () => {
    updateOrchestratorReject = 'git pull failed: could not resolve host';

    await updater.runUpdate();

    const s = get(updater);
    expect(s.error).toContain('could not resolve host');
    expect(s.untrackedCollision).toBeNull();
    expect(s.autostashPop).toBeNull();
    expect(s.nonFf).toBeNull();
  });

  it('dismiss helpers clear their respective payloads', async () => {
    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_untracked_collision',
      operation: 'merge',
      branch: 'main',
      resolvable: true,
      identical_files: [],
      divergent_files: ['docs/SPEC.md'],
    });
    await updater.runUpdate();
    expect(get(updater).untrackedCollision).not.toBeNull();
    updater.dismissUntrackedCollision();
    expect(get(updater).untrackedCollision).toBeNull();

    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_autostash_pop_conflict',
      branch: 'main',
      conflicted_files: ['a'],
      git_stderr: '',
    });
    await updater.runUpdate();
    expect(get(updater).autostashPop).not.toBeNull();
    updater.dismissAutostashPop();
    expect(get(updater).autostashPop).toBeNull();
  });
});
