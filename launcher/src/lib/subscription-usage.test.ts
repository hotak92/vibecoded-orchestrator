// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.97 — the home-page subscription-usage card's presentation rules.
// The one that matters most: an unknown window is never drawn as a bar.
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ invoke: vi.fn(), tauriAvailable: vi.fn() }));

import { invoke, tauriAvailable } from '$lib/tauri';
import {
  barTone,
  barWidth,
  cardVisible,
  describeCountdown,
  describeTokens,
  fetchUsage,
  INITIAL_CARD_STATE,
  nextPollMs,
  settle,
  unknownLabel,
  USAGE_POLL_MS,
  USAGE_RETRY_WHILE_REFRESHING_MS,
  vendorStatus,
  visibleVendors,
  type UsageBridgeResult,
  type UsageSnapshot,
  type UsageVendor,
} from './subscription-usage';

function vendor(over: Partial<UsageVendor> = {}): UsageVendor {
  return {
    id: 'anthropic',
    label: 'Claude',
    name: 'Claude subscription',
    state: 'ok',
    problem: null,
    plan: null,
    windows: [],
    ...over,
  };
}

function snapshot(vendors: UsageVendor[], refreshing = false): UsageSnapshot {
  return {
    generated_at: '2026-09-23T04:00:00Z',
    last_refresh_at: null,
    refreshing,
    refresh_interval_s: 240,
    vendors,
  };
}

describe('cardVisible', () => {
  it('is quiet only in browser mode and when the gateway is not running', () => {
    expect(cardVisible(null)).toBe(false);
    expect(cardVisible({ ok: false, reason: 'not_running', message: 'm' })).toBe(false);
  });

  it('shows data AND every failure the user can act on — a broken install above all', () => {
    expect(cardVisible({ ok: true, port: 11460, snapshot: snapshot([]) })).toBe(true);
    for (const reason of [
      'broken_install',
      'bridge_error',
      'no_token',
      'outdated_gateway',
      'unreachable',
    ]) {
      expect(cardVisible({ ok: false, reason, message: 'm' }), reason).toBe(true);
    }
  });
});

describe('settle (review F4: a transient failure must not blank a good card)', () => {
  const good: UsageBridgeResult = { ok: true, port: 11460, snapshot: snapshot([vendor()]) };
  const withGood = settle(INITIAL_CARD_STATE, good);

  it('keeps the last good reading through a transient failure, and says so', () => {
    for (const reason of ['bridge_error', 'unreachable']) {
      const next = settle(withGood, { ok: false, reason, message: 'timed out after 20 s' });
      expect(next.result).toBe(good);
      expect(next.warning).toBe('not refreshed — timed out after 20 s');
    }
  });

  it('never hides a broken install behind an old reading', () => {
    const broken = { ok: false as const, reason: 'broken_install', message: 're-run install.py' };
    expect(settle(withGood, broken)).toEqual({ result: broken, warning: null });
  });

  it('shows a transient failure when there is nothing good to keep', () => {
    const err = { ok: false as const, reason: 'bridge_error', message: 'no interpreter' };
    expect(settle(INITIAL_CARD_STATE, err)).toEqual({ result: err, warning: null });
  });

  it('a fresh answer clears the warning; browser mode clears everything', () => {
    const warned = settle(withGood, { ok: false, reason: 'unreachable', message: 'm' });
    expect(settle(warned, good)).toEqual({ result: good, warning: null });
    expect(settle(warned, null)).toEqual(INITIAL_CARD_STATE);
    const gone = { ok: false as const, reason: 'not_running', message: 'm' };
    expect(settle(withGood, gone).result).toBe(gone);
  });
});

describe('bars', () => {
  it('tones by headroom', () => {
    expect(barTone(12)).toBe('teal');
    expect(barTone(70)).toBe('purple');
    expect(barTone(90)).toBe('pink');
  });

  it('clamps to the track', () => {
    expect(barWidth(-3)).toBe('0%');
    expect(barWidth(31)).toBe('31%');
    expect(barWidth(140)).toBe('100%');
  });

  it('labels an unknown window with its reason instead of a bar', () => {
    expect(unknownLabel('stale')).toBe('unknown — last reading is too old');
    expect(unknownLabel('reset_passed')).toContain('reset');
    expect(unknownLabel(null)).toBe('unknown — no reading');
  });
});

