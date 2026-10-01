// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: the RL "is training data being collected?" figure.
//
// Source of truth is the `rl_events` table in launcher.db, read by the
// `get_rl_dashboard_state` command (`recent_events_count` = last 24 h,
// `total_events_count` = everything for the project). This is independent of
// the RL module install, of the reranking switch and of the container:
// collection runs in every one of those states, and the copy says so.

/** One embedding source's share (Rust `RlSourceEventCount`, W5R-13). */
export interface RlSourceEventCount {
  embedding_source: string;
  retrieval: number;
  citation: number;
  total: number;
}

/** The collection counters of `RlDashboardState` (Rust wire shape). */
export interface RlCollectionCounts {
  recent_events_count?: number | null;
  total_events_count?: number | null;
  /** v0.2.100 W5R-13: the same corpus split by embedding source. Absent from
   *  pre-v0.2.100 payloads. */
  events_by_embedding_source?: RlSourceEventCount[] | null;
}

export interface RlCollectionSummary {
  /** `none` = nothing collected yet, `collecting` = at least one event. */
  key: 'none' | 'collecting';
  /** The figure line, e.g. "1,204 events collected (37 in the last 24 h)". */
  headline: string;
  /** One line per embedding source, e.g. "arctic: 602 (retrieval 451,
   *  citation 151)" — shows whether each space (qwen3 / arctic / codesage) is
   *  actually being saved. Empty when the payload carries no breakdown. */
  bySource: string[];
}

function sourceLine(r: RlSourceEventCount): string {
  const n = (x: number) => fmt(Math.max(0, Number(x ?? 0)));
  const other = Math.max(0, Number(r.total ?? 0) - Number(r.retrieval ?? 0) - Number(r.citation ?? 0));
  const parts = [`retrieval ${n(r.retrieval)}`, `citation ${n(r.citation)}`];
  if (other > 0) parts.push(`other ${n(other)}`);
  return `${r.embedding_source}: ${n(r.total)} (${parts.join(', ')})`;
}

const fmt = (n: number) => n.toLocaleString('en-US');

export function summarizeCollection(counts: RlCollectionCounts | null): RlCollectionSummary | null {
  if (!counts) return null;
  const total = Math.max(0, Number(counts.total_events_count ?? 0));
  const recent = Math.max(0, Number(counts.recent_events_count ?? 0));
  const bySource = (counts.events_by_embedding_source ?? []).map(sourceLine);
  if (total === 0) {
    return { key: 'none', headline: 'No events collected yet for this project', bySource };
  }
  return {
    key: 'collecting',
    headline: `${fmt(total)} event${total === 1 ? '' : 's'} collected (${fmt(recent)} in the last 24 h)`,
    bySource,
  };
}
