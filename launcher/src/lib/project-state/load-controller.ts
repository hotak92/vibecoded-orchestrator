// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.103 (review R8 m1): the Settings surfaces' load/retry lifecycle as
// ONE rune-free controller, unit-testable with a fake invoke (the vitest
// environment is node — no DOM, no component mounting). The components
// keep their $state bindings and mirror the controller's status through
// `subscribe`; the template stays a thin binding.
//
// Rules the lifecycle must keep (pinned in load-controller.test.ts):
//   - a rejected load surfaces an error and NEVER a stale value;
//   - a retry clears the error FIRST (so it shows the loading state
//     again) but KEEPS the previous value until the new one lands — a
//     slow-but-valid load is never cut and never flashes the form away;
//   - there is no timeout anywhere: only success or an exception ends a
//     load.

export interface LoadStatus<T> {
  value: T | null;
  error: string | null;
}

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export interface Loader<T> {
  /** Current snapshot. */
  get(): LoadStatus<T>;
  /** Re-run the load. Resolves when the attempt has settled (either
   *  way), so callers can `await` follow-up work. */
  retry(): Promise<void>;
  /** Fired on every status change, and once immediately on subscribe
   *  (catch-up). Returns an unsubscribe function. */
  subscribe(listener: () => void): () => void;
}

export function createLoader<T>(
  load: () => Promise<T>,
  /** Optional side channel (e.g. a toast) when an attempt fails. */
  onError?: (e: unknown) => void,
): Loader<T> {
  let status: LoadStatus<T> = { value: null, error: null };
  const listeners = new Set<() => void>();
  const emit = () => {
    for (const l of listeners) l();
  };
  return {
    get: () => status,
    retry: async () => {
      // Clear the error, not the value: a retry after a failure shows
      // "Loading…" (value still null), a reload after success keeps the
      // old form visible until the fresh row arrives.
      status = { value: status.value, error: null };
      emit();
      try {
        status = { value: await load(), error: null };
      } catch (e) {
        status = { value: null, error: errorMessage(e) };
        onError?.(e);
      }
      emit();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      listener();
      return () => {
        listeners.delete(listener);
      };
    },
  };
}
