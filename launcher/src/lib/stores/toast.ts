// Tiny toast notification store. Auto-dismiss after 4 s.
//
// Errors additionally persist to the bell inbox (notifications.ts) so the
// user can review them after the 4 s toast vanishes. Pass an optional
// `key` to get dedup + auto-resolve: an error and a later success sharing
// the same key cancel out ("the problem got fixed"). Untagged calls behave
// exactly as before — the `opts` argument is optional and back-compatible.

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

const items = writable<ToastItem[]>([]);
let nextId = 1;

function toText(message: unknown): string {
  return typeof message === 'string'
    ? message
    : message instanceof Error
      ? message.message
      : String(message ?? 'unknown error');
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

  items.update((list) => [...list, { id, kind, message: text }]);
  setTimeout(() => {
    items.update((list) => list.filter((t) => t.id !== id));
  }, 4000);
}

export const toast = {
  subscribe: items.subscribe,
  info: (m: unknown, opts?: ToastOptions) => push('info', m, opts),
  success: (m: unknown, opts?: ToastOptions) => push('success', m, opts),
  error: (m: unknown, opts?: ToastOptions) => push('error', m, opts),
  dismiss(id: number) {
    items.update((list) => list.filter((t) => t.id !== id));
  },
};
