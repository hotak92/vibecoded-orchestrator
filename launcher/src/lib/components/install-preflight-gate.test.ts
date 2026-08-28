// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (P2-B7) — tests for the preflight modal's deferred-proceed gate.
//
// The bug these pin: "Detect again" succeeded, the modal showed
// "Detected podman — proceeding…" for 350 ms, and during that pause the
// user could still cancel — via the re-enabled Cancel button, Escape or
// the backdrop. `cancel()` ran `onCancel()`, and then the pending timer
// fired `onProceed()` anyway. The install the user cancelled proceeded.

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import {
  cancelProceed,
  newProceedGate,
  preflightBusy,
  scheduleProceed,
  PROCEED_DELAY_MS,
} from './install-preflight-gate';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('scheduleProceed', () => {
  it('fires the proceed handler after the cosmetic pause', () => {
    const gate = newProceedGate();
    const calls: string[] = [];

    scheduleProceed(gate, () => calls.push('proceed'));
    expect(calls).toEqual([]);
    vi.advanceTimersByTime(PROCEED_DELAY_MS - 1);
    expect(calls).toEqual([]);
    vi.advanceTimersByTime(1);
    expect(calls).toEqual(['proceed']);
  });

  it('clears `proceeding` once the proceed has fired', () => {
    const gate = newProceedGate();
    scheduleProceed(gate, () => {});
    expect(gate.proceeding).toBe(true);
    vi.advanceTimersByTime(PROCEED_DELAY_MS);
    expect(gate.proceeding).toBe(false);
    expect(gate.timer).toBeNull();
  });

  it('replaces a pending proceed instead of queueing a second one', () => {
    // Two "Detect again" successes in a row must install once, not twice.
    const gate = newProceedGate();
    const calls: string[] = [];
    scheduleProceed(gate, () => calls.push('proceed'));
    scheduleProceed(gate, () => calls.push('proceed'));
    vi.advanceTimersByTime(PROCEED_DELAY_MS * 4);
    expect(calls).toEqual(['proceed']);
  });
});

describe('cancelProceed — cancel means cancel', () => {
  it('prevents a scheduled proceed from ever firing', () => {
    const gate = newProceedGate();
    const calls: string[] = [];

    // "Detect again" succeeded → proceed committed, pause running.
    scheduleProceed(gate, () => calls.push('proceed'));
    // ...user changes their mind inside the pause (Cancel / Escape /
    // backdrop all land on the modal's `cancel()`).
    expect(cancelProceed(gate)).toBe(true);
    calls.push('cancel');

    vi.advanceTimersByTime(PROCEED_DELAY_MS * 10);

    // Pre-fix this was ['cancel', 'proceed'] — both handlers ran, in that
    // order, and the install went ahead.
    expect(calls).toEqual(['cancel']);
    expect(gate.proceeding).toBe(false);
    expect(gate.timer).toBeNull();
  });

  it('is a harmless no-op when nothing is pending', () => {
    const gate = newProceedGate();
    expect(cancelProceed(gate)).toBe(false);
    expect(gate.proceeding).toBe(false);
  });

  it('leaves a NORMAL confirm path intact (leave-alone)', () => {
    // No cancel → the proceed still fires. The fix must not turn the
    // success path into a dead end.
    const gate = newProceedGate();
    const calls: string[] = [];
    scheduleProceed(gate, () => calls.push('proceed'));
    vi.advanceTimersByTime(PROCEED_DELAY_MS);
    expect(calls).toEqual(['proceed']);
  });
});

describe('preflightBusy — what the dismissal routes are gated on', () => {
  it('is true while a detection round-trip is in flight', () => {
    expect(preflightBusy(newProceedGate(), true)).toBe(true);
  });

  it('is true during the pause, when `redetecting` is already false', () => {
    // This is the whole point: `detectAgain`'s `finally` sets
    // `redetecting = false` ~350 ms before the timer fires, so a guard
    // written against `redetecting` alone evaluates false exactly when
    // it must not.
    const gate = newProceedGate();
    scheduleProceed(gate, () => {});
    expect(preflightBusy(gate, false)).toBe(true);
  });

  it('is false when idle, and false again once the proceed has fired', () => {
    const gate = newProceedGate();
    expect(preflightBusy(gate, false)).toBe(false);
    scheduleProceed(gate, () => {});
    vi.advanceTimersByTime(PROCEED_DELAY_MS);
    expect(preflightBusy(gate, false)).toBe(false);
  });
});
