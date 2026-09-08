// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.93 (field incident 2026-09-07): tests for the updater store's
// `beginOp` / `endOp` bracket (the ONE live progress indicator for every
// update-class operation), the hoisted `conflict` field, the badge's
// `merge_in_progress` kind priority, and the op-based overlay titles.
//
// Contract under test:
//   - beginOp(kind) ⇒ updating=true, op=kind, error=null, the orchestrator's
//     progress snapshot is reset, and the REAL ui store's overlay flag opens.
//   - beginOp never touches the decision-modal payload fields (a merge
//     started from the divergence modal must keep `nonFf`).
//   - endOp() ⇒ updating=false, error=null, op RETAINED (overlay title stays).
//   - endOp(err) ⇒ error is the error's text (Error instance or string).
//   - runUpdate() rejecting with `orchestrator_update_conflict` — including
//     the whitespace-prefixed shape from the incident — routes to `conflict`.
//   - setConflict clears nonFf; dismissConflict clears conflict.
//   - openPendingConflict: parses `get_pending_conflict_payload`, routes a
//     bad payload / a rejection to `error`.
//   - pickKind: merge_in_progress beats every other kind.
//   - titleForUpdateKind / titleForUpdateOp / runningMessageForUpdateOp.

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
  progress: unknown;
};

const orchStore = writable<OrchValue>({
  status: 'installed',
  version: '0.2.92',
  installPath: '/install/root',
  updateStatus: null,
  lastCheckFailed: null,
  progress: { stage: 'done', message: 'stale', percentage: 100, error: null },
});

let updateOrchestratorReject: unknown = null;
const updateOrchestratorMock = vi.fn(async () => {
  if (updateOrchestratorReject !== null) {
    throw updateOrchestratorReject instanceof Error
      ? updateOrchestratorReject
      : new Error(String(updateOrchestratorReject));
  }
});
const checkStatusMock = vi.fn(async () => {});
const resetProgressMock = vi.fn(() => {
  orchStore.update((s) => ({ ...s, progress: null }));
});

let tauriIsAvailable = true;
// `invoke` is controlled per test for `get_pending_conflict_payload`.
let invokeImpl: (cmd: string, args?: unknown) => Promise<unknown> = async () => undefined;

vi.mock('$lib/tauri', () => ({
  invoke: (cmd: string, args?: unknown) => invokeImpl(cmd, args),
  safeInvoke: vi.fn(async () => null),
  listen: vi.fn(async () => () => {}),
  tauriAvailable: () => tauriIsAvailable,
  isTauriRuntime: () => false,
}));

vi.mock('./orchestrator', () => ({
  orchestrator: {
    subscribe: orchStore.subscribe,
    checkStatus: checkStatusMock,
    update_orchestrator: updateOrchestratorMock,
    resetProgress: resetProgressMock,
  },
  cancelScheduledRetry: () => {},
  renderCheck: () => 'ok',
  checkError: () => null,
}));

type UpdaterModule = typeof import('./updater');
type UiModule = typeof import('./ui');
let mod: UpdaterModule;
let updater: UpdaterModule['updater'];
let ui: UiModule['ui'];

const CONFLICT = {
  event: 'orchestrator_update_conflict',
  operation: 'merge',
  branch: 'main',
  conflicted_files: ['CLAUDE.md'],
  git_stderr: 'CONFLICT (content): Merge conflict in CLAUDE.md',
};

