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
  /**
   * Live intra-project sub-progress label (v0.2.89) — a condensed line like
   * "syncing docs 35/35" or "building code graph…", derived from the
   * `kg-sync-progress` / `code-graph-build-progress` sub-events that stream
   * *during* a single project's update. `null` when nothing informative is in
   * flight (quiet phase, or the row is terminal). Only rendered while
   * `status === 'running'` so a done row never shows a stale sub-line.
   */
  sub: string | null;
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
      sub: null,
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
    // FORCE null on `finished`: a late, fire-and-forget sub-event that arrives
    // after the project's terminal event must not leave a stale sub-line under
    // a done row.
    sub: null,
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

// ── Intra-project sub-progress (v0.2.89) ──────────────────────────────────
//
// During a single project's update the backend already streams two finer
// events that each carry the owning `project_id`:
//   - `kg-sync-progress`         → mirror of Rust `KgSyncView` (kg_sync.rs)
//   - `code-graph-build-progress`→ mirror of Rust `CodeGraphBuildView`
//     (codegraph.rs)
// During Update-all these stream for whichever project is currently running,
// so we fold a condensed label into that project's row to prove long
// re-embed / codegraph builds are progressing (not frozen). FRONTEND-ONLY:
// no backend change — the events and their `project_id` already exist.
//
// `status` for both is one of the DB status strings: 'pending' | 'running' |
// 'success' | 'partial' (codegraph only) | 'failed' | 'skipped'. Only
// 'running' is worth surfacing as an in-flight sub-line; terminal statuses
// return null so the reducer clears any lingering sub-detail.

/**
 * Mirror of the Rust `KgSyncView` payload (`kg-sync-progress` event). Only the
 * fields we render are typed here; extra fields on the wire are ignored.
 */
export type KgSyncProgress = {
  project_id: string;
  status: string;
  kg_total: number;
  kg_succeeded: number;
  docs_total: number;
  docs_succeeded: number;
  current_phase: string | null;
};

/**
 * Mirror of the Rust `CodeGraphBuildView` payload
 * (`code-graph-build-progress` event). Only the rendered fields are typed.
 */
export type CodeGraphBuildProgress = {
  project_id: string;
  status: string;
  files_analyzed: number;
  current_phase: string | null;
};

/**
 * Condensed sub-label for a `kg-sync-progress` event, or `null` when the event
 * is terminal / uninformative (so the row's sub-line clears). Deterministic:
 * no clock / randomness — the label is a pure function of the payload.
 */
export function kgSyncSubLabel(evt: KgSyncProgress): string | null {
  // Only surface while the sync is actually running; a terminal status means
  // this phase is done and the row should stop showing a sub-line.
  if (evt.status !== 'running') return null;

  const phase = evt.current_phase ?? null;
  // Docs re-embedding is the long tail the Windows field report flagged as
  // "looks frozen" — show the running count so it visibly advances.
  if (phase === 'docs' && evt.docs_total > 0) {
    return `syncing docs ${evt.docs_succeeded}/${evt.docs_total}`;
  }
  if (phase === 'knowledge' && evt.kg_total > 0) {
    return `syncing knowledge ${evt.kg_succeeded}/${evt.kg_total}`;
  }
  if (phase === 'scan') {
    return 'scanning knowledge graph…';
  }
  // Running but no count yet / unknown phase: a generic-but-alive line beats a
  // static spinner.
  return 'syncing knowledge graph…';
}

/**
 * Condensed sub-label for a `code-graph-build-progress` event, or `null` when
 * the event is terminal / uninformative. Deterministic (no clock/randomness).
 */
export function codeGraphSubLabel(evt: CodeGraphBuildProgress): string | null {
  if (evt.status !== 'running') return null;

  const phase = evt.current_phase ?? null;
  if (phase === 'weaviate-upload') {
    return 'uploading code graph…';
  }
  if (evt.files_analyzed > 0) {
    // Language phases (python / typescript / …) — show files analysed so far.
    return `building code graph (${evt.files_analyzed} files)…`;
  }
  return 'building code graph…';
}

