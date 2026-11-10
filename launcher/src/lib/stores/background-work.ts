// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106: the background-work store behind the MenuBar indicator.
//
// A MODULE-SINGLETON (like `project-setup.ts`): the listeners are registered
// once at module load, so the running-job map survives route changes and the
// closing of whatever window started the work (the field case: "Update all").
// It reuses the events the backend already emits for each job, plus the
// project-setup store, and seeds itself once from the per-project status rows
// so a job that started before this window loaded still shows.

import { derived, get, writable } from 'svelte/store';
import { listen, safeInvoke, tauriAvailable } from '$lib/tauri';
import { projectSetup } from '$lib/stores/project-setup';
import {
  applyBgJob,
  codeGraphJob,
  kgSummaryJob,
  kgSyncJob,
  setupSubLabel,
  type BgJobKind,
  type BgJobs,
} from '$lib/background-work';
import type {
  CodeGraphBuildProgress,
  KgSyncProgress,
} from '$lib/components/update-all-progress';
import type { KgSummaryView } from '$lib/types/launcher';

function createBackgroundWorkStore() {
  const jobs = writable<BgJobs>({});
  const fold = ([kind, id, label]: [BgJobKind, string, string | null]) =>
    jobs.update((j) => applyBgJob(j, kind, id, label));

  if (tauriAvailable()) {
    void listen<KgSyncProgress>('kg-sync-progress', (e) => fold(kgSyncJob(e.payload)));
    void listen<CodeGraphBuildProgress>('code-graph-build-progress', (e) =>
      fold(codeGraphJob(e.payload)),
    );
    void listen<KgSummaryView>('kg-summary-progress', (e) => fold(kgSummaryJob(e.payload)));
  }
  // The add-project setup is already folded by its own store; mirror it.
  projectSetup.subscribe((s) => {
    const a = s.active;
    if (a) fold(['setup', a.project_id, setupSubLabel(a.status)]);
  });

  const seeded = new Set<string>();

  /** Seed once per project from the three status rows (a job that started
   *  before this window loaded emits no event we could have heard). */
  async function seed(projectIds: readonly string[]): Promise<void> {
    if (!tauriAvailable()) return;
    for (const projectId of projectIds) {
      if (seeded.has(projectId)) continue;
      seeded.add(projectId);
      const [sync, graph, summary] = await Promise.all([
        safeInvoke<KgSyncProgress | null>('get_kg_sync_status', { projectId }),
        safeInvoke<CodeGraphBuildProgress | null>('get_code_graph_build_status', { projectId }),
        safeInvoke<KgSummaryView | null>('get_kg_summary_status', { projectId }),
      ]);
      // A live event that arrived meanwhile is newer than the row: only add.
      const current = get(jobs);
      for (const upd of [
        sync ? kgSyncJob(sync) : null,
        graph ? codeGraphJob(graph) : null,
        summary ? kgSummaryJob(summary) : null,
      ]) {
        if (upd && upd[2] !== null && !(`${upd[0]}:${upd[1]}` in current)) fold(upd);
      }
    }
  }

  return {
    subscribe: jobs.subscribe,
    seed,
    /** Test hook. */
    reset() {
      jobs.set({});
      seeded.clear();
    },
  };
}

export const backgroundWork = createBackgroundWorkStore();
export const backgroundJobCount = derived(backgroundWork, ($j) => Object.keys($j).length);
