// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Error-notification store — the "bell" inbox that PERSISTS error toasts.
//
// Why this exists: toasts auto-dismiss (dwell policy in toast.ts). Important
// errors (failed module install, container start failure, etc.) often
// vanish before the user reads them. This store keeps a durable copy of
// every error toast so the user can review them later from the bell in
// the MenuBar, copy the text, or dismiss them.
//
// v0.2.102 W6-J: the same inbox also carries INFO alerts with action
// buttons (`upsertAlert`, tone 'info') — state-of-the-world heads-ups like
// "these projects' KGs may need a duplicate re-scan" (kg-rescan-alerts.ts),
// where a re-check REPLACES the entry rather than counting occurrences.
//
// Auto-resolution (mixed strategy, per a contributor 2026-06-05):
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

/** One actionable button on an info alert (v0.2.102 W6-J). The command is a
 *  Tauri command name; the panel that renders it narrows it to the literal
 *  union it supports so the invoke-name census still sees the names.
 *  `key` is the action's IDENTITY — the each-key in the bell and the
 *  `dropAction` match — so two actions that share a display label (two
 *  registered projects with the same display name) stay two distinct
 *  buttons; producers set it to the projectId the action targets. Falls
 *  back to the label when absent. */
export interface NotificationAction {
  label: string;
  /** Stable identity (e.g. the target projectId). Defaults to the label. */
  key?: string;
  command: string;
  args?: Record<string, unknown>;
}

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
  /** 'error' (default, red styling) or 'info' (a heads-up, calmer tint). */
  tone?: 'error' | 'info';
  /** Actionable buttons (info alerts) — each runs one Tauri command. */
  actions?: NotificationAction[];
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

/**
 * Insert or refresh an ACTIONABLE alert (v0.2.102 W6-J — the KG re-scan
 * heads-up). Unlike `record` (an error happened; dedup counts), an alert
 * describes CURRENT state: a re-check REPLACES the message and the action
 * list under the same key and never bumps a count. Clear it with
 * `resolve(key)` when nothing qualifies anymore.
 */
function upsertAlert(
  entry: { key: string; message: string; actions?: NotificationAction[] },
  now: number,
): void {
  items.update((list) => {
    const existing = list.find((n) => n.key === entry.key);
    if (existing) {
      return list.map((n) =>
        n.key === entry.key
          ? { ...n, message: entry.message, tone: 'info' as const, actions: entry.actions, lastSeen: now }
          : n,
      );
    }
    return [
      ...list,
      {
        id: nextId++,
        key: entry.key,
        message: entry.message,
        firstSeen: now,
        lastSeen: now,
        count: 1,
        tone: 'info' as const,
        actions: entry.actions,
      },
    ];
  });
}

/**
 * One action of an alert ran successfully — drop just that action. When the
 * last action goes, the alert goes with it (nothing left to act on).
 * Matched by the action's identity key (`a.key ?? a.label`) — NOT by the
 * display label, which is not unique (two projects may share a name).
 */
function dropAction(id: number, key: string): void {
  items.update((list) =>
    list.flatMap((n) => {
      if (n.id !== id) return [n];
      const before = n.actions ?? [];
      const actions = before.filter((a) => (a.key ?? a.label) !== key);
      if (actions.length === before.length) return [n];
      if (actions.length === 0) return [];
      return [{ ...n, actions }];
    }),
  );
}

/** Clear every stored error (the panel's "Clear all"). */
function clearAll(): void {
  items.update(() => []);
}

/** Live ERROR count for the bell badge. Info alerts (W6-J `upsertAlert`) are
 *  not errors — the bell counts them separately — so they never inflate this
 *  (an info-only inbox must not read "1 error"). */
export const errorCount = derived(items, ($items) =>
  $items.reduce((sum, n) => (n.tone === 'info' ? sum : sum + n.count), 0),
);

export const notifications = {
  subscribe: items.subscribe,
  record,
  resolve,
  dismiss,
  clearAll,
  upsertAlert,
  dropAction,
};
