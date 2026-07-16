// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.83 (WP-A2 / D3, A-RC2): REGRESSION PIN for the orchestrator store's
// remote-check retry scheduling.
//
// When `checkStatus()` lands `remote_check_ok === false` (the remote probe
// could not determine whether an update exists), the store schedules a short
// burst of retries — 30s → 90s → 300s, capped at 3 per failure episode,
// single-flight (one pending timer), and the episode RESETS the moment a
// check lands `remote_check_ok !== false` (ok, OR the field MISSING — older
// Rust back-compat). Without the fix a transient startup miss meant up to an
// hour of false "no update".
//
// We mock `$lib/tauri` so `checkStatus` runs against controllable fakes and
// drive `setTimeout` with vitest fake timers to assert the cadence + caps.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

// ---------------------------------------------------------------------------
// $lib/tauri mock. `orchestrator.ts` imports { invoke, safeInvoke, listen }.
// - `listen` is called at store-creation time ('install_progress') — a no-op.
// - `safeInvoke` is the whole surface `checkStatus` reads.
// The mock's `check_for_updates` return is swapped per test via
// `nextUpdateStatus`.
// ---------------------------------------------------------------------------

type FakeStatus = Record<string, unknown> | null;
let nextUpdateStatus: FakeStatus = null;
let installed = true;

const safeInvokeMock = vi.fn(
  async (cmd: string, _args?: Record<string, unknown>): Promise<unknown> => {
    switch (cmd) {
      case 'get_known_install_path':
        return '/install/root';
      case 'get_default_install_path':
        return '/install/root';
      case 'check_install_status':
        return installed;
      case 'get_installed_version':
        return '0.2.82';
      case 'check_for_updates':
        return nextUpdateStatus;
      default:
        return null;
    }
  },
);

vi.mock('$lib/tauri', () => ({
  invoke: vi.fn(),
  safeInvoke: (cmd: string, args?: Record<string, unknown>) =>
    safeInvokeMock(cmd, args),
  listen: vi.fn(async () => () => {}),
  tauriAvailable: () => true,
  isTauriRuntime: () => true,
}));

// A well-formed UpdateStatus with all required fields, parametrised on the
// remote-check health fields.
function status(
  health: { remote_check_ok?: boolean; remote_check_error?: string | null } = {},
): FakeStatus {
  return {
    remote_ahead: false,
    install_stale: false,
    binary_stale: false,
    source_version: '0.2.82',
    installed_version: '0.2.82',
    running_version: '0.2.82',
    on_disk_binary_version: '0.2.82',
    ...health,
  };
}

let orchestrator: typeof import('./orchestrator').orchestrator;
let cancelScheduledRetry: typeof import('./orchestrator').cancelScheduledRetry;

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  safeInvokeMock.mockClear();
  nextUpdateStatus = null;
  installed = true;
  const mod = await import('./orchestrator');
  orchestrator = mod.orchestrator;
  cancelScheduledRetry = mod.cancelScheduledRetry;
});

afterEach(() => {
  cancelScheduledRetry();
  vi.useRealTimers();
});

function checkForUpdatesCalls(): number {
  return safeInvokeMock.mock.calls.filter((c) => c[0] === 'check_for_updates')
    .length;
}

