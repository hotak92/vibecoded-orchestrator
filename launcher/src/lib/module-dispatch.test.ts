// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.91 decision #24 — the legacy manifest-action gate, renderer side.
//
// The POLICY (which command names a manifest may dispatch) lives in Rust and
// is tested there (`module_dispatch.rs::tests`). What is tested HERE is the
// renderer's half of the contract, which is exactly where the hole was:
//
//   * a legacy string action is NOT invoked until the backend has allowed it;
//   * a refusal propagates the BACKEND's message, unaltered;
//   * a failing allow-query REFUSES rather than falling through to invoke
//     (a gate that opens when it cannot check is not a gate);
//   * a declarative descriptor still bypasses the query entirely (it is
//     gated in-process, and a second round trip would be dead weight).
//
// Deliberately NO list of allowed names in this file: asserting a mirrored
// allowlist here would create the second copy the design forbids. The names
// used below are stand-ins — the mock decides the verdict.

import { describe, expect, it, vi, beforeEach } from 'vitest';

const invokeMock = vi.fn();
vi.mock('$lib/tauri', () => ({
  invoke: (name: string, args?: Record<string, unknown>) => invokeMock(name, args),
}));

import {
  assertLegacyActionAllowed,
  dispatchAction,
  _resetLegacyVerdictCacheForTests,
} from './module-dispatch';

const CTX = { moduleId: 'vct-rl-reranker', projectId: 'p-1' };

/** Backend that allows everything except the named commands. */
function backendRefusing(...refused: string[]) {
  return (name: string, args?: Record<string, unknown>) => {
    if (name === 'module_manifest_command_allowed') {
      const command = String(args?.command ?? '');
      if (refused.includes(command)) {
        return Promise.reject(
          new Error(
            `module_dispatch: manifest-dispatched Tauri command '${command}' is ` +
              'not whitelisted (allowed: any name starting with \'module_\' OR one ' +
              'of [...]). Reject prevents manifest-driven RCE.',
          ),
        );
      }
      return Promise.resolve(null);
    }
    return Promise.resolve({ ok: true });
  };
}

beforeEach(() => {
  invokeMock.mockReset();
  _resetLegacyVerdictCacheForTests();
});

