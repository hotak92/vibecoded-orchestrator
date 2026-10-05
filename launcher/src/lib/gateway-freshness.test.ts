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
  offerNote,
  promptIdentity,
  scheduleStartupCheck,
  shouldOfferRestart,
  STARTUP_CHECK_DELAY_MS,
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

  // ── review R1 F3: overlapping checks must never re-arm Continue ─────────

  /** A controller whose backend calls resolve only when the test says so. */
  function deferredHarness() {
    let state: FreshnessState = INITIAL_FRESHNESS_STATE;
    const calls: string[] = [];
    const pending: Array<{ cmd: string; resolve: (v: unknown) => void }> = [];
    const invoke = (cmd: string) =>
      new Promise((resolve) => {
        calls.push(cmd);
        pending.push({ cmd, resolve });
      });
    const ctl = createFreshnessController({
      invoke: invoke as never,
      storage: memoryStore(),
      set: (s) => (state = s),
      get: () => state,
    });
    const settle = (i: number, value: unknown) => pending[i].resolve(value);
    return { ctl, calls, settle, state: () => state };
  }

  const flush = () => new Promise((r) => setTimeout(r, 0));

  it('two overlapping checks share one backend call', async () => {
    const h = deferredHarness();
    const a = h.ctl.check();
    const b = h.ctl.check();
    expect(h.calls).toEqual(['model_gateway_freshness']);
    h.settle(0, report());
    await Promise.all([a, b]);
    expect(h.state().open).toBe(true);
  });

  it('a check that lands mid-restart does not re-enable Continue (no double restart)', async () => {
    // The reviewer's interleaving: A (launcher-start timer) and B (an update
    // that finished meanwhile) are BOTH in flight; A lands, the user presses
    // Continue, then B lands while the restart is running.
    const h = deferredHarness();
    const a = h.ctl.check();
    const b = h.ctl.check();
    const freshIdx = () =>
      h.calls.map((c, i) => (c === 'model_gateway_freshness' ? i : -1)).filter((i) => i >= 0);
    h.settle(freshIdx()[0], report());
    await a;
    expect(h.state().open).toBe(true);
    const restart = h.ctl.continueRestart();
    await flush();
    expect(h.state().restarting).toBe(true);
    // B's answer (still stale — the old process is not replaced yet), if B
    // ever reached the backend on its own.
    if (freshIdx().length > 1) h.settle(freshIdx()[1], report());
    await b;
    expect(h.state().restarting).toBe(true);
    // A second Continue while the first is in flight reaches nothing.
    await h.ctl.continueRestart();
    expect(h.calls.filter((c) => c === 'model_gateway_restart_stale')).toHaveLength(1);
    // Finish the restart; a later check must not wipe the result either.
    h.settle(h.calls.indexOf('model_gateway_restart_stale'), {
      outcome: 'restarted', restarted: true, message: 'done',
    });
    await restart;
    await h.ctl.check();
    expect(h.state().result?.restarted).toBe(true);
    expect(freshIdx()).toHaveLength(1);
    expect(h.calls.filter((x) => x === 'model_gateway_restart_stale')).toHaveLength(1);
  });

  it('an answer that lands after the state moved on is dropped', async () => {
    let state: FreshnessState = INITIAL_FRESHNESS_STATE;
    let resolveCheck: (v: unknown) => void = () => {};
    const ctl = createFreshnessController({
      invoke: (() => new Promise((r) => (resolveCheck = r))) as never,
      storage: memoryStore(),
      set: (s) => (state = s),
      get: () => state,
    });
    const pending = ctl.check();
    // Meanwhile a restart got under way (another writer of the same store).
    const busy: FreshnessState = { ...INITIAL_FRESHNESS_STATE, report: report(), open: true, restarting: true };
    state = busy;
    resolveCheck(report({ pid: 7 }));
    await pending;
    expect(state).toBe(busy);
  });

  it('a check while the modal is up neither asks nor replaces it', async () => {
    const h = deferredHarness();
    const first = h.ctl.check();
    h.settle(0, report());
    await first;
    h.ctl.dismiss(); // closed, but remembered
    // Re-open by a fresh situation, then a slow check lands with a stale reply.
    const second = h.ctl.check();
    h.settle(1, report({ pid: 99 }));
    await second;
    expect(h.state().open).toBe(true);
    const snapshot = h.state();
    const late = h.ctl.check(); // busy: must not even ask
    await late;
    expect(h.calls.filter((c) => c === 'model_gateway_freshness')).toHaveLength(2);
    expect(h.state()).toBe(snapshot);
  });

  // ── review R1 F15: the startup timer is cancelled on teardown ────────────

  it('the startup check fires once after the delay', () => {
    vi.useFakeTimers();
    try {
      const check = vi.fn();
      scheduleStartupCheck(check);
      vi.advanceTimersByTime(STARTUP_CHECK_DELAY_MS - 1);
      expect(check).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(check).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a cancelled startup check never fires (layout teardown)', () => {
    vi.useFakeTimers();
    try {
      const check = vi.fn();
      const cancel = scheduleStartupCheck(check);
      cancel();
      vi.advanceTimersByTime(STARTUP_CHECK_DELAY_MS * 10);
      expect(check).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
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

// ── v0.2.101 (P299-A2): the user-initiated ask ────────────────────────────
// Before offer(): a single Dismiss suppressed the prompt for this exact
// situation forever, the usage card's `outdated_gateway` had no affordance,
// and a failed check lived in the console only.

describe('offer() — the user-initiated ask (v0.2.101, P299-A2)', () => {
  it('opens the modal for a proven-stale gateway EVEN when the exact situation was dismissed', async () => {
    const h = harness(report());
    // A stored dismissal for THIS identity keeps the automatic check silent…
    h.storage.data.set(DISMISS_STORAGE_KEY, promptIdentity(report()));
    await h.ctl.check();
    expect(h.state().open).toBe(false);
    // …but the user asking again IS the point of offer(): it ignores it.
    // (Red-proof mutation: make offer() consult DISMISS_STORAGE_KEY — e.g.
    // gate the open on shouldOfferRestart(report, readDismissed(storage)) —
    // and the open assertion below fails.)
    const got = await h.ctl.offer();
    expect(got?.prompt).toBe(true);
    expect(h.calls).toEqual(['model_gateway_freshness', 'model_gateway_freshness']);
    expect(h.state().open).toBe(true);
    expect(h.state().report?.pid).toBe(42);
    // offer() leaves the stored dismissal alone; the next Dismiss re-stores it.
    expect(h.storage.data.get(DISMISS_STORAGE_KEY)).toBe(promptIdentity(report()));
  });

  it("never restarts without the modal's Continue — offer only opens", async () => {
    const h = harness(report());
    await h.ctl.offer();
    // (Red-proof mutation: call continueRestart() from inside offer() and the
    // single-command assertion below fails with a restart nobody confirmed.)
    expect(h.calls).toEqual(['model_gateway_freshness']);
    expect(h.state().open).toBe(true);
    expect(h.state().restarting).toBe(false);
    expect(h.state().result).toBeNull();
    // Continue, pressed by the user, remains the ONLY restart path.
    await h.ctl.continueRestart();
    expect(h.calls).toEqual(['model_gateway_freshness', 'model_gateway_restart_stale']);
    expect(h.state().result?.restarted).toBe(true);
  });

  it('a non-stale verdict opens nothing but still reports what the backend said', async () => {
    for (const r of [
      report({ verdict: 'current', prompt: false }),
      report({ verdict: 'unknown', prompt: false }),
      report({ verdict: 'not_running', prompt: false, pid: null }),
    ]) {
      const h = harness(r);
      const got = await h.ctl.offer();
      expect(got?.verdict, r.verdict).toBe(r.verdict);
      expect(h.state().open).toBe(false);
      expect(h.state().report).toEqual(r);
      expect(h.calls).toEqual(['model_gateway_freshness']);
      await h.ctl.continueRestart();
      expect(h.calls, r.verdict).toEqual(['model_gateway_freshness']);
    }
  });

  it('a failed offer is a VISIBLE error state, not console-only', async () => {
    const h = harness(new Error('no python'));
    await expect(h.ctl.offer()).resolves.toBeNull();
    // (Red-proof mutation: drop the error from the failure set and both
    // assertions below fail — the click would be silently dead again.)
    expect(h.state().error).toMatch(/freshness check failed/);
    expect(h.state().error).toMatch(/no python/);
    expect(h.state().open).toBe(false);
    expect(h.calls).toEqual(['model_gateway_freshness']);
    // And the dead check cannot be turned into a restart.
    await h.ctl.continueRestart();
    expect(h.calls).toEqual(['model_gateway_freshness']);
  });

  it('a failed BACKGROUND check is recorded in error too (Services page renders it)', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const h = harness(new Error('no python'));
    await h.ctl.check();
    expect(h.state().open).toBe(false);
    expect(h.state().error).toMatch(/freshness check failed/);
    // Still no modal, still no restart path.
    expect(h.calls).toEqual(['model_gateway_freshness']);
    warn.mockRestore();
  });

  it('does not disturb a decision already under way', async () => {
    const h = harness(report());
    await h.ctl.check();
    expect(h.state().open).toBe(true);
    const got = await h.ctl.offer();
    expect(got?.prompt).toBe(true);
    // The modal in front of the user is untouched and the backend was asked once.
    expect(h.calls).toEqual(['model_gateway_freshness']);
    expect(h.state().open).toBe(true);
  });

  it('a FAILED offer never clobbers a decision that started while it was in flight', async () => {
    // Review nit 1: user clicks while the 4 s startup check is in flight;
    // the background check opens the modal; the offer's invoke then rejects.
    let state: FreshnessState = INITIAL_FRESHNESS_STATE;
    let rejectCheck: (e: unknown) => void = () => {};
    const calls: string[] = [];
    const ctl = createFreshnessController({
      invoke: ((cmd: string) => {
        calls.push(cmd);
        return new Promise((_resolve, reject) => (rejectCheck = reject));
      }) as never,
      storage: memoryStore(),
      set: (s) => (state = s),
      get: () => state,
    });
    const pending = ctl.offer();
    // Meanwhile the background check landed and opened the modal.
    const busy: FreshnessState = { ...INITIAL_FRESHNESS_STATE, report: report(), open: true };
    state = busy;
    rejectCheck(new Error('no python'));
    await expect(pending).resolves.toBeNull();
    // (Red-proof mutation: drop the catch path's decisionInProgress guard
    // and the failed ask resets the store — the assertion below fails with
    // the modal the user is reading closed by a check that failed.)
    expect(state).toBe(busy);
    expect(calls).toEqual(['model_gateway_freshness']);
  });
});

// ── review nit 2: the click is never a silent no-op ────────────────────────

describe('offerNote (v0.2.101 review nit 2)', () => {
  it('says why for a non-stale verdict', () => {
    const current = report({ verdict: 'current', prompt: false, summary: 'gateway is current' });
    expect(offerNote(current)).toBe('gateway is current');
    expect(offerNote(report({ verdict: 'not_running', prompt: false, pid: null }))).toBe(
      report({ verdict: 'not_running', prompt: false, pid: null }).summary,
    );
  });

  it('adds nothing when the modal opened or the check failed — those have their own answer', () => {
    expect(offerNote(report())).toBeNull(); // stale: the modal IS the answer
    expect(offerNote(null)).toBeNull(); // failed: the visible error state is
  });
});
