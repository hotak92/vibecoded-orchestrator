// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100 (F-W2-05): the "Syncing…" / "Re-syncing…" modal's progress, fed
// by the analyzer's own progress lines. `project_codegraph_extras.rs` turns
// every `{"progress": …}` JSON line of the analyzer into a
// `vct-codegraph-extras-progress` event; before this the modal showed an
// indeterminate spinner and the events reached only the shell's finish
// notice. Pure, so the reduction is tested without mounting the modal.

/** Must match `PROGRESS_EVENT` in `src-tauri/src/commands/project_codegraph_extras.rs`. */
export const EXTRAS_PROGRESS_EVENT = 'vct-codegraph-extras-progress';

/** Mirror of Rust `project_codegraph_extras::ExtrasProgress`. */
export interface ExtrasProgressPayload {
  project_id: string;
  label: string;
  /** Fractional [0, 1]. */
  progress: number;
  message: string;
  file: string;
  lang: string;
}

/** What the modal renders: a determinate fraction and one status line. */
export interface ExtrasSyncProgress {
  fraction: number;
  line: string;
}

/**
 * Fold one event into the modal's progress. Events for another project, or
 * arriving while no run is in flight, leave it unchanged (the per-project
 * mutex serialises runs, so the project id identifies the run). The fraction
 * never goes backwards within a run; the line is the analyzer's message, or
 * the file it is on.
 */
export function applyExtrasProgress(
  prev: ExtrasSyncProgress | null,
  payload: ExtrasProgressPayload,
  projectId: string,
  running: boolean,
): ExtrasSyncProgress | null {
  if (!running || payload.project_id !== projectId) return prev;
  const raw = Number.isFinite(payload.progress) ? payload.progress : 0;
  const clamped = Math.max(0, Math.min(1, raw));
  const fraction = Math.max(prev?.fraction ?? 0, clamped);
  const line =
    (payload.message ?? '').trim() ||
    ((payload.file ?? '').trim() ? `Indexing ${payload.file.trim()}` : '') ||
    prev?.line ||
    'Analyzing…';
  return { fraction, line };
}

/** Percentage for the bar and its aria value. */
export function progressPercent(p: ExtrasSyncProgress): number {
  return Math.round(p.fraction * 100);
}
