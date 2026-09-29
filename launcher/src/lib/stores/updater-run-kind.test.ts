// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-08, AD-1): `updater.run(kind)` is THE update action, and every
// surface reaches the backend through it.
//
// Contract under test (REAL updater + orchestrator + ui stores; only the
// Tauri bridge is faked):
//   - run(kind) invokes `run_orchestrator_update` with exactly `{ kind }`, for
//     every kind, through the orchestrator store (status → `updating` during
//     the call — L3-F07: the old resume path skipped that);
//   - run(kind) opens the ONE overlay with the op that kind drives;
//   - a success re-checks status and clears the badge state;
//   - every badge state's action (`actionForKind`, shared by the badge and the
//     Updates page) reaches the right backend call via `updater.perform`;
//   - Merge / Rebase keep the divergence payload while running and clear it on
//     success; Resume keeps the conflict payload;
//   - the wording table (`badgeCopyFor`) is keyed on the kind and never hides
//     a remote update behind a stale binary.

import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

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

type Call = { cmd: string; args: unknown };
let calls: Call[] = [];
// Per-command behaviour; default resolves undefined.
let behaviour: Record<string, (args: unknown) => Promise<unknown>> = {};
// What the orchestrator store's status was WHILE the backend ran.
let statusDuringRun: string | null = null;

vi.mock('$lib/tauri', () => ({
  invoke: async (cmd: string, args?: unknown) => {
    calls.push({ cmd, args });
    const b = behaviour[cmd];
    return b ? b(args) : undefined;
  },
  // checkStatus → safeInvoke('get_known_install_path') … returns null in this
  // fake, so checkStatus is a recorded no-op.
  safeInvoke: async (cmd: string, args?: unknown) => {
    calls.push({ cmd, args });
    return null;
  },
  listen: async () => () => {},
  tauriAvailable: () => true,
  isTauriRuntime: () => false,
}));

type Updater = typeof import('./updater');
type Orch = typeof import('./orchestrator');
let U: Updater;
let O: Orch;
let ui: typeof import('./ui')['ui'];

beforeEach(async () => {
  vi.resetModules();
  calls = [];
  behaviour = {};
  statusDuringRun = null;
  U = await import('./updater');
  O = await import('./orchestrator');
  ui = (await import('./ui')).ui;
  O.orchestrator.setInstallPath('/install/root');
  behaviour.run_orchestrator_update = async () => {
    statusDuringRun = get(O.orchestrator).status;
    return {
      kind: 'PullFf',
      head_before: 'a',
      head_after: 'b',
      install_py_ran: true,
      restarted: false,
      log_path: '/logs/x.log',
    };
  };
});

function runCalls(): Call[] {
  return calls.filter((c) => c.cmd === 'run_orchestrator_update');
}

describe('run(kind) → run_orchestrator_update({kind})', () => {
  it.each(U_KINDS())('%s: one invoke with exactly {kind}', async (kind) => {
    const r = await U.updater.run(kind);
    expect(r.ok).toBe(true);
    expect(runCalls()).toEqual([{ cmd: 'run_orchestrator_update', args: { kind } }]);
    // L3-F07: every kind — Resume included — sets the store's `updating`.
    expect(statusDuringRun).toBe('updating');
    expect(get(O.orchestrator).status).toBe('installed');
  });

  it('opens the one overlay with the op the kind drives, and re-checks after', async () => {
    for (const kind of U.UPDATE_RUN_KINDS) {
      calls = [];
      ui.closeOrchestratorUpdateProgress();
      await U.updater.run(kind);
      const s = get(U.updater);
      expect(s.op).toBe(U.opForRunKind(kind));
      expect(s.updating).toBe(false);
      expect(s.failed).toBe(false);
      expect(get(ui).showOrchestratorUpdateProgress).toBe(true);
      // The post-run re-check went out (checkStatus's first probe).
      expect(calls.some((c) => c.cmd === 'get_known_install_path')).toBe(true);
    }
  });

  it('resolves to the backend outcome', async () => {
    const r = await U.updater.run('PullFf');
    expect(r).toEqual({
      ok: true,
      outcome: {
        kind: 'PullFf',
        head_before: 'a',
        head_after: 'b',
        install_py_ran: true,
        restarted: false,
        log_path: '/logs/x.log',
      },
    });
  });

  it('opForRunKind maps every kind to a titled op', () => {
    expect(U.UPDATE_RUN_KINDS.map((k) => U.opForRunKind(k))).toEqual([
      'update',
      'merge',
      'rebase',
      'resume',
      'install',
      'reset',
    ]);
    for (const k of U.UPDATE_RUN_KINDS) {
      expect(U.titleForUpdateOp(U.opForRunKind(k), null)).not.toBe('');
    }
    expect(U.titleForUpdateOp('reset', null)).toBe('Resetting to upstream');
  });
});

function U_KINDS(): Array<import('./updater').UpdateRunKind> {
  return ['PullFf', 'Merge', 'Rebase', 'Resume', 'ApplyOnly', 'ResetHard'];
}

