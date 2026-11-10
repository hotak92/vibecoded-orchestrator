// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.106: the background-work indicator's fold. Field report: after the
// "Update all" window closed, nothing showed that KG-sync / code-graph /
// summary jobs were still running.

import { describe, it, expect } from 'vitest';
import {
  applyBgJob,
  bgJobLines,
  bgSummary,
  codeGraphJob,
  kgSummaryJob,
  kgSummarySubLabel,
  kgSyncJob,
  setupSubLabel,
  type BgJobs,
} from './background-work';
import type { KgSummaryView, KgSyncView } from './types/launcher';
import { kgSyncSubLabel } from './components/update-all-progress';

function sync(project_id: string, status: KgSyncView['status']): KgSyncView {
  return {
    project_id,
    status,
    started_at_iso: null,
    finished_at_iso: null,
    duration_ms: null,
    kg_total: 10,
    kg_succeeded: 3,
    kg_failed: 0,
    docs_total: 0,
    docs_succeeded: 0,
    docs_failed: 0,
    error_message: null,
    log_tail: null,
    current_phase: 'kg',
  } as unknown as KgSyncView;
}

function summary(status: KgSummaryView['status']): KgSummaryView {
  return {
    project_id: 'b',
    status,
    started_at_iso: null,
    finished_at_iso: null,
    duration_ms: null,
    nodes_total: 8,
    nodes_succeeded: 2,
    nodes_unchanged: 1,
    nodes_failed: 0,
    nodes_skipped: 1,
    backend: null,
    error_message: null,
    log_tail: null,
    current_phase: 'summarise',
  };
}

describe('background work fold', () => {
  it('a running job shows until its terminal event, across every job kind', () => {
    let jobs: BgJobs = {};
    jobs = applyBgJob(jobs, ...kgSyncJob(sync('a', 'running')));
    jobs = applyBgJob(jobs, ...codeGraphJob({ project_id: 'a', status: 'running', files_analyzed: 12, current_phase: 'python' }));
    jobs = applyBgJob(jobs, ...kgSummaryJob(summary('running')));
    jobs = applyBgJob(jobs, 'setup', 'c', setupSubLabel('running'));
    expect(bgSummary(jobs)).toBe('4 jobs running');

    const lines = bgJobLines(jobs, new Map([['a', 'Alpha'], ['b', 'Beta']]));
    // The KG-sync wording is the Update-all modal's own (kgSyncSubLabel).
    expect(lines[0].label).toBe(kgSyncSubLabel(sync('a', 'running')));
    expect(lines.map((l) => `${l.project_name} — ${l.label}`)).toEqual([
      `Alpha — ${kgSyncSubLabel(sync('a', 'running'))}`,
      'Alpha — building code graph (12 files)…',
      'Beta — writing summaries 4/8',
      'c — setting up the project…',
    ]);

    jobs = applyBgJob(jobs, ...kgSyncJob(sync('a', 'success')));
    jobs = applyBgJob(jobs, ...codeGraphJob({ project_id: 'a', status: 'failed', files_analyzed: 12, current_phase: null }));
    jobs = applyBgJob(jobs, ...kgSummaryJob(summary('success')));
    jobs = applyBgJob(jobs, 'setup', 'c', setupSubLabel('done'));
    expect(bgSummary(jobs)).toBeNull();
  });

  it('a repeat label or a terminal event for an unknown job is an identity no-op', () => {
    const jobs = applyBgJob({}, 'code-graph', 'a', 'x');
    expect(applyBgJob(jobs, 'code-graph', 'a', 'x')).toBe(jobs);
    expect(applyBgJob(jobs, 'kg-sync', 'zzz', null)).toBe(jobs);
  });

  it('summary label only while running', () => {
    expect(kgSummarySubLabel(summary('pending'))).toBeNull();
    expect(kgSummarySubLabel({ ...summary('running'), nodes_total: 0 })).toBe('writing summaries…');
  });
});
