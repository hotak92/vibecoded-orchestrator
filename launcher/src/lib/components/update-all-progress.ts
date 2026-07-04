// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.73 — Update-all live progress reducer.
//
// Pure, dependency-free reducer for the `update_all_progress` Tauri event
// stream emitted by `update_all_projects` (see the EVENT CONTRACT doc-comment
// on `UPDATE_ALL_PROGRESS_EVENT` in
// `launcher/src-tauri/src/commands/projects_v2.rs`). Extracted from
// `UpdateAllProjectsModal.svelte` so the progress-list state transitions are
// unit-testable in the pure-node vitest environment (which does NOT stand up
// SvelteKit / a DOM — see `launcher/vitest.config.ts`).
//
// The modal owns the reactive `$state` bindings; this module owns the *logic*
// of turning an ordered event stream into a keyed, ordered progress list.

/**
 * Mirror of the Rust `UpdateAllProgressEvent` payload. Field names are
 * snake_case to match the serialised struct — keep in sync with the Rust
 * doc-comment (the source of truth).
 */
export type UpdateAllProgress = {
  phase: 'started' | 'finished';
  project_id: string;
  project_name: string;
  index: number;
  total: number;
  status: 'succeeded' | 'failed' | 'skipped' | null;
  warnings_count: number | null;
};

/** A row in the live progress checklist. `running` is the pre-terminal state. */
export type ProgressRow = {
  project_id: string;
  project_name: string;
  index: number;
  total: number;
  status: 'running' | 'succeeded' | 'failed' | 'skipped';
  warnings_count: number | null;
};

/** The "Updating <name> (i/N)…" headline target, or null between projects. */
export type CurrentProject = {
  name: string;
  index: number;
  total: number;
} | null;

/** Immutable snapshot the reducer maps forward on each event. */
export type ProgressState = {
  rows: ProgressRow[];
  current: CurrentProject;
};

/** The empty starting state (reset every run). */
export function emptyProgressState(): ProgressState {
  return { rows: [], current: null };
}

/**
 * Fold one `update_all_progress` event into the progress state, returning a
 * NEW state (never mutates the input). Rows are keyed by `project_id` and kept
 * in first-seen order:
 *   - `started`  → append (or reset) the project's row as `running` and set it
 *                  as the current headline.
 *   - `finished` → flip the project's row to its terminal status. If we never
 *                  saw a `started` for it (a `stop_on_error` skip only emits a
 *                  single finished/skipped event), append the terminal row. If
 *                  the finished project was the current headline, clear it.
 */
export function applyProgressEvent(
  state: ProgressState,
  ev: UpdateAllProgress,
): ProgressState {
  const idx = state.rows.findIndex((r) => r.project_id === ev.project_id);

  if (ev.phase === 'started') {
    const row: ProgressRow = {
      project_id: ev.project_id,
      project_name: ev.project_name,
      index: ev.index,
      total: ev.total,
      status: 'running',
      warnings_count: null,
    };
    const rows =
      idx >= 0
        ? state.rows.map((r, i) => (i === idx ? row : r))
        : [...state.rows, row];
    return {
      rows,
      current: { name: ev.project_name, index: ev.index, total: ev.total },
    };
  }

  // finished
  const terminal: ProgressRow = {
    project_id: ev.project_id,
    project_name: ev.project_name,
    index: ev.index,
    total: ev.total,
    status: ev.status ?? 'succeeded',
    warnings_count: ev.warnings_count,
  };
  const rows =
    idx >= 0
      ? state.rows.map((r, i) => (i === idx ? terminal : r))
      : [...state.rows, terminal];
  // Clear the headline if the just-finished project was the current one.
  const current =
    state.current && state.current.index === ev.index ? null : state.current;
  return { rows, current };
}

/** The batch size for the "(i/N)" display: events carry it, fall back to a hint. */
export function progressTotal(
  state: ProgressState,
  fallbackCount: number,
): number {
  return state.rows.length > 0 ? state.rows[0].total : fallbackCount;
}

/** Checklist glyph for a row's status. Unicode so we don't pull an icon dep. */
export function progressIcon(s: ProgressRow['status']): string {
  if (s === 'succeeded') return '✓';
  if (s === 'failed') return '✗';
  if (s === 'skipped') return '–';
  return '⟳'; // running
}