describe('describeCountdown', () => {
  const now = Date.parse('2026-09-23T04:00:00Z');
  it('counts down in the coarsest useful units', () => {
    expect(describeCountdown('2026-09-23T06:14:30Z', now)).toBe('resets in 2h 14m');
    expect(describeCountdown('2026-09-26T08:00:00Z', now)).toBe('resets in 3d 4h');
    expect(describeCountdown('2026-09-23T04:12:00Z', now)).toBe('resets in 12m');
  });
  it('never shows a negative countdown', () => {
    expect(describeCountdown('2026-09-23T03:00:00Z', now)).toBe('resetting now');
  });
  it('says nothing for an absent or garbled reset', () => {
    expect(describeCountdown(null, now)).toBe('');
    expect(describeCountdown('not a date', now)).toBe('');
  });
});

describe('tokens (no quota endpoint)', () => {
  const tokens = {
    tokens: 1234567,
    requests: 12,
    unit: 'tokens' as const,
    period: 'month' as const,
    period_start: '2026-09-01T00:00:00Z',
    counted_since: null,
    source: 'gateway_ledger',
    fetched_at: '2026-09-23T04:00:00Z',
  };
  it('is labelled as tokens, never as a percentage', () => {
    const text = describeTokens(tokens, 'en-US');
    expect(text).toBe('1,234,567 tokens this month');
    expect(text).not.toContain('%');
  });
  it('says when the ledger does not reach the month start', () => {
    expect(describeTokens({ ...tokens, counted_since: '2026-09-10T12:00:00Z' }, 'en-US')).toMatch(
      /^1,234,567 tokens since Sep 10$/,
    );
  });
});

describe('vendor rows', () => {
  it('states what a vendor without numbers is waiting on', () => {
    expect(vendorStatus(vendor())).toBeNull();
    expect(vendorStatus(vendor({ state: 'pending' }))).toBe('reading…');
    expect(vendorStatus(vendor({ state: 'unconfigured', problem: 'no key resolved for zai' }))).toBe(
      'no key resolved for zai',
    );
    expect(vendorStatus(vendor({ state: 'unknown', problem: null }))).toBe('unknown');
  });

  it('drops only a vendor with nothing to show', () => {
    const empty = vendor({ id: 'qwen' });
    const pending = vendor({ id: 'zai', state: 'pending' });
    const withWindow = vendor({
      windows: [
        {
          id: '5h', label: '5h', kind: 'percent', percent: null, resets_at: null,
          source: 'oauth_usage', fetched_at: '', unknown_reason: 'stale',
        },
      ],
    });
    expect(visibleVendors(snapshot([empty, pending, withWindow])).map((v) => v.id)).toEqual([
      'zai',
      'anthropic',
    ]);
  });
});

describe('polling', () => {
  it('re-reads soon while the gateway is still fetching, else at the normal pace', () => {
    expect(nextPollMs({ ok: true, port: 1, snapshot: snapshot([], true) })).toBe(
      USAGE_RETRY_WHILE_REFRESHING_MS,
    );
    expect(nextPollMs({ ok: true, port: 1, snapshot: snapshot([]) })).toBe(USAGE_POLL_MS);
    expect(nextPollMs(null)).toBe(USAGE_POLL_MS);
  });

});

describe('fetchUsage (review F4/F15: the bridge error is surfaced, not swallowed)', () => {
  beforeEach(() => {
    vi.mocked(invoke).mockReset();
    vi.mocked(tauriAvailable).mockReset();
  });

  it('turns a rejected command into a visible bridge_error naming the cause', async () => {
    vi.mocked(tauriAvailable).mockReturnValue(true);
    vi.mocked(invoke).mockRejectedValueOnce(
      'vco_lib.gateway_usage exited 1 and did not return JSON: ModuleNotFoundError',
    );
    const result = await fetchUsage();
    expect(invoke).toHaveBeenCalledWith('model_gateway_usage_windows');
    expect(result).toEqual({
      ok: false,
      reason: 'bridge_error',
      message:
        'could not read subscription usage: vco_lib.gateway_usage exited 1 and did not ' +
        'return JSON: ModuleNotFoundError',
    });
    expect(cardVisible(result)).toBe(true);
  });

  it('reads an Error rejection by its message', async () => {
    vi.mocked(tauriAvailable).mockReturnValue(true);
    vi.mocked(invoke).mockRejectedValueOnce(new Error('usage-windows task failed: panicked'));
    const result = await fetchUsage();
    expect(result?.ok).toBe(false);
    expect(result && !result.ok && result.message).toContain('usage-windows task failed');
  });

  it('asks nothing in browser mode, and that is the only null', async () => {
    vi.mocked(tauriAvailable).mockReturnValue(false);
    await expect(fetchUsage()).resolves.toBeNull();
    expect(invoke).not.toHaveBeenCalled();
  });
});