describe('orchestrator retry scheduling (D3)', () => {
  it('schedules a retry when remote_check_ok === false', async () => {
    nextUpdateStatus = status({ remote_check_ok: false, remote_check_error: 'fetch failed' });

    await orchestrator.checkStatus();
    expect(checkForUpdatesCalls()).toBe(1);

    // First retry fires at 30s.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(checkForUpdatesCalls()).toBe(2);
  });

  it('does NOT schedule a retry when remote_check_ok === true', async () => {
    nextUpdateStatus = status({ remote_check_ok: true });

    await orchestrator.checkStatus();
    expect(checkForUpdatesCalls()).toBe(1);

    // No pending timer ⇒ advancing the clock triggers no further check.
    await vi.advanceTimersByTimeAsync(600_000);
    expect(checkForUpdatesCalls()).toBe(1);
  });

  it('treats a MISSING remote_check_ok as healthy (older Rust — no retry)', async () => {
    // No remote_check_ok / remote_check_error at all (pre-v0.2.83 binary).
    nextUpdateStatus = status();

    await orchestrator.checkStatus();
    expect(checkForUpdatesCalls()).toBe(1);

    await vi.advanceTimersByTimeAsync(600_000);
    expect(checkForUpdatesCalls()).toBe(1);
  });

  it('walks the 30s / 90s / 300s ladder and caps at 3 retries per episode', async () => {
    // Remote stays unreachable across every attempt.
    nextUpdateStatus = status({ remote_check_ok: false, remote_check_error: 'net down' });

    await orchestrator.checkStatus(); // initial (1)
    expect(checkForUpdatesCalls()).toBe(1);

    await vi.advanceTimersByTimeAsync(30_000); // retry 1 (2)
    expect(checkForUpdatesCalls()).toBe(2);

    await vi.advanceTimersByTimeAsync(90_000); // retry 2 (3)
    expect(checkForUpdatesCalls()).toBe(3);

    await vi.advanceTimersByTimeAsync(300_000); // retry 3 (4)
    expect(checkForUpdatesCalls()).toBe(4);

    // Cap reached: no 4th retry, no matter how long we wait.
    await vi.advanceTimersByTimeAsync(10 * 300_000);
    expect(checkForUpdatesCalls()).toBe(4);
  });

  it('does NOT stack timers (single-flight): back-to-back failing checks arm ONE timer at the FIRST tier', async () => {
    nextUpdateStatus = status({ remote_check_ok: false });

    // Two failing checks in a row while a timer is already pending. Without
    // the single-flight guard the 2nd check would (a) advance retryAttempt to
    // the 90s tier and (b) leak a second timer — so we'd see the retry fire
    // at 90s, or twice.
    await orchestrator.checkStatus(); // arms the 30s timer (retryAttempt→1)
    await orchestrator.checkStatus(); // single-flight: must be a no-op

    expect(checkForUpdatesCalls()).toBe(2); // the two explicit checks

    // The single pending timer must be at the FIRST tier (30s). After it
    // fires (and the retry recovers), stop the episode so any LEAKED timer
    // would betray itself.
    nextUpdateStatus = status({ remote_check_ok: true }); // retry recovers
    await vi.advanceTimersByTimeAsync(30_000);
    expect(checkForUpdatesCalls()).toBe(3); // exactly ONE retry fired at 30s

    // The recovered retry reset the episode. If a second timer had been
    // leaked at the 90s tier (guard removed), it would fire here → 4.
    await vi.advanceTimersByTimeAsync(600_000);
    expect(checkForUpdatesCalls()).toBe(3);
  });

  it('resets the episode on a subsequent healthy check (ok=true clears the ladder)', async () => {
    nextUpdateStatus = status({ remote_check_ok: false });
    await orchestrator.checkStatus(); // (1) fail → arm 30s

    // The remote recovers before the first retry fires: a manual healthy
    // check resets the counter.
    nextUpdateStatus = status({ remote_check_ok: true });
    await orchestrator.checkStatus(); // (2) ok → cancelScheduledRetry()

    // The previously-armed 30s timer was cleared → no retry.
    await vi.advanceTimersByTimeAsync(600_000);
    expect(checkForUpdatesCalls()).toBe(2);

    // And a NEW failure episode starts fresh at 30s (counter was reset).
    nextUpdateStatus = status({ remote_check_ok: false });
    await orchestrator.checkStatus(); // (3) fail → arm 30s again
    await vi.advanceTimersByTimeAsync(30_000);
    expect(checkForUpdatesCalls()).toBe(4);
  });

  it('a null updateStatus (command soft-failed) is treated as a failed check → retry', async () => {
    nextUpdateStatus = null;

    await orchestrator.checkStatus();
    expect(checkForUpdatesCalls()).toBe(1);

    await vi.advanceTimersByTimeAsync(30_000);
    expect(checkForUpdatesCalls()).toBe(2);
  });

  it('cancelScheduledRetry() clears a pending timer and resets the counter', async () => {
    nextUpdateStatus = status({ remote_check_ok: false });
    await orchestrator.checkStatus(); // arm 30s

    cancelScheduledRetry();

    await vi.advanceTimersByTimeAsync(600_000);
    expect(checkForUpdatesCalls()).toBe(1); // the retry never fired
  });
});

// ---------------------------------------------------------------------------
// N-4 (v0.2.83): the store tracks an EXPLICIT lastCheckFailed outcome so the
// amber "couldn't check" badge can render even when updateStatus is null (the
// case the old derivation silently dropped).
// ---------------------------------------------------------------------------
describe('orchestrator lastCheckFailed tracking (N-4)', () => {
  it('is null before the first completed check (no amber flash at startup)', async () => {
    // Fresh store, no checkStatus() yet.
    expect(get(orchestrator).lastCheckFailed).toBeNull();
  });

  it('is true after a completed check with a NULL updateStatus (command soft-failed)', async () => {
    nextUpdateStatus = null; // check_for_updates soft-failed to null

    await orchestrator.checkStatus();

    // The completed check FAILED — amber must be derivable even though
    // updateStatus is null (the exact gap N-4 closes).
    expect(get(orchestrator).lastCheckFailed).toBe(true);
    expect(get(orchestrator).updateStatus).toBeNull();
  });

  it('is true after remote_check_ok === false', async () => {
    nextUpdateStatus = status({ remote_check_ok: false, remote_check_error: 'fetch failed' });

    await orchestrator.checkStatus();

    expect(get(orchestrator).lastCheckFailed).toBe(true);
  });

  it('is false after a successful check (remote_check_ok === true)', async () => {
    nextUpdateStatus = status({ remote_check_ok: true });

    await orchestrator.checkStatus();

    expect(get(orchestrator).lastCheckFailed).toBe(false);
  });

  it('is false after a MISSING remote_check_ok (older Rust — treated healthy)', async () => {
    nextUpdateStatus = status(); // no health fields

    await orchestrator.checkStatus();

    expect(get(orchestrator).lastCheckFailed).toBe(false);
  });

  it('is false when not installed (no remote to check → no amber)', async () => {
    installed = false;

    await orchestrator.checkStatus();

    expect(get(orchestrator).status).toBe('not_installed');
    expect(get(orchestrator).lastCheckFailed).toBe(false);
  });

  it('flips false→true→false as the remote check recovers then fails then recovers', async () => {
    nextUpdateStatus = status({ remote_check_ok: true });
    await orchestrator.checkStatus();
    expect(get(orchestrator).lastCheckFailed).toBe(false);

    nextUpdateStatus = status({ remote_check_ok: false });
    await orchestrator.checkStatus();
    expect(get(orchestrator).lastCheckFailed).toBe(true);

    nextUpdateStatus = status({ remote_check_ok: true });
    await orchestrator.checkStatus();
    expect(get(orchestrator).lastCheckFailed).toBe(false);
  });
});