beforeEach(async () => {
  vi.resetModules();
  updateOrchestratorMock.mockClear();
  checkStatusMock.mockClear();
  resetProgressMock.mockClear();
  tauriIsAvailable = true;
  updateOrchestratorReject = null;
  invokeImpl = async () => undefined;
  orchStore.set({
    status: 'installed',
    version: '0.2.92',
    installPath: '/install/root',
    updateStatus: null,
    lastCheckFailed: null,
    progress: { stage: 'done', message: 'stale', percentage: 100, error: null },
  });
  mod = await import('./updater');
  updater = mod.updater;
  // The REAL ui store (via the `$app/navigation` vitest alias stub) — the
  // overlay flag assertion is against the store the layout actually reads.
  ui = (await import('./ui')).ui;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('beginOp / endOp (v0.2.93)', () => {
  it('beginOp sets updating+op, clears error, resets progress, opens the overlay', () => {
    expect(get(ui).showOrchestratorUpdateProgress).toBe(false);
    updater.beginOp('merge');
    const s = get(updater);
    expect(s.updating).toBe(true);
    expect(s.op).toBe('merge');
    expect(s.error).toBeNull();
    expect(resetProgressMock).toHaveBeenCalledTimes(1);
    expect(get(orchStore).progress).toBeNull();
    expect(get(ui).showOrchestratorUpdateProgress).toBe(true);
  });

  it('beginOp clears a stale error from a previous op', () => {
    updater.beginOp('merge');
    updater.endOp('first attempt failed');
    expect(get(updater).error).toBe('first attempt failed');
    updater.beginOp('rebase');
    expect(get(updater).error).toBeNull();
    expect(get(updater).op).toBe('rebase');
  });

  it('beginOp does NOT clear the decision-modal payload fields (nonFf survives a merge)', async () => {
    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_update_non_ff',
      branch: 'main',
      local_sha: 'a',
      remote_sha: 'b',
      diverged_files: ['CLAUDE.md'],
      git_stderr: 'fatal: Not possible to fast-forward',
    });
    await updater.runUpdate();
    expect(get(updater).nonFf).not.toBeNull();
    expect(get(updater).updating).toBe(false);
    // The divergence modal now starts a merge FROM that state.
    updater.beginOp('merge');
    expect(get(updater).nonFf).not.toBeNull();
    expect(get(updater).updating).toBe(true);
  });

  it('endOp() ends the op with no error and RETAINS op for the overlay title', () => {
    updater.beginOp('keep_local');
    updater.endOp();
    const s = get(updater);
    expect(s.updating).toBe(false);
    expect(s.error).toBeNull();
    expect(s.op).toBe('keep_local');
  });

  it('endOp(err) surfaces the error text for a string and for an Error', () => {
    updater.beginOp('abort');
    updater.endOp('Abort failed: boom');
    expect(get(updater).updating).toBe(false);
    expect(get(updater).error).toBe('Abort failed: boom');

    updater.beginOp('abort');
    updater.endOp(new Error('as-error'));
    expect(get(updater).error).toBe('as-error');
  });

  it('endOp(null) is a success, same as endOp()', () => {
    updater.beginOp('update');
    updater.endOp(null);
    expect(get(updater).error).toBeNull();
    expect(get(updater).updating).toBe(false);
  });

  it('runUpdate brackets itself with beginOp/endOp (op=update, overlay opened)', async () => {
    await updater.runUpdate();
    const s = get(updater);
    expect(s.op).toBe('update');
    expect(s.updating).toBe(false);
    expect(s.error).toBeNull();
    expect(resetProgressMock).toHaveBeenCalledTimes(1);
    expect(get(ui).showOrchestratorUpdateProgress).toBe(true);
    expect(checkStatusMock).toHaveBeenCalledTimes(1);
  });

  it('runUpdate is a no-op outside Tauri (no overlay, no op)', async () => {
    tauriIsAvailable = false;
    await updater.runUpdate();
    expect(get(updater).op).toBeNull();
    expect(get(ui).showOrchestratorUpdateProgress).toBe(false);
    expect(resetProgressMock).not.toHaveBeenCalled();
  });
});

describe('conflict routing (hoisted `conflict` field)', () => {
  it('runUpdate rejecting with the conflict payload routes to store.conflict, error null', async () => {
    updateOrchestratorReject = JSON.stringify(CONFLICT);
    await updater.runUpdate();
    const s = get(updater);
    expect(s.conflict?.operation).toBe('merge');
    expect(s.conflict?.conflicted_files).toEqual(['CLAUDE.md']);
    expect(s.error).toBeNull();
    expect(s.nonFf).toBeNull();
    expect(s.updating).toBe(false);
  });

  it('routes the WHITESPACE-PREFIXED conflict payload too (the incident shape, end-to-end through the store)', async () => {
    updateOrchestratorReject = `\n  ${JSON.stringify(CONFLICT)}`;
    await updater.runUpdate();
    expect(get(updater).conflict).not.toBeNull();
    expect(get(updater).error).toBeNull();
  });

  it('a plain error still lands in store.error with every payload field null', async () => {
    updateOrchestratorReject = 'git: command not found';
    await updater.runUpdate();
    const s = get(updater);
    expect(s.error).toBe('git: command not found');
    expect(s.conflict).toBeNull();
    expect(s.nonFf).toBeNull();
    expect(s.untrackedCollision).toBeNull();
    expect(s.autostashPop).toBeNull();
  });

  it('setConflict clears nonFf (never two decision modals at once); dismissConflict clears conflict', async () => {
    updateOrchestratorReject = JSON.stringify({
      event: 'orchestrator_update_non_ff',
      branch: 'main',
      local_sha: 'a',
      remote_sha: 'b',
      diverged_files: [],
      git_stderr: '',
    });
    await updater.runUpdate();
    expect(get(updater).nonFf).not.toBeNull();
    updater.setConflict(CONFLICT as never);
    expect(get(updater).nonFf).toBeNull();
    expect(get(updater).conflict).not.toBeNull();
    updater.dismissConflict();
    expect(get(updater).conflict).toBeNull();
  });
});

