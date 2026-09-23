// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.97 — the post-update gateway-restart modal. Every branch of the one
// destructive decision, in both directions: Continue reaches the restart
// command; Dismiss, a current gateway, an unprovable verdict, a gateway that
// is not running and a failed check reach NOTHING but the read-only check.
import { describe, expect, it, vi } from 'vitest';
import {
  canContinue,
  createFreshnessController,
  DISMISS_STORAGE_KEY,
  INITIAL_FRESHNESS_STATE,
  MODAL_MESSAGE,
  promptIdentity,
  shouldOfferRestart,
  type FreshnessState,
  type GatewayFreshnessReport,
  type KeyValueStore,
} from './gateway-freshness';

function report(over: Partial<GatewayFreshnessReport> = {}): GatewayFreshnessReport {
  return {
    verdict: 'stale',
    summary: 'model gateway: running 0.2.96, the checkout is 0.2.97.',
    running_version: '0.2.96',
    checkout_version: '0.2.97',
    served_sha: null,
    expected_sha: 'abc',
    pid: 42,
    port: 11460,
    prompt: true,
    restart: { mechanism: 'boot_service', possible: true, reason: 'restart the unit' },
    ...over,
  };
}

function memoryStore(): KeyValueStore & { data: Map<string, string> } {
  const data = new Map<string, string>();
  return {
    data,
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
  };
}

function harness(check: GatewayFreshnessReport | Error, restart?: unknown) {
  let state: FreshnessState = INITIAL_FRESHNESS_STATE;
  const calls: string[] = [];
  const invoke = vi.fn(async (cmd: string) => {
    calls.push(cmd);
    if (cmd === 'model_gateway_freshness') {
      if (check instanceof Error) throw check;
      return check;
    }
    if (cmd === 'model_gateway_restart_stale') {
      return restart ?? { outcome: 'restarted', restarted: true, message: 'done' };
    }
    throw new Error(`unexpected ${cmd}`);
  });
  const storage = memoryStore();
  const ctl = createFreshnessController({
    invoke: invoke as never,
    storage,
    set: (s) => (state = s),
    get: () => state,
  });
  return { ctl, calls, storage, state: () => state };
}

describe('gateway freshness modal (v0.2.97)', () => {
  it('uses the owner-specified wording', () => {
    expect(MODAL_MESSAGE).toMatch(/needs restarting to use the updated version/);
    expect(MODAL_MESSAGE).toMatch(/no agent is running/);
  });

  it('offers only for a proven-stale gateway', () => {
    expect(shouldOfferRestart(report(), null)).toBe(true);
    for (const verdict of ['current', 'unknown', 'not_running']) {
      expect(shouldOfferRestart(report({ verdict, prompt: false }), null)).toBe(false);
    }
    expect(shouldOfferRestart(null, null)).toBe(false);
    expect(shouldOfferRestart(undefined, null)).toBe(false);
  });

  it('a dismissal holds for the same situation only', () => {
    const r = report();
    expect(shouldOfferRestart(r, promptIdentity(r))).toBe(false);
    // A new update (new checkout digest) or a restarted gateway asks again.
    expect(shouldOfferRestart(report({ expected_sha: 'def' }), promptIdentity(r))).toBe(true);
    expect(shouldOfferRestart(report({ pid: 43 }), promptIdentity(r))).toBe(true);
  });

  it('offers Continue only when something owns the process', () => {
    expect(canContinue(report())).toBe(true);
    expect(canContinue(report({ restart: { mechanism: 'none', possible: false, reason: 'x' } }))).toBe(false);
    expect(canContinue(report({ restart: null }))).toBe(false);
    expect(canContinue(report({ prompt: false }))).toBe(false);
  });

  it('Continue restarts the gateway', async () => {
    const h = harness(report());
    await h.ctl.check();
    expect(h.state().open).toBe(true);
    await h.ctl.continueRestart();
    expect(h.calls).toEqual(['model_gateway_freshness', 'model_gateway_restart_stale']);
    expect(h.state().result?.restarted).toBe(true);
    expect(h.state().restarting).toBe(false);
  });

  it('Dismiss restarts nothing and remembers the situation', async () => {
    const h = harness(report());
    await h.ctl.check();
    h.ctl.dismiss();
    expect(h.calls).toEqual(['model_gateway_freshness']);
    expect(h.state().open).toBe(false);
    expect(h.storage.data.get(DISMISS_STORAGE_KEY)).toBe(promptIdentity(report()));
    // The next check (e.g. next launcher start) stays silent.
    await h.ctl.check();
    expect(h.state().open).toBe(false);
  });

  it('a second close after a restart does not become a dismissal', async () => {
    const h = harness(report(), { outcome: 'unverified', restarted: false, message: 'm' });
    await h.ctl.check();
    await h.ctl.continueRestart();
    h.ctl.dismiss(); // "Close"
    h.ctl.dismiss(); // DialogRoot reporting its own close
    expect(h.storage.data.has(DISMISS_STORAGE_KEY)).toBe(false);
  });

  it.each([
    ['current', report({ verdict: 'current', prompt: false })],
    ['unknown', report({ verdict: 'unknown', prompt: false })],
    ['not running', report({ verdict: 'not_running', prompt: false, pid: null })],
  ])('%s: no modal, and Continue cannot reach the restart', async (_name, r) => {
    const h = harness(r);
    await h.ctl.check();
    expect(h.state().open).toBe(false);
    await h.ctl.continueRestart();
    expect(h.calls).toEqual(['model_gateway_freshness']);
  });

  it('a failed check shows nothing', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const h = harness(new Error('no python'));
    await h.ctl.check();
    expect(h.state().open).toBe(false);
    await h.ctl.continueRestart();
    expect(h.calls).toEqual(['model_gateway_freshness']);
    warn.mockRestore();
  });

  it('a stale gateway nothing owns is shown, but Continue does nothing', async () => {
    const h = harness(report({ restart: { mechanism: 'none', possible: false, reason: 'not registered' } }));
    await h.ctl.check();
    expect(h.state().open).toBe(true);
    await h.ctl.continueRestart();
    expect(h.calls).toEqual(['model_gateway_freshness']);
  });

  it('a restart error is shown, not swallowed', async () => {
    let state: FreshnessState = { ...INITIAL_FRESHNESS_STATE, report: report(), open: true };
    const ctl = createFreshnessController({
      invoke: (async () => {
        throw new Error('boom');
      }) as never,
      storage: null,
      set: (s) => (state = s),
      get: () => state,
    });
    await ctl.continueRestart();
    expect(state.error).toMatch(/boom/);
    expect(state.restarting).toBe(false);
    expect(state.open).toBe(true);
  });
});