/**
 * Fold a sub-progress `label` into the row matching `projectId`, returning a
 * NEW state (never mutates the input). Rules:
 *   - Only rows that are still `running` accept a sub-label — a terminal row
 *     (a late sub-event arriving after the project finished) is ignored so a
 *     done row never regrows a sub-line.
 *   - An unknown `projectId` (no matching row) is a no-op.
 *   - `label === null` clears the row's sub-line.
 *   - If the row's `sub` already equals `label`, return the SAME state object
 *     (identity no-op) so reactive consumers don't re-render on a repeat.
 */
export function applySubProgress(
  state: ProgressState,
  projectId: string,
  label: string | null,
): ProgressState {
  const idx = state.rows.findIndex((r) => r.project_id === projectId);
  if (idx < 0) return state; // unknown project — ignore
  const row = state.rows[idx];
  if (row.status !== 'running') return state; // terminal row — ignore late event
  if (row.sub === label) return state; // no change — identity no-op

  const rows = state.rows.map((r, i) => (i === idx ? { ...r, sub: label } : r));
  return { rows, current: state.current };
}

// ── Background-activity footer (v0.2.89 review MAJOR-2) ────────────────────
//
// kg-sync / codegraph work is spawned FIRE-AND-FORGET by the backend
// (`projects_v2.rs` tokio::spawn; the kg-sync also waits on embed-admission)
// and mostly runs AFTER the project's `finished` boundary event — so its
// sub-events stream against TERMINAL rows, which `applySubProgress`
// correctly drops per its spec (a done row must never regrow a sub-line).
// Without a second surface those embeds are invisible: the modal says
// "finished" while Weaviate still grinds → the field failure mode
// ("looks frozen → restart → re-embed") survives.
//
// The background-activity map is that second surface. It accepts sub-events
// for ANY project REGARDLESS of row status and feeds a footer line
// ("background: Alpha — syncing docs 35/120") that the modal keeps live
// through phase === 'done' (the two sub-event listeners stay alive until the
// modal closes). Row behaviour for RUNNING rows is unchanged — the footer
// only RENDERS entries whose row is terminal (see backgroundActivityLines),
// so a running project's activity shows once (on its row), never twice.

/** project_id → latest in-flight background sub-label. */
export type BackgroundActivity = Record<string, string>;

/** The empty starting map (reset every run / on modal open). */
export function emptyBackgroundActivity(): BackgroundActivity {
  return {};
}

/**
 * Fold one sub-event's label into the background-activity map, returning a
 * NEW map (never mutates the input). Rules:
 *   - a non-null label sets/updates the project's entry — row status is
 *     IRRELEVANT here (that is the whole point vs `applySubProgress`);
 *   - a null label (terminal sub-event: status !== 'running') CLEARS the
 *     project's entry, so a finished embed drops off the footer;
 *   - identity no-op when nothing changes (repeat label, or clearing an
 *     absent entry) so reactive consumers don't re-render on repeats.
 */
export function applyBackgroundActivity(
  state: BackgroundActivity,
  projectId: string,
  label: string | null,
): BackgroundActivity {
  if (label === null) {
    if (!(projectId in state)) return state; // nothing to clear — identity
    const next = { ...state };
    delete next[projectId];
    return next;
  }
  if (state[projectId] === label) return state; // identity no-op
  return { ...state, [projectId]: label };
}

/**
 * Render-ready footer lines ("<project name> — <label>"). Resolves each
 * project's display name from the progress rows and SKIPS:
 *   - unknown project_ids (a sub-event for a project outside this run);
 *   - projects whose row is still 'running' — their activity already renders
 *     as the row's own sub-line; the footer only covers what the row rules
 *     drop (terminal rows, i.e. post-`finished` background embeds).
 * Deterministic: preserves the map's insertion order; no clock/randomness.
 */
export function backgroundActivityLines(
  bg: BackgroundActivity,
  rows: ProgressRow[],
): string[] {
  const byId = new Map(rows.map((r) => [r.project_id, r]));
  const lines: string[] = [];
  for (const [projectId, label] of Object.entries(bg)) {
    const row = byId.get(projectId);
    if (!row) continue; // not a project in this run's report
    if (row.status === 'running') continue; // row sub-line already covers it
    lines.push(`${row.project_name} — ${label}`);
  }
  return lines;
}