describe('openPendingConflict (badge merge_in_progress action)', () => {
  it('parses the payload from get_pending_conflict_payload and opens the conflict modal', async () => {
    const calls: Array<{ cmd: string; args: unknown }> = [];
    invokeImpl = async (cmd, args) => {
      calls.push({ cmd, args });
      return JSON.stringify(CONFLICT);
    };
    await updater.openPendingConflict();
    expect(calls).toEqual([
      { cmd: 'get_pending_conflict_payload', args: { path: '/install/root' } },
    ]);
    expect(get(updater).conflict?.branch).toBe('main');
    expect(get(updater).error).toBeNull();
    // A read, not an update-class op: no overlay.
    expect(get(ui).showOrchestratorUpdateProgress).toBe(false);
    expect(get(updater).updating).toBe(false);
  });

  it('an unparseable payload becomes a visible error, not a silent nothing', async () => {
    invokeImpl = async () => 'not json at all';
    await updater.openPendingConflict();
    expect(get(updater).conflict).toBeNull();
    expect(get(updater).error).toContain('not json at all');
  });

  it('a rejected command becomes a visible error', async () => {
    invokeImpl = async () => {
      throw new Error('no MERGE_HEAD');
    };
    await updater.openPendingConflict();
    expect(get(updater).conflict).toBeNull();
    expect(get(updater).error).toBe('no MERGE_HEAD');
  });
});

describe('pickKind priority (v0.2.93)', () => {
  const all = {
    remote_ahead: true,
    install_stale: true,
    binary_stale: true,
    merge_resolved_incomplete: true,
  };

  it('merge_in_progress beats every other kind', () => {
    expect(mod.pickKind({ ...all, merge_in_progress: true })).toBe('merge_in_progress');
  });

  it('with merge_in_progress false/absent, merge_resolved_incomplete is still highest', () => {
    expect(mod.pickKind({ ...all, merge_in_progress: false })).toBe('merge_resolved_incomplete');
    expect(mod.pickKind(all)).toBe('merge_resolved_incomplete');
  });

  it('keeps binary > install > remote below the two merge kinds', () => {
    expect(
      mod.pickKind({ remote_ahead: true, install_stale: true, binary_stale: true }),
    ).toBe('binary_stale');
    // v0.2.93 (field 2026-09-08): a half-finished install must NOT mask the
    // only action that pulls — the update flow includes the install.
    expect(mod.pickKind({ remote_ahead: true, install_stale: true, binary_stale: false })).toBe(
      'remote_ahead',
    );
    expect(mod.pickKind({ remote_ahead: false, install_stale: true, binary_stale: false })).toBe(
      'install_stale',
    );
    expect(mod.pickKind({ remote_ahead: true, install_stale: false, binary_stale: false })).toBe(
      'remote_ahead',
    );
    expect(mod.pickKind({ remote_ahead: false, install_stale: false, binary_stale: false })).toBe(
      null,
    );
    expect(mod.pickKind(null)).toBe(null);
  });

  it('merge_in_progress alone (older flags all false) still renders the stalled-merge badge', () => {
    expect(
      mod.pickKind({
        remote_ahead: false,
        install_stale: false,
        binary_stale: false,
        merge_in_progress: true,
      }),
    ).toBe('merge_in_progress');
  });
});

describe('overlay titles / messages per op (v0.2.93)', () => {
  it('titleForUpdateKind titles the new kind', () => {
    expect(mod.titleForUpdateKind('merge_in_progress')).toBe('Resolving merge conflict');
  });

  it('titleForUpdateOp: the op wins over the badge kind', () => {
    expect(mod.titleForUpdateOp('merge', 'remote_ahead')).toBe('Merging upstream changes');
    expect(mod.titleForUpdateOp('rebase', null)).toBe('Rebasing onto upstream');
    expect(mod.titleForUpdateOp('keep_local', null)).toBe('Keeping local versions');
    expect(mod.titleForUpdateOp('accept_upstream', null)).toBe('Accepting upstream versions');
    expect(mod.titleForUpdateOp('abort', null)).toBe('Aborting merge');
    expect(mod.titleForUpdateOp('restart', null)).toBe('Restarting launcher');
    expect(mod.titleForUpdateOp('install', null)).toBe('Installing update');
    expect(mod.titleForUpdateOp('update', null)).toBe('Updating orchestrator');
  });

  it('titleForUpdateOp: resume keeps the autostash-pop distinction', () => {
    expect(mod.titleForUpdateOp('resume', null, 'autostash-pop')).toBe('Finishing update');
    expect(mod.titleForUpdateOp('resume', null, 'merge')).toBe('Resuming update');
  });

  it('titleForUpdateOp: with no op, falls back to the kind-based title', () => {
    expect(mod.titleForUpdateOp(null, 'install_stale')).toBe('Installing update');
    expect(mod.titleForUpdateOp(null, null)).toBe('Updating orchestrator');
  });

  it('runningMessageForUpdateOp names the git phase for the progress-less ops', () => {
    expect(mod.runningMessageForUpdateOp('merge')).toMatch(/merge/i);
    expect(mod.runningMessageForUpdateOp('rebase')).toMatch(/rebase/i);
    expect(mod.runningMessageForUpdateOp('abort')).toMatch(/restoring/i);
    expect(mod.runningMessageForUpdateOp(null)).toBe('Working…');
  });

  it('updateOpRestartsOnSuccess: only abort does not end in a restart', () => {
    expect(mod.updateOpRestartsOnSuccess('abort')).toBe(false);
    expect(mod.updateOpRestartsOnSuccess('merge')).toBe(true);
    expect(mod.updateOpRestartsOnSuccess('update')).toBe(true);
  });
});
