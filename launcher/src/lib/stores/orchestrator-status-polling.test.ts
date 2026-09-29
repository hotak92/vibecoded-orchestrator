// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.100 (WP-08, L3-F09 + L3-F13): the status polling.
//
// Contract under test:
//   - `startStatusPolling()` checks immediately and arms an hourly tick; the
//     teardown clears it;
//   - the hourly tick honours "Check for updates automatically"
//     (`get_auto_check_enabled`): OFF ⇒ no check; ON or unreadable ⇒ check;
//   - the launch-time check is NOT gated by the preference.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

let autoCheck: boolean | null = true;
let calls: string[] = [];

vi.mock('$lib/tauri', () => ({
  invoke: async () => undefined,
  safeInvoke: async (cmd: string) => {
    calls.push(cmd);
    if (cmd === 'get_auto_check_enabled') return autoCheck;
    // checkStatus's first probe; returning null ends it early (recorded).
    return null;
  },
  listen: async () => () => {},
  tauriAvailable: () => true,
  isTauriRuntime: () => false,
}));

type Orch = typeof import('./orchestrator');
let O: Orch;

beforeEach(async () => {
  vi.resetModules();
  vi.useFakeTimers();
  autoCheck = true;
  calls = [];
  O = await import('./orchestrator');
});

afterEach(() => {
  vi.useRealTimers();
});

const checks = () => calls.filter((c) => c === 'get_known_install_path').length;

describe('startStatusPolling', () => {
  it('checks at once, then hourly while auto-check is ON', async () => {
    const stop = O.startStatusPolling();
    await vi.advanceTimersByTimeAsync(0);
    expect(checks()).toBe(1);
    await vi.advanceTimersByTimeAsync(O.STATUS_POLL_INTERVAL_MS);
    expect(checks()).toBe(2);
    await vi.advanceTimersByTimeAsync(O.STATUS_POLL_INTERVAL_MS);
    expect(checks()).toBe(3);
    stop();
    await vi.advanceTimersByTimeAsync(O.STATUS_POLL_INTERVAL_MS * 3);
    expect(checks()).toBe(3);
  });

  it('the hourly tick is SKIPPED while auto-check is OFF; the launch check is not', async () => {
    autoCheck = false;
    const stop = O.startStatusPolling();
    await vi.advanceTimersByTimeAsync(0);
    expect(checks()).toBe(1); // launch-time check always runs
    await vi.advanceTimersByTimeAsync(O.STATUS_POLL_INTERVAL_MS * 2);
    expect(checks()).toBe(1);
    expect(calls.filter((c) => c === 'get_auto_check_enabled')).toHaveLength(2);
    // Turning it back on takes effect at the next tick, no restart needed.
    autoCheck = true;
    await vi.advanceTimersByTimeAsync(O.STATUS_POLL_INTERVAL_MS);
    expect(checks()).toBe(2);
    stop();
  });

  it('an unreadable preference counts as ON (never silently stops checking)', async () => {
    autoCheck = null;
    await O.scheduledStatusCheck();
    expect(checks()).toBe(1);
  });
});