describe('assertLegacyActionAllowed', () => {
  it('resolves for a name the backend allows', async () => {
    invokeMock.mockImplementation(backendRefusing());
    await expect(assertLegacyActionAllowed('set_rl_use_global')).resolves.toBeUndefined();
    expect(invokeMock).toHaveBeenCalledWith('module_manifest_command_allowed', {
      command: 'set_rl_use_global',
    });
  });

  it('throws the backend message verbatim for a refused name', async () => {
    invokeMock.mockImplementation(backendRefusing('delete_project_v2'));
    await expect(assertLegacyActionAllowed('delete_project_v2')).rejects.toThrow(
      /'delete_project_v2' is not whitelisted/,
    );
  });

  it('refuses when the allow-query itself fails', async () => {
    // Backend unreachable / IPC error. The safe direction is REFUSE.
    invokeMock.mockImplementation(() => Promise.reject(new Error('ipc unavailable')));
    await expect(assertLegacyActionAllowed('set_rl_use_global')).rejects.toThrow(
      /ipc unavailable/,
    );
  });

  it('memoises a verdict per name (the allowlist is compile-time constant)', async () => {
    invokeMock.mockImplementation(backendRefusing('delete_project_v2'));

    await assertLegacyActionAllowed('set_rl_use_global');
    await assertLegacyActionAllowed('set_rl_use_global');
    await expect(assertLegacyActionAllowed('delete_project_v2')).rejects.toThrow();
    await expect(assertLegacyActionAllowed('delete_project_v2')).rejects.toThrow(
      // A repeat refusal must stay as informative as the first — the memo
      // stores the message, not a bare boolean.
      /not whitelisted/,
    );

    const queries = invokeMock.mock.calls.filter(
      ([name]) => name === 'module_manifest_command_allowed',
    );
    expect(queries.map(([, args]) => args?.command)).toEqual([
      'set_rl_use_global',
      'delete_project_v2',
    ]);
  });

  it('does NOT memoise a transient failure — a later call re-queries and can succeed (NIT-6)', async () => {
    // First call: backend/IPC hiccup unrelated to the allowlist ("ipc
    // unavailable" does not match the policy-refusal shape). Second call:
    // the same transient condition has cleared and the backend now
    // answers normally. If the first failure had been cached identically
    // to a real refusal, the second call would short-circuit from the
    // cache and stay refused forever — a first-party control stuck
    // blocked until reload.
    let calls = 0;
    invokeMock.mockImplementation((name: string) => {
      if (name === 'module_manifest_command_allowed') {
        calls += 1;
        return calls === 1
          ? Promise.reject(new Error('ipc unavailable'))
          : Promise.resolve(null);
      }
      return Promise.resolve({ ok: true });
    });

    await expect(assertLegacyActionAllowed('set_rl_use_global')).rejects.toThrow(
      /ipc unavailable/,
    );
    await expect(assertLegacyActionAllowed('set_rl_use_global')).resolves.toBeUndefined();

    const queries = invokeMock.mock.calls.filter(
      ([name]) => name === 'module_manifest_command_allowed',
    );
    expect(queries).toHaveLength(2);
  });

  it('DOES memoise a real policy refusal — a later call is served from cache', async () => {
    invokeMock.mockImplementation(backendRefusing('delete_project_v2'));

    await expect(assertLegacyActionAllowed('delete_project_v2')).rejects.toThrow(
      /not whitelisted/,
    );
    await expect(assertLegacyActionAllowed('delete_project_v2')).rejects.toThrow(
      /not whitelisted/,
    );

    const queries = invokeMock.mock.calls.filter(
      ([name]) => name === 'module_manifest_command_allowed',
    );
    expect(queries).toHaveLength(1);
  });
});

describe('dispatchAction — legacy string form', () => {
  it('does not invoke a refused command at all', async () => {
    invokeMock.mockImplementation(backendRefusing('delete_project_v2'));

    await expect(dispatchAction(CTX, 'delete_project_v2')).rejects.toThrow(
      /not whitelisted/,
    );

    // The REAL assertion: the destructive command never reached invoke().
    const dispatched = invokeMock.mock.calls.filter(
      ([name]) => name !== 'module_manifest_command_allowed',
    );
    expect(dispatched).toEqual([]);
  });

  it('still dispatches an allowed command, with the legacy arg shape', async () => {
    invokeMock.mockImplementation(backendRefusing('delete_project_v2'));

    await dispatchAction(CTX, 'set_rl_use_global', true);

    const dispatched = invokeMock.mock.calls.filter(
      ([name]) => name !== 'module_manifest_command_allowed',
    );
    expect(dispatched).toEqual([
      ['set_rl_use_global', { moduleId: 'vct-rl-reranker', projectId: 'p-1', value: true }],
    ]);
  });

  it('checks BEFORE invoking, not after', async () => {
    const order: string[] = [];
    invokeMock.mockImplementation((name: string) => {
      order.push(name);
      return Promise.resolve(null);
    });

    await dispatchAction(CTX, 'set_rl_use_global');

    expect(order).toEqual(['module_manifest_command_allowed', 'set_rl_use_global']);
  });
});

describe('dispatchAction — declarative descriptor form', () => {
  it('goes straight to module_dispatch_action with no allow-query', async () => {
    invokeMock.mockImplementation(() => Promise.resolve({ ok: true }));

    await dispatchAction(CTX, {
      kind: 'http',
      method: 'POST',
      path: '/retrain',
    } as never);

    expect(invokeMock.mock.calls.map(([name]) => name)).toEqual([
      'module_dispatch_action',
    ]);
  });
});
