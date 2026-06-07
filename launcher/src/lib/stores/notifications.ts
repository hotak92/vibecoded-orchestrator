// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Error-notification store — the "bell" inbox that PERSISTS error toasts.
//
// Why this exists: toasts auto-dismiss after 4 s (toast.ts). Important
// errors (failed module install, container start failure, etc.) often
// vanish before the user reads them. This store keeps a durable copy of
// every error toast so the user can review them later from the bell in
// the RightSidebar, copy the text, or dismiss them.
//
// Auto-resolution (mixed strategy, per Fabio 2026-06-05):
//   1. Dedup by `key`: two errors with the same key collapse into one
//      entry with a `count` ("×3") instead of stacking duplicates.
//   2. Auto-clear on success: when a SUCCESS toast fires with the same
//      `key` as a stored error, that error auto-removes ("the problem
//      got resolved → the notification disappears").
//   3. Manual: each entry has a trash action (and copy).
//
// The `key` is optional. Callers that tag a toast with a key
// (toast.error(msg, { key: 'module:rl-reranker:install' })) get dedup +
// auto-resolve. Untagged errors still persist but only dedup on identical
// message text and can only be cleared manually.

import { writable, derived } from 'svelte/store';

export interface ErrorNotification {
  id: number;
  /** Resolution/dedup key. Falls back to the message text when absent. */
  key: string;
  message: string;
  /** First time this error was seen (ms epoch, set by caller — see note). */
  firstSeen: number;
  lastSeen: number;
  /** How many times this same error fired (dedup counter). */
  count: number;
}

const items = writable<ErrorNotification[]>([]);
let nextId = 1;

/**
 * Record an error in the bell inbox. Called by toast.error so every error
 * toast leaves a durable trace. Dedups by effective key (explicit key or
 * the message text). `now` is injected so the store stays pure/testable.
 */
function record(message: string, key: string | undefined, now: number): void {
  const effectiveKey = key && key.length > 0 ? key : message;
  items.update((list) => {
    const existing = list.find((n) => n.key === effectiveKey);
    if (existing) {
      // Dedup: bump count + lastSeen, keep the entry at its position.
      return list.map((n) =>
        n.key === effectiveKey
          ? { ...n, count: n.count + 1, lastSeen: now, message }
          : n,
      );
    }
    return [
      ...list,
      {
        id: nextId++,
        key: effectiveKey,
        message,
        firstSeen: now,
        lastSeen: now,
        count: 1,
      },
    ];
  });
}

/**
 * Auto-resolve: a success for the same key clears the matching stored
 * error ("the problem got fixed"). No-op when no entry matches the key.
 */
function resolve(key: string | undefined): void {
  if (!key || key.length === 0) return;
  items.update((list) => list.filter((n) => n.key !== key));
}

/** Manual dismiss of a single entry by id (trash icon). */
function dismiss(id: number): void {
  items.update((list) => list.filter((n) => n.id !== id));
}

/** Clear every stored error (the panel's "Clear all"). */
function clearAll(): void {
  items.update(() => []);
}

/** Live count for the bell badge. */
export const errorCount = derived(items, ($items) =>
  $items.reduce((sum, n) => sum + n.count, 0),
);

export const notifications = {
  subscribe: items.subscribe,
  record,
  resolve,
  dismiss,
  clearAll,
};
