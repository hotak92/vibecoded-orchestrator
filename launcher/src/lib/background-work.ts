// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106: the always-visible background-work indicator, pure half.
//
// Field report: after closing the "Update all" window nothing showed that
// KG sync / code-graph / summary jobs were still running, so a slow machine
// looked idle (or frozen). Those jobs already stream their own events
// (`kg-sync-progress`, `code-graph-build-progress`, `kg-summary-progress`,
// `project://setup-progress`) and keep a status row each; this module folds
// them into ONE map — no new backend signal. The per-event wording is the
// existing one (`kgSyncSubLabel` / `codeGraphSubLabel` from the Update-all
// modal), so the indicator and that modal can never describe the same job
// differently.

import { codeGraphSubLabel, kgSyncSubLabel } from './components/update-all-progress';
import type {
  CodeGraphBuildProgress,
  KgSyncProgress,
} from './components/update-all-progress';
import type { KgSummaryView } from './types/launcher';

export type BgJobKind = 'setup' | 'kg-sync' | 'code-graph' | 'kg-summary';

export interface BgJob {
  kind: BgJobKind;
  project_id: string;
  label: string;
}

/** `<kind>:<project_id>` → the running job. Insertion order is display order. */
export type BgJobs = Record<string, BgJob>;

export function bgJobKey(kind: BgJobKind, projectId: string): string {
  return `${kind}:${projectId}`;
}

/**
 * Fold one job update in. A non-null `label` means the job is running (add or
 * relabel); `null` means it reached a terminal state (remove). Returns the SAME
 * object when nothing changed so reactive consumers do not re-render.
 */
export function applyBgJob(
  jobs: BgJobs,
  kind: BgJobKind,
  projectId: string,
  label: string | null,
): BgJobs {
  const key = bgJobKey(kind, projectId);
  if (label === null) {
    if (!(key in jobs)) return jobs;
    const next = { ...jobs };
    delete next[key];
    return next;
  }
  if (jobs[key]?.label === label) return jobs;
  return { ...jobs, [key]: { kind, project_id: projectId, label } };
}

/** Label for a `kg-summary-progress` event / status row; null unless running. */
export function kgSummarySubLabel(v: Pick<KgSummaryView, 'status' | 'nodes_total' | 'nodes_succeeded' | 'nodes_unchanged' | 'nodes_failed' | 'nodes_skipped'>): string | null {
  if (v.status !== 'running') return null;
  if (v.nodes_total > 0) {
    const done = v.nodes_succeeded + v.nodes_unchanged + v.nodes_failed + v.nodes_skipped;
    return `writing summaries ${Math.min(done, v.nodes_total)}/${v.nodes_total}`;
  }
  return 'writing summaries…';
}

/** Label for a project-setup status; null once the setup is terminal. */
export function setupSubLabel(status: string): string | null {
  if (status === 'done' || status === 'deferred' || status === 'failed') return null;
  return 'setting up the project…';
}

/** Event-to-update adapters (one per existing event). */
export function kgSyncJob(e: KgSyncProgress): [BgJobKind, string, string | null] {
  return ['kg-sync', e.project_id, kgSyncSubLabel(e)];
}
export function codeGraphJob(e: CodeGraphBuildProgress): [BgJobKind, string, string | null] {
  return ['code-graph', e.project_id, codeGraphSubLabel(e)];
}
export function kgSummaryJob(e: KgSummaryView): [BgJobKind, string, string | null] {
  return ['kg-summary', e.project_id, kgSummarySubLabel(e)];
}

export interface BgJobLine {
  key: string;
  project_id: string;
  project_name: string;
  label: string;
}

/** Render-ready rows ("<project> — <label>"), names resolved from the project
 *  list; an id no longer in the list still shows (by id) rather than vanish. */
export function bgJobLines(jobs: BgJobs, nameById: ReadonlyMap<string, string>): BgJobLine[] {
  return Object.entries(jobs).map(([key, j]) => ({
    key,
    project_id: j.project_id,
    project_name: nameById.get(j.project_id) ?? j.project_id,
    label: j.label,
  }));
}

/** The indicator's short text: `null` hides it. */
export function bgSummary(jobs: BgJobs): string | null {
  const n = Object.keys(jobs).length;
  if (n === 0) return null;
  return n === 1 ? '1 job running' : `${n} jobs running`;
}
