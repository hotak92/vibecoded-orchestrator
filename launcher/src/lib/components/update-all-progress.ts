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

import type { KgSyncView } from '$lib/types/launcher';
// v0.2.92 (review MAJOR-9): the KG-sync phase VOCABULARY and the
// "an intentional skip is done" counting rule have ONE home — the module the
// banner + pill already classify through. This surface used to carry a second,
// older copy of both, so WP-B1's `finalize` stage and its skip-inclusive counts
// reached the banner and not the Update-all modal. Importing is the fix; a
// second copy here is what created the defect.
import { kgSyncDoneCount, kgSyncPhaseKind } from './kg-sync-banner-logic';

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
 * The `kg-sync-progress` event payload — an ALIAS of the shared `KgSyncView`
 * mirror, not a second hand-written subset.
 *
 * v0.2.92 (review MAJOR-9): this used to declare 7 of the Rust struct's
 * fields "because they're the ones we render". That is exactly how
 * `kg_skipped` / `docs_skipped` came to exist on the wire, be typed for the
 * banner, and be invisible here — a payload with two type homes drifts at the
 * one that nobody edits. `KgSyncView` in `$lib/types/launcher` is the home.
 */
export type KgSyncProgress = KgSyncView;

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
 * Files fully accounted for on ONE side of the sync (knowledge/ or docs/),
 * routed through the shared "an intentional skip is done" rule rather than
 * re-adding it here. The other side is zeroed because this label is
 * per-phase, where the banner's counter is whole-run.
 */
function sideDone(succeeded: number, skipped: number | undefined): number {
  return kgSyncDoneCount({
    kg_succeeded: succeeded,
    kg_skipped: skipped,
    docs_succeeded: 0,
    docs_skipped: 0,
  });
}

/**
 * Condensed sub-label for a `kg-sync-progress` event, or `null` when the event
 * is terminal / uninformative (so the row's sub-line clears). Deterministic:
 * no clock / randomness — the label is a pure function of the payload.
 *
 * v0.2.92 (review MAJOR-9) — three defects closed, all of them WP-B1's, all of
 * them present here only because this was a second copy of the banner's logic:
 *   * `finalize` had no case, so a run whose `.node_formats.json` regen takes
 *     up to 600 s sat on a completed-looking count for ten minutes;
 *   * skips were not counted as done, so 100 nodes with 60 archived froze the
 *     row at `40/100` — the census's own remedy ("Projects → Update all")
 *     demonstrating the bug the census reports;
 *   * an unrecognized phase fell through to the generic line rather than being
 *     named, hiding a new Rust stage instead of showing it.
 * Phase classification is `kgSyncPhaseKind`'s job — this function only chooses
 * wording, so a new stage string is one edit for every KG-sync surface.
 */
export function kgSyncSubLabel(evt: KgSyncProgress): string | null {
  // Only surface while the sync is actually running; a terminal status means
  // this phase is done and the row should stop showing a sub-line.
  if (evt.status !== 'running') return null;

  const kind = kgSyncPhaseKind(evt.current_phase);
  if (kind === 'scan') return 'scanning knowledge graph…';
  if (kind === 'queued') return 'waiting for the embed lane…';
  if (kind === 'finalize') return 'finalizing summaries…';
  // Neutral, never a confident wrong label: name the stage this build does not
  // know rather than calling it "syncing knowledge".
  if (kind === 'unknown') return `${evt.current_phase}…`;
  // Docs re-embedding is the long tail the Windows field report flagged as
  // "looks frozen" — show the running count so it visibly advances.
  if (kind === 'docs' && evt.docs_total > 0) {
    const done = sideDone(evt.docs_succeeded, evt.docs_skipped);
    return `syncing docs ${done}/${evt.docs_total}`;
  }
  if (kind === 'embed' && evt.kg_total > 0) {
    const done = sideDone(evt.kg_succeeded, evt.kg_skipped);
    return `syncing knowledge ${done}/${evt.kg_total}`;
  }
  // Running but no count yet: a generic-but-alive line beats a static spinner.
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

// ─── v0.2.91 decision #26 — modal close-gating ───────────────────────────
//
// `update_all_projects` performs a real bundle install per project. Before
// #26 the modal mounted `<DialogRoot bind:open>` with DialogRoot's defaults,
// so Escape or a backdrop click closed it MID-RUN; reopening and re-clicking
// then started a second concurrent run over the same folders, and the first
// run's resolve later overwrote the live run's `report`/`phase`. (The backend
// now refuses the second run outright — this half stops the user reaching
// that refusal by accident, and keeps the modal's own header claim honest.)
//
// The gate is deliberately NARROW: only while a run is in flight. A modal
// that cannot be dismissed in a TERMINAL state is the F-4-shaped dead end
// this wave is removing everywhere else — the report, including a failure
// report, must always be closable.
//
// Note DialogRoot's `onClose` is a NOTIFICATION, not a veto: gating it (the
// EnrichmentProgressModal mistake) does not stop the dialog closing, it only
// stops the parent hearing about it. `closeOnBackdrop` / `closeOnEscape` are
// the actual gates — see `OnboardingWizard.svelte`'s mount for the reference
// shape.

/** The modal's three phases, as the component models them. */
export type UpdateAllPhase = 'confirm' | 'running' | 'done';

/**
 * Whether the user may dismiss the modal in `phase`.
 *
 * `confirm` — yes, nothing has started. `running` — no, a destructive
 * traversal is in flight. `done` — yes, and this is load-bearing: BOTH the
 * success report and the error report land in `done`, so a gate that keyed
 * on "did it succeed" would strand a user on a failure they cannot close.
 */
export function updateAllModalDismissable(phase: UpdateAllPhase): boolean {
  return phase !== 'running';
}