describe('every badge state reaches the backend through the store (badge + Updates page)', () => {
  const cases: Array<[import('./updater').UpdateKind, Call]> = [
    ['remote_ahead', { cmd: 'run_orchestrator_update', args: { kind: 'PullFf' } }],
    ['install_stale', { cmd: 'run_orchestrator_update', args: { kind: 'ApplyOnly' } }],
    ['merge_resolved_incomplete', { cmd: 'run_orchestrator_update', args: { kind: 'Resume' } }],
    ['binary_stale', { cmd: 'restart_launcher', args: { installRoot: '/install/root' } }],
    ['merge_in_progress', { cmd: 'get_pending_conflict_payload', args: { path: '/install/root' } }],
  ];

  it.each(cases)('%s → %o', async (kind, expected) => {
    behaviour.get_pending_conflict_payload = async () =>
      JSON.stringify({
        event: 'orchestrator_update_conflict',
        operation: 'merge',
        branch: 'main',
        conflicted_files: ['a'],
        git_stderr: '',
      });
    await U.updater.perform(U.actionForKind(kind));
    const backend = calls.filter((c) =>
      ['run_orchestrator_update', 'restart_launcher', 'get_pending_conflict_payload'].includes(c.cmd),
    );
    expect(backend).toEqual([expected]);
  });

  it('no badge state → no action, no call', async () => {
    expect(U.actionForKind(null)).toBeNull();
    await U.updater.perform(null);
    expect(calls).toEqual([]);
  });

  it('perform works as a bare callback (no `this` binding)', async () => {
    const { perform } = U.updater;
    await perform({ type: 'run', kind: 'PullFf' });
    expect(runCalls()).toHaveLength(1);
  });
});

describe('decision-modal payloads across a run', () => {
  const NONFF = {
    event: 'orchestrator_update_non_ff',
    branch: 'main',
    local_sha: 'a',
    remote_sha: 'b',
    diverged_files: ['x'],
    git_stderr: '',
  };

  it('a Merge started from the divergence modal keeps nonFf WHILE running and clears it on success', async () => {
    behaviour.run_orchestrator_update = async () => {
      throw JSON.stringify({ kind: 'NonFastForward', message: 'diverged', ...NONFF });
    };
    await U.updater.run('PullFf');
    expect(get(U.updater).nonFf?.branch).toBe('main');
    let nonFfDuring: unknown = 'unset';
    behaviour.run_orchestrator_update = async () => {
      nonFfDuring = get(U.updater).nonFf;
      return null;
    };
    const r = await U.updater.run('Merge');
    expect(r.ok).toBe(true);
    expect(nonFfDuring).not.toBeNull();
    expect(get(U.updater).nonFf).toBeNull();
  });

  it('a fresh PullFf drops a stale nonFf', async () => {
    behaviour.run_orchestrator_update = async () => {
      throw JSON.stringify({ kind: 'NonFastForward', message: 'diverged', ...NONFF });
    };
    await U.updater.run('PullFf');
    let nonFfDuring: unknown = 'unset';
    behaviour.run_orchestrator_update = async () => {
      nonFfDuring = get(U.updater).nonFf;
      return null;
    };
    await U.updater.run('PullFf');
    expect(nonFfDuring).toBeNull();
  });

  it('Resume from the conflict modal keeps the conflict payload (the modal shows its own result)', async () => {
    U.updater.setConflict({
      event: 'orchestrator_update_conflict',
      operation: 'merge',
      branch: 'main',
      conflicted_files: ['a'],
      git_stderr: '',
    });
    const r = await U.updater.run('Resume');
    expect(r.ok).toBe(true);
    expect(get(U.updater).conflict).not.toBeNull();
  });
});

describe('badgeCopyFor — one wording table keyed on kind', () => {
  const us = {
    binary_stale: true,
    source_version: '0.2.100',
    installed_version: '0.2.99',
    running_version: '0.2.99',
    on_disk_binary_version: '0.2.100',
  };

  it('remote_ahead + binary_stale: the remote update is offered and the binary is named', () => {
    const kind = U.pickKind({ remote_ahead: true, install_stale: false, binary_stale: true });
    expect(kind).toBe('remote_ahead');
    const c = U.badgeCopyFor(kind, us);
    expect(c.action).toEqual({ type: 'run', kind: 'PullFf' });
    expect(c.binaryAlsoStale).toBe(true);
    expect(c.buttonLabel).toBe('Fetch + Install');
  });

  it('every kind has a title, a button and the action of actionForKind', () => {
    const kinds: Array<import('./updater').UpdateKind> = [
      'merge_in_progress',
      'merge_resolved_incomplete',
      'remote_ahead',
      'binary_stale',
      'install_stale',
    ];
    for (const k of kinds) {
      const c = U.badgeCopyFor(k, us);
      expect(c.title.length).toBeGreaterThan(0);
      expect(c.buttonLabel.length).toBeGreaterThan(0);
      expect(c.action).toEqual(U.actionForKind(k));
    }
    expect(U.badgeCopyFor(null, us).action).toBeNull();
  });

  it('binary_stale says why the binary is newer (L3-F11)', () => {
    expect(U.badgeCopyFor('binary_stale', us).desc).toMatch(/An update refreshed the launcher binary/);
  });

  it('autostash-pop resume is worded as Finish, a plain resume as Continue', () => {
    expect(U.badgeCopyFor('merge_resolved_incomplete', { resume_operation: 'autostash-pop' }).buttonLabel).toBe(
      'Finish Update',
    );
    expect(U.badgeCopyFor('merge_resolved_incomplete', { resume_operation: 'merge' }).buttonLabel).toBe(
      'Continue Update',
    );
  });
});

describe('DeferralBadge → update badge pointer', () => {
  it('counts only the update-resolvable condition ids', () => {
    expect(
      U.updateDeferralCount([
        'update_resume_required',
        'launcher_restart_required',
        'weaviate_unreachable_at_update',
        'safe_add_skipped_env_merge',
      ]),
    ).toBe(2);
    expect(U.updateDeferralCount([])).toBe(0);
  });

  it('every id is a registered deferral condition (vco_lib/deferral_conditions.toml)', () => {
    const toml = readFileSync(
      fileURLToPath(new URL('../../../../vco_lib/deferral_conditions.toml', import.meta.url)),
      'utf-8',
    );
    for (const id of U.UPDATE_BADGE_DEFERRAL_IDS) {
      expect(toml, id).toContain(`[conditions.${id}]`);
    }
  });
});
