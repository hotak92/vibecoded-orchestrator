// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-08, L3-F03): `updater.state.failed` is THE failure signal.
//
// The defect: `endOp(err)` stored `errorText(err)`, and a failure whose text
// was the EMPTY string left `error === ""` — falsy — so the overlay, which read
// `!!upd.error`, celebrated "Update complete" for a failed update.
//
// Contract under test:
//   - endOp('') / a run rejecting with '' ⇒ failed=true AND a non-empty error;
//   - endOp() / endOp(null) ⇒ failed=false, error=null;
//   - beginOp and clearError reset `failed`;
//   - a run that hands over to a decision modal is NOT a failure state;
//   - the displayed text never starts with "Update failed" (the overlay and
//     the popover render their own heading — one prefix, not two).

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

beforeAll(() => {
  if (typeof (globalThis as { localStorage?: Storage }).localStorage === 'undefined') {
    const s = new Map<string, string>();
    (globalThis as { localStorage?: Storage }).localStorage = {
      get length() {
        return s.size;
      },
      clear: () => s.clear(),
      getItem: (k: string) => (s.has(k) ? (s.get(k) as string) : null),
      key: (i: number) => Array.from(s.keys())[i] ?? null,
      removeItem: (k: string) => void s.delete(k),
      setItem: (k: string, v: string) => void s.set(k, String(v)),
    } as Storage;
  }
});

let runReject: unknown = undefined;

vi.mock('$lib/tauri', () => ({
  invoke: async (cmd: string) => {
    if (cmd === 'run_orchestrator_update' && runReject !== undefined) throw runReject;
    return undefined;
  },
  safeInvoke: async () => null,
  listen: async () => () => {},
  tauriAvailable: () => true,
  isTauriRuntime: () => false,
}));

type Updater = typeof import('./updater');
let U: Updater;

beforeEach(async () => {
  vi.resetModules();
  runReject = undefined;
  U = await import('./updater');
});

describe('endOp — failure is a boolean, not error truthiness', () => {
  it("endOp('') is a FAILURE with a non-empty explanation", () => {
    U.updater.beginOp('update');
    U.updater.endOp('');
    const s = get(U.updater);
    expect(s.failed).toBe(true);
    expect(s.error).toBe(U.EMPTY_FAILURE_TEXT);
    expect(s.updating).toBe(false);
  });

  it('endOp(whitespace) and endOp(new Error("")) are failures too', () => {
    U.updater.endOp('   \n');
    expect(get(U.updater).failed).toBe(true);
    expect(get(U.updater).error).toBe(U.EMPTY_FAILURE_TEXT);
    U.updater.endOp(new Error(''));
    expect(get(U.updater).failed).toBe(true);
    expect(get(U.updater).error).toBe(U.EMPTY_FAILURE_TEXT);
  });

  it('endOp() and endOp(null) are success', () => {
    U.updater.endOp('x');
    U.updater.endOp();
    expect(get(U.updater)).toMatchObject({ failed: false, error: null });
    U.updater.endOp('x');
    U.updater.endOp(null);
    expect(get(U.updater)).toMatchObject({ failed: false, error: null });
  });

  it('beginOp and clearError reset the failed state', () => {
    U.updater.endOp('boom');
    U.updater.beginOp('install');
    expect(get(U.updater).failed).toBe(false);
    U.updater.endOp('boom');
    U.updater.clearError();
    expect(get(U.updater)).toMatchObject({ failed: false, error: null });
  });

  it('a failure text is rendered with ONE heading: a leading "Update failed:" is stripped', () => {
    U.updater.endOp('Update failed: Update failed: install.py exited 1');
    expect(get(U.updater).error).toBe('install.py exited 1');
    U.updater.endOp('Update failed:');
    expect(get(U.updater).error).toBe(U.EMPTY_FAILURE_TEXT);
  });
});

describe('run(kind) failures', () => {
  it("a run rejecting with '' renders the FAILED state (the L3-F03 shape)", async () => {
    runReject = '';
    const r = await U.updater.run('PullFf');
    expect(r.ok).toBe(false);
    const s = get(U.updater);
    expect(s.failed).toBe(true);
    expect(s.error).toBe(U.EMPTY_FAILURE_TEXT);
  });

  it('a typed InstallFailed with an empty message is still a failure with text', async () => {
    runReject = JSON.stringify({ kind: 'InstallFailed', message: '', log_path: '/l/install.log' });
    await U.updater.run('ApplyOnly');
    const s = get(U.updater);
    expect(s.failed).toBe(true);
    expect(s.error).toContain(U.EMPTY_FAILURE_TEXT);
    expect(s.error).toContain('/l/install.log');
  });

  it('a hand-over to a decision modal is not a failure', async () => {
    runReject = JSON.stringify({
      kind: 'Conflict',
      message: 'merge stopped at a conflict',
      operation: 'merge',
      branch: 'main',
      conflicted_files: ['CLAUDE.md'],
      git_stderr: '',
    });
    await U.updater.run('PullFf');
    const s = get(U.updater);
    expect(s.failed).toBe(false);
    expect(s.error).toBeNull();
    expect(s.conflict?.conflicted_files).toEqual(['CLAUDE.md']);
  });

  it('failOp routes like run and sets failed for plain text', () => {
    U.updater.beginOp('keep_local');
    const routed = U.updater.failOp('');
    expect(routed.to).toBe('failed');
    expect(get(U.updater).failed).toBe(true);
    expect(get(U.updater).error).toBe(U.EMPTY_FAILURE_TEXT);
  });
});
