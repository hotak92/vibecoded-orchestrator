// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.107 (A-003) — Preferences → Services → "Owed background work": the
// machine's one owed-work queue (projects whose skipped KG seed, secondary
// backfill or code-graph walk waits for Weaviate and a free slot) and its one
// worker. Every decision the line makes lives here (vitest-pinned, injected
// `invoke`); `OwedWorkQueue.svelte` is markup over it. The queue itself is
// `vco_lib.owed_work`, read through `get_owed_work_status`.

export interface InvokeDeps {
  invoke: <T>(cmd: string, args?: Record<string, unknown>) => Promise<T>;
}

/** Mirror of `vco_lib.owed_work.status` JSON. */
export interface OwedWorkStatus {
  ok: boolean;
  queued: number;
  worker_running: boolean;
  projects: Array<{ key: string; folder: string; enqueued_at: number; tries: number }>;
  error?: string;
}

export function loadOwedWorkStatus(deps: InvokeDeps): Promise<OwedWorkStatus> {
  return deps.invoke<OwedWorkStatus>('get_owed_work_status');
}

/** The folder's last path segment (either separator). */
export function projectLabel(folder: string): string {
  const parts = folder.split(/[\\/]+/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : folder;
}

/** The one-line state under "Owed background work". */
export function queueLine(st: OwedWorkStatus): string {
  if (!st.ok) return `Could not read the owed-work queue${st.error ? `: ${st.error}` : ''}.`;
  if (st.queued === 0) return 'Nothing queued — no skipped background work is waiting.';
  const names = st.projects.map((p) => projectLabel(p.folder)).join(', ');
  const n = st.queued === 1 ? '1 project' : `${st.queued} projects`;
  const state = st.worker_running
    ? 'the background worker is running them one at a time'
    : 'they run once Weaviate is ready (the worker starts with the next session or prompt)';
  return `${n} queued (${names}) — ${state}.`;
}
