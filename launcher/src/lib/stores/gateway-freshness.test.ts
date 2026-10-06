// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.101 (P299-A2) — the store shell's `offer()` passthrough. The
// controller's decisions are pinned in `$lib/gateway-freshness.test.ts`;
// this file only proves the shell the GUI surfaces actually call exposes
// offer(), routes it through the Tauri bridge, and stays a no-op in browser
// mode (same mocking shape as `subscription-usage.test.ts`).
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn(), tauriAvailable: vi.fn() }));

import { get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import { gatewayFreshness } from './gateway-freshness';
import type { GatewayFreshnessReport } from '$lib/gateway-freshness';

const stale: GatewayFreshnessReport = {
  verdict: 'stale',
  summary: 'model gateway: running 0.2.100, the checkout is 0.2.101.',
  running_version: '0.2.100',
  checkout_version: '0.2.101',
  served_sha: null,
  expected_sha: 'abc',
  pid: 42,
  port: 11460,
  prompt: true,
  restart: { mechanism: 'boot_service', possible: true, reason: 'restart the unit' },
};

describe('gatewayFreshness store shell — offer() (v0.2.101, P299-A2)', () => {
  beforeEach(() => {
    vi.mocked(invoke).mockReset();
    vi.mocked(tauriAvailable).mockReset();
    // The store is a module singleton: close anything a previous test opened.
    gatewayFreshness.dismiss();
  });

  it('exposes offer(): asks the backend and opens the modal for a proven-stale gateway', async () => {
    vi.mocked(tauriAvailable).mockReturnValue(true);
    vi.mocked(invoke).mockResolvedValueOnce(stale);
    const report = await gatewayFreshness.offer();
    expect(invoke).toHaveBeenCalledWith('model_gateway_freshness');
    expect(report?.verdict).toBe('stale');
    const state = get(gatewayFreshness);
    expect(state.open).toBe(true);
    expect(state.report).toEqual(stale);
    // The shell must not smuggle in a restart: only the freshness command ran.
    expect(vi.mocked(invoke)).toHaveBeenCalledTimes(1);
  });

  it('is a no-op in browser mode: no gateway to ask about', async () => {
    vi.mocked(tauriAvailable).mockReturnValue(false);
    await expect(gatewayFreshness.offer()).resolves.toBeNull();
    expect(invoke).not.toHaveBeenCalled();
    expect(get(gatewayFreshness).open).toBe(false);
  });
});
