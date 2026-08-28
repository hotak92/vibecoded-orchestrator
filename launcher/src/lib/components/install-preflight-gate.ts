// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 (P2-B7) — the deferred-proceed gate for
// `InstallPreflightRuntimeModal`.
//
// Why this is a module and not four inline lines:
//
// The modal's "Detect again" success path shows "Detected podman —
// proceeding…" for 350 ms before it closes the dialog and calls
// `onProceed()`. That pause was commented "cosmetic only" — but the timer
// it schedules carries the whole consequence of the modal (it is what
// unblocks a real install), and during those 350 ms the modal was fully
// interactive:
//
//   * `redetecting` is set false in `detectAgain`'s `finally`, which runs
//     ~350 ms BEFORE the timer fires, so the footer buttons re-enable and
//     the explicit Cancel button becomes clickable;
//   * `DialogRoot` had no close-gating at all, so Escape / backdrop also
//     dismissed it.
//
// Any of those three routes ran `cancel()` → `onCancel()`, and then the
// still-pending timer fired `onProceed()` anyway. Both handlers ran, in
// that order: the install the user just cancelled went ahead. (This is
// also why a `closeOnEscape={!redetecting}` guard does NOT close the
// window — `redetecting` is already false throughout it. The flag that
// gates dismissal has to be a SEPARATE one that spans the pause.)
//
// Extracted here so the "cancel means cancel" decision is unit-testable
// under the existing node/vitest setup, which deliberately does not mount
// Svelte components (see vitest.config.ts). The modal owns no copy of
// this logic — it calls these functions.

/** Cosmetic pause between "Detected <runtime>" and the modal closing. */
export const PROCEED_DELAY_MS = 350;

/**
 * Mutable gate state. Lives in the component as `$state(...)`, so
 * mutating a field re-renders; every transition goes through the
 * functions below rather than being poked directly.
 */
export interface ProceedGate {
  /**
   * True from the moment a successful re-detect schedules the proceed
   * until that proceed fires (or is cancelled). Spans the pause that
   * `redetecting` does not.
   */
  proceeding: boolean;
  /** Handle of the pending proceed timer; null when none is scheduled. */
  timer: ReturnType<typeof setTimeout> | null;
}

export function newProceedGate(): ProceedGate {
  return { proceeding: false, timer: null };
}

/**
 * Schedule the deferred proceed. Idempotent per gate: a second call
 * while one is pending replaces it rather than queueing a second
 * `onProceed()` (double-detect clicks must not double-install).
 */
export function scheduleProceed(
  gate: ProceedGate,
  proceed: () => void,
  delayMs: number = PROCEED_DELAY_MS,
): void {
  cancelProceed(gate);
  gate.proceeding = true;
  gate.timer = setTimeout(() => {
    gate.timer = null;
    gate.proceeding = false;
    proceed();
  }, delayMs);
}

/**
 * Cancel a pending proceed. Returns true when one was actually pending
 * (the caller can use it for logging; the modal ignores it — cancelling
 * an idle gate is a normal Cancel click).
 */
export function cancelProceed(gate: ProceedGate): boolean {
  const wasPending = gate.timer !== null;
  if (gate.timer !== null) {
    clearTimeout(gate.timer);
    gate.timer = null;
  }
  gate.proceeding = false;
  return wasPending;
}

/**
 * Is the modal busy — i.e. must every dismissal route (Cancel button,
 * Escape, backdrop) and the footer be inert right now?
 *
 * Two disjoint reasons: a detection round-trip is in flight
 * (`redetecting`), or a proceed is already committed and merely waiting
 * out the cosmetic pause (`gate.proceeding`). The second is the one the
 * pre-v0.2.91 modal had no flag for.
 */
export function preflightBusy(gate: ProceedGate, redetecting: boolean): boolean {
  return redetecting || gate.proceeding;
}
