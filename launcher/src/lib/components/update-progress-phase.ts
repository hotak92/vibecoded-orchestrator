// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 (F-W2-04): the progress overlay's per-tick decision, pure.
//
// The failure signal is the store's `failed` flag — ONE home (WP-08,
// `updater.ts::UpdaterState.failed`), never `error`'s truthiness. `error` can
// be set without a failed update op (e.g. reading a pending merge conflict
// failed), and a failure whose backend text was empty used to leave
// `error === ""` — both are exactly why `failed` exists.

export interface ProgressTickInput {
  /** `updater.updating`. */
  updating: boolean;
  /** `updater.failed` — the last update-class op failed. */
  failed: boolean;
  /** The op ended by surfacing a decision modal (non-FF, conflict,
   *  untracked collision, autostash-pop): hand over, never "complete". */
  handover: boolean;
  /** `updating` as seen on the previous tick. */
  prevUpdating: boolean;
}

export type ProgressTick =
  /** Show the failed state; stop the auto-close timer. */
  | 'failed'
  /** An op is in flight (rising edge or steady). */
  | 'running'
  /** Falling edge onto a decision modal — close immediately. */
  | 'handover'
  /** Falling edge, genuine completion — hold, fade, close. */
  | 'completed'
  /** Nothing changed. */
  | 'idle';

export function progressTick(i: ProgressTickInput): ProgressTick {
  if (i.failed) return 'failed';
  if (i.updating) return 'running';
  if (i.prevUpdating) return i.handover ? 'handover' : 'completed';
  return 'idle';
}
