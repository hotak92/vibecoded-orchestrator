// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.97 — the home-page subscription-usage card's presentation rules.
// The one that matters most: an unknown window is never drawn as a bar.
import { describe, expect, it, vi } from 'vitest';

vi.mock('$lib/tauri', () => ({ safeInvoke: vi.fn() }));

import { safeInvoke } from '$lib/tauri';
import {
  barTone,
  barWidth,
  cardVisible,
  describeCountdown,
  describeTokens,
  fetchUsage,
  nextPollMs,
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
  it('hides the card where the gateway is not running or never ran', () => {
    expect(cardVisible(null)).toBe(false);
    expect(cardVisible({ ok: false, reason: 'not_running', message: 'm' })).toBe(false);
    expect(cardVisible({ ok: false, reason: 'no_token', message: 'm' })).toBe(false);
  });

  it('shows the card for data AND for a failure the user can act on', () => {
    expect(cardVisible({ ok: true, port: 11460, snapshot: snapshot([]) })).toBe(true);
    expect(cardVisible({ ok: false, reason: 'outdated_gateway', message: 'restart it' })).toBe(true);
    expect(cardVisible({ ok: false, reason: 'unreachable', message: 'm' })).toBe(true);
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

  it('asks the one backend command', async () => {
    const answer: UsageBridgeResult = { ok: false, reason: 'not_running', message: 'm' };
    vi.mocked(safeInvoke).mockResolvedValueOnce(answer);
    await expect(fetchUsage()).resolves.toBe(answer);
    expect(safeInvoke).toHaveBeenCalledWith('model_gateway_usage_windows');
  });
});
