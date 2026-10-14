// Toast notification store. The DWELL POLICY lives here — one home, used
// by every call site (none passes its own duration).
//
// v0.2.103 (field, 2026-10-14): toasts used to auto-dismiss after a flat
// 4 s for every kind, and outcome toasts vanished before they could be
// read — an owner clicked a code-graph update action, the "started in the
// background" toast was gone inside a glance, a started walk looked like a
// failure, and it got clicked again. The policy now in force:
//
//   * error toasts dwell at least 15 s (they also persist to the bell
//     inbox, but the toast itself must stay readable);
//   * success/info toasts dwell at least 8 s;
//   * long messages dwell proportionally longer;
//   * hovering a toast PAUSES its dismissal — the timer re-arms with the
//     computed dwell only after the pointer leaves it.
//
// Errors additionally persist to the bell inbox (notifications.ts) so the
// user can review them after the toast is gone. Pass an optional `key` to
// get dedup + auto-resolve: an error and a later success sharing the same
// key cancel out ("the problem got fixed"). Untagged calls behave exactly
// as before — the `opts` argument is optional and back-compatible.

import { writable } from 'svelte/store';
import { notifications } from './notifications';

export type ToastKind = 'info' | 'success' | 'error';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

export interface ToastOptions {
  /**
   * Resolution/dedup key shared across an error and its eventual success
   * (e.g. `module:rl-reranker:install`). Lets the bell inbox dedup
   * repeated errors and auto-clear a stored error when the same action
   * later succeeds.
   */
  key?: string;
}

/** Minimum dwell per kind (ms). Errors get the long end: they are the
 *  messages whose loss made users re-click actions that had succeeded. */
export const TOAST_MIN_DWELL_MS: Record<ToastKind, number> = {
  error: 15_000,
  success: 8_000,
  info: 8_000,
};

/** Messages up to this many characters get the bare minimum dwell. */
const LENGTH_BASE_CHARS = 80;
/** Extra dwell per character beyond {@link LENGTH_BASE_CHARS} — long
 *  messages take proportionally longer to read. */
const MS_PER_EXTRA_CHAR = 60;
/** Ceiling so a pathological message cannot pin a toast forever. */
const MAX_DWELL_MS = 60_000;

/**
 * The ONE dwell policy for every toast (pure — pinned by unit tests).
 * Call sites never pass their own duration; `push` applies this.
 */
export function toastDwellMs(kind: ToastKind, message: string): number {
  const base = TOAST_MIN_DWELL_MS[kind] ?? TOAST_MIN_DWELL_MS.info;
  const extra =
    Math.max(0, message.length - LENGTH_BASE_CHARS) * MS_PER_EXTRA_CHAR;
  return Math.min(MAX_DWELL_MS, base + extra);
}

const items = writable<ToastItem[]>([]);
let nextId = 1;

/** Toasts under the pointer — their dismissal timers are paused. */
const hovered = new Set<number>();
/** Toasts whose timer fired while hovered; leaving re-arms them. */
const paused = new Set<number>();
/** Computed dwell per live toast, so a re-arm repeats the same policy. */
const dwellMs = new Map<number, number>();

function toText(message: unknown): string {
  return typeof message === 'string'
    ? message
    : message instanceof Error
      ? message.message
      : String(message ?? 'unknown error');
}

function remove(id: number): void {
  hovered.delete(id);
  paused.delete(id);
  dwellMs.delete(id);
  items.update((list) => list.filter((t) => t.id !== id));
}

function armAutoDismiss(id: number, delay: number): void {
  setTimeout(() => {
    // Pause-on-hover: while the pointer is on the toast the dismissal
    // waits; `setHovered(id, false)` re-arms the computed dwell.
    if (hovered.has(id)) {
      paused.add(id);
      return;
    }
    remove(id);
  }, delay);
}

function push(kind: ToastKind, message: unknown, opts?: ToastOptions) {
  const id = nextId++;
  const text = toText(message);

  // Side-effects on the bell inbox: persist errors, auto-resolve on
  // success. info toasts don't touch the inbox.
  if (kind === 'error') {
    notifications.record(text, opts?.key, Date.now());
  } else if (kind === 'success') {
    notifications.resolve(opts?.key);
  }

  const delay = toastDwellMs(kind, text);
  dwellMs.set(id, delay);
  items.update((list) => [...list, { id, kind, message: text }]);
  armAutoDismiss(id, delay);
}

export const toast = {
  subscribe: items.subscribe,
  info: (m: unknown, opts?: ToastOptions) => push('info', m, opts),
  success: (m: unknown, opts?: ToastOptions) => push('success', m, opts),
  error: (m: unknown, opts?: ToastOptions) => push('error', m, opts),
  /** Mark a toast as (no longer) under the pointer — the renderer
   *  (Toast.svelte) calls this on pointerenter/pointerleave. A paused
   *  toast re-arms its full computed dwell when the pointer leaves. */
  setHovered(id: number, isHovered: boolean) {
    if (isHovered) {
      hovered.add(id);
      return;
    }
    hovered.delete(id);
    if (paused.delete(id)) {
      armAutoDismiss(id, dwellMs.get(id) ?? toastDwellMs('info', ''));
    }
  },
  dismiss(id: number) {
    remove(id);
  },
};
