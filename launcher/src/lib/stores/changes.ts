// Change-log poller — P7 concurrency invalidation.
//
// Polls the launcher every POLL_INTERVAL_MS for new change_log rows.
// Whenever a row arrives whose `table_name` matches a registered
// listener, the listener fires (with the row as payload). Each
// data-store can subscribe to the tables it cares about and refetch.
//
// Approach: deliberately simple. We chose polling over Tauri-event push
// because:
//   * polling is correct under multiple concurrent windows without any
//     additional fan-out infra,
//   * 5s latency is acceptable for human-driven mutations,
//   * backend complexity is minimal (one extra table + two commands),
//   * fallback to "no real-time" is trivial when Tauri is unavailable.
//
// A future v2 may layer Tauri events on top for sub-second updates;
// the listener API here would not need to change.
//
// See docs/CONCURRENCY_INVALIDATION.md for the full design + tradeoffs.

import { writable, get } from 'svelte/store';
import { safeInvoke, tauriAvailable } from '$lib/tauri';

export interface ChangeRow {
  seq: number;
  table_name: string;
  op: string;
  key: string | null;
  project_id: string | null;
  created_at: number;
}

const POLL_INTERVAL_MS = 5000;

type Listener = (row: ChangeRow) => void;
const listeners = new Map<string, Set<Listener>>();

let cursor: number = 0;
let timer: ReturnType<typeof setInterval> | null = null;
let started = false;

export const lastPolledAt = writable<number | null>(null);
export const pollerActive = writable<boolean>(false);

export function onChange(table: string, fn: Listener): () => void {
  let set = listeners.get(table);
  if (!set) {
    set = new Set();
    listeners.set(table, set);
  }
  set.add(fn);
  return () => {
    set!.delete(fn);
    if (set!.size === 0) listeners.delete(table);
  };
}

async function pollOnce(): Promise<void> {
  if (!tauriAvailable()) return;
  try {
    const rows = await safeInvoke<ChangeRow[]>('poll_changes', { since: cursor });
    if (!rows) return;
    for (const row of rows) {
      cursor = Math.max(cursor, row.seq);
      const set = listeners.get(row.table_name);
      if (set) {
        for (const fn of set) {
          try {
            fn(row);
          } catch (e) {
            // Listener errors must not stop the poller.
            console.warn('[changes] listener for', row.table_name, 'threw:', e);
          }
        }
      }
    }
    lastPolledAt.set(Date.now());
  } catch (e) {
    // Silent — we'll retry on the next tick. The user is not blocked
    // by polling failures.
    console.debug('[changes] poll failed:', e);
  }
}

/** Start the polling loop. Idempotent. Initial cursor = current head
 *  so we don't replay historical changes. */
export async function startChangePoller(): Promise<void> {
  if (started || !tauriAvailable()) return;
  started = true;
  try {
    const head = await safeInvoke<number>('current_change_seq');
    if (typeof head === 'number') cursor = head;
  } catch {}
  timer = setInterval(pollOnce, POLL_INTERVAL_MS);
  pollerActive.set(true);
}

export function stopChangePoller(): void {
  if (timer) clearInterval(timer);
  timer = null;
  started = false;
  pollerActive.set(false);
}

/** Force an immediate poll (for "Refresh now" buttons). */
export async function pollNow(): Promise<void> {
  await pollOnce();
}

export function currentCursor(): number {
  return cursor;
}

// Re-export for stores that want to introspect last-refresh timing.
export function lastPolledMsAgo(): number | null {
  const t = get(lastPolledAt);
  return t == null ? null : Date.now() - t;
}
