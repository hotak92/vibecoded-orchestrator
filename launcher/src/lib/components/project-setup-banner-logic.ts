// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.95 R7 — pure view-model logic for the post-add / post-adopt project
// setup banner (`ProjectSetupBanner.svelte` → `OperationProgressBanner`).
//
// Extracted for the same reason as `kg-sync-banner-logic.ts` and
// `codegraph-build-banner-logic.ts`: the stage vocabulary, the auto-hide
// window and the "which affordance does this state get" decision were inline
// in a `$derived.by` and therefore untestable without mounting the component
// (the launcher's vitest run is a `node` environment — no DOM, no component
// rendering). With the decision here, "shows the bundle stage", "hides once
// setup completes", "a failure keeps its error text and offers Retry" are
// real asserts rather than a source scan.

import type {
  ProjectSetupPhase,
  ProjectSetupStatus,
  SetupWarning,
} from '$lib/types/launcher';
// Type-only import: erased at build time, so unit tests never load the store
// module (which registers a Tauri `listen` at import).
import type { ActiveSetup } from '$lib/stores/project-setup';

/** How long a terminal `done` / `deferred` banner stays before it hides
 *  itself. `failed` never auto-hides — it waits for Retry or Dismiss. */
export const SETUP_BANNER_HIDE_TERMINAL_AFTER_MS = 30_000;

/** The banner's own status vocabulary: `pending` (queued behind another add)
 *  presents as `running`, because to the user it IS the operation running. */
export type SetupBannerStatus = 'running' | 'deferred' | 'done' | 'failed';

export interface SetupBannerView {
  /** Headline — carries the project NAME the user is waiting on. */
  title: string;
  /** Plain-language stage line. */
  phaseLabel: string;
  status: SetupBannerStatus;
  /** Reassurance / elapsed / terminal copy. */
  detail?: string;
  /** Failure text, verbatim from the backend (failed only). */
  error?: string | null;
  warnings: SetupWarning[];
  /** Retry is offered on failure only — never on a deferral, which is
   *  informational (the deferred work catches up by itself). */
  canRetry: boolean;
  /** Terminal, non-failure states can be dismissed early. */
  canDismiss: boolean;
}

export function setupElapsedLabel(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s elapsed`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}m ${rem}s elapsed`;
}

export function setupPhaseLabel(
  status: ProjectSetupStatus,
  phase: ProjectSetupPhase | null,
): string {
  if (status === 'pending') return 'Queued…';
  if (status === 'running') {
    switch (phase) {
      case 'bootstrap':
        return 'Creating knowledge collections…';
      case 'bundle':
        return 'Installing project bundle (hooks, scripts, agents)…';
      case 'post_bundle':
        return 'Indexing — continues in the background…';
      default:
        return 'Setting up…';
    }
  }
  if (status === 'deferred')
    return 'Knowledge collections will be created when Weaviate is ready.';
  if (status === 'done') return 'Setup complete.';
  return 'Setup failed.';
}

/**
 * Fold the active setup + queue depth into what the banner renders.
 *
 * Returns `null` when nothing should be shown: no setup observed this
 * session, or a `done` / `deferred` setup whose visible window has expired.
 *
 * @param active   latest setup observed by the `project-setup` store
 * @param queued   how many adds are waiting behind it
 * @param now      ms-epoch "now" (injected so the auto-hide is testable)
 */
export function buildSetupBannerView(
  active: ActiveSetup | null,
  queued: number,
  now: number,
): SetupBannerView | null {
  if (!active) return null;

  if (
    (active.status === 'done' || active.status === 'deferred') &&
    now - active.observed_at >= SETUP_BANNER_HIDE_TERMINAL_AFTER_MS
  ) {
    return null;
  }

  const status: SetupBannerStatus =
    active.status === 'pending' ? 'running' : active.status;

  // The project name is the headline; the queue count rides along when adds
  // are waiting behind this one.
  const title =
    queued > 0
      ? `Adding ${active.project_name} — ${queued} queued`
      : `Setting up ${active.project_name}`;

  let detail: string | undefined;
  if (active.status === 'running' || active.status === 'pending') {
    detail =
      `Project saved — setup (hooks, indexing) finishes in the background · ` +
      setupElapsedLabel(now - active.observed_at);
  } else if (active.status === 'deferred') {
    detail = 'Your project is ready to use now; the deferred work catches up automatically.';
  } else if (active.status === 'done') {
    detail = 'Hooks installed, knowledge collections ready.';
  }

  return {
    title,
    phaseLabel: setupPhaseLabel(active.status, active.phase),
    status,
    detail,
    error: active.error,
    warnings: active.warnings,
    canRetry: active.status === 'failed',
    canDismiss: active.status === 'done' || active.status === 'deferred',
  };
}

/**
 * Does a live setup need the 1Hz clock? True while the operation runs, and
 * for one extra second past the auto-hide window so the last tick actually
 * removes the banner.
 */
export function setupBannerNeedsTick(active: ActiveSetup | null, now: number): boolean {
  if (!active) return false;
  const live = active.status === 'running' || active.status === 'pending';
  return (
    live || now - active.observed_at < SETUP_BANNER_HIDE_TERMINAL_AFTER_MS + 1000
  );
}
