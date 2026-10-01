// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: the RL "is training data being collected?" figure.
//
// Source of truth is the `rl_events` table in launcher.db, read by the
// `get_rl_dashboard_state` command (`recent_events_count` = last 24 h,
// `total_events_count` = everything for the project). This is independent of
// the RL module install, of the reranking switch and of the container:
// collection runs in every one of those states, and the copy says so.

/** The two collection counters of `RlDashboardState` (Rust wire shape). */
export interface RlCollectionCounts {
  recent_events_count?: number | null;
  total_events_count?: number | null;
}

export interface RlCollectionSummary {
  /** `none` = nothing collected yet, `collecting` = at least one event. */
  key: 'none' | 'collecting';
  /** The figure line, e.g. "1,204 events collected (37 in the last 24 h)". */
  headline: string;
}

const fmt = (n: number) => n.toLocaleString('en-US');

export function summarizeCollection(counts: RlCollectionCounts | null): RlCollectionSummary | null {
  if (!counts) return null;
  const total = Math.max(0, Number(counts.total_events_count ?? 0));
  const recent = Math.max(0, Number(counts.recent_events_count ?? 0));
  if (total === 0) {
    return { key: 'none', headline: 'No events collected yet for this project' };
  }
  return {
    key: 'collecting',
    headline: `${fmt(total)} event${total === 1 ? '' : 's'} collected (${fmt(recent)} in the last 24 h)`,
  };
}
