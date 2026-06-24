// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Module catalog tile — per-status display contract.
//
// v0.2.35 (Agent J): centralised gating logic so the Svelte tile renderer
// and the unit tests share one source of truth. Previously the gating
// lived inline in `ModuleCatalog.svelte`'s `card-footer` template and
// conflated "row exists in module_installs" with "module is healthy" —
// the result was that rows with status='error' (and the reconciler's
// 'broken' status added in v0.2.33) only offered an "Uninstall" CTA.
// The user had to uninstall first before they could try again, which is
// unintuitive — especially in the post-update validation flow where an install
// failure mid-pipeline is the common case the retry button should fix.
//
// The Rust backend (Agent A's v0.2.34 UPSERT fix in commands/modules.rs)
// already supports calling `install_module_for_project` on top of an
// existing error row: the row is overwritten with the new attempt
// instead of triggering a UNIQUE-constraint crash. This helper just
// exposes that capability through the GUI.
//
// Pure / dependency-free so the tile renderer can stay declarative and
// so we can unit-test the gating exhaustively without a Svelte runtime.

import type { ModuleCatalogEntry, ModuleInstallRow, ModuleStatus } from '$lib/types/launcher';

/**
 * Discriminated union describing what the catalog tile should render
 * for a given (catalog entry, install row) pair.
 *
 * - `bundled` / `included` — orchestrator-shipped components (launcher
 *   itself, KG, code graph). No install/uninstall affordance.
 * - `installing` — a v0.2.35 install attempt is in flight (the store's
 *   `installingId === module.id`); render the spinner, no buttons.
 * - `installed` — happy path. Enabled toggle + optional Update +
 *   Uninstall, plus dashboard CTA if `cta_route` is present.
 * - `error` / `broken` — failed install or reconciler-detected missing
 *   on-disk artifact. Render BOTH a primary "Retry install" button
 *   (which dispatches the same `install_module_for_project` Tauri
 *   command — the UPSERT contract handles the overwrite) AND a
 *   secondary "Uninstall" button. The `last_error` string is shown
 *   above the buttons with explicit labelling.
 * - `available` — not yet installed; render the primary "Install"
 *   button (or "Activate license" when tier-gated and unlicensed).
 */
export type TileDisplay =
  | { kind: 'bundled' }
  | { kind: 'included'; parent_id: string; cta_route: string }
  | {
      kind: 'installing';
      // v0.2.67: optional live-progress label (e.g. "Pulling image… 40%")
      // sourced from the store's `installProgress[module_id]`. `null` when
      // no progress event has arrived yet (render the bare spinner).
      progress: string | null;
    }
  | {
      kind: 'installed';
      install_row: ModuleInstallRow;
      can_update: boolean;
    }
  | {
      kind: 'errored';
      // 'error' = install pipeline failed; 'broken' = reconciler found
      // the on-disk manifest missing. Same UI contract (Retry + Uninstall)
      // because the Tauri commands are identical, but the badge wording
      // differs so the user understands the failure category.
      status: 'error' | 'broken';
      install_row: ModuleInstallRow;
      last_error: string | null;
    }
  | { kind: 'available'; needs_license: boolean };

/**
 * Best-effort semver comparison. Splits on '.', parses the leading
 * integer of each segment (so "0.2.4-dev" → 0.2.4) and compares
 * lexicographically. Returns true iff `a` is strictly less than `b`.
 *
 * Duplicated here (rather than imported from ModuleCatalog.svelte) so
 * the helper is self-contained for unit-testing.
 */
export function semverLess(a: string, b: string): boolean {
  const parse = (v: string): number[] =>
    v.split('.').map((s) => {
      const match = s.match(/^(\d+)/);
      return match ? parseInt(match[1], 10) : 0;
    });
  const aa = parse(a);
  const bb = parse(b);
  for (let i = 0; i < Math.max(aa.length, bb.length); i++) {
    const x = aa[i] ?? 0;
    const y = bb[i] ?? 0;
    if (x < y) return true;
    if (x > y) return false;
  }
  return false;
}

/**
 * v0.2.67: the live install-progress shape the store records per module
 * (`installProgress[module_id]`). Re-declared structurally here (rather
 * than imported from the store) so this module stays free of Svelte/store
 * imports and remains a pure, unit-testable helper.
 */
export interface TileInstallProgress {
  stage: string;
  percent: number;
  message: string;
  failed: boolean;
}

/**
 * v0.2.67: human-readable label for an in-flight install, derived from
 * the latest `module://install-progress` event the store recorded.
 *
 * Returns `null` when there's no progress yet (render the bare spinner)
 * or for the terminal `done`/`failed` stages (the install row's own
 * status drives the display once those arrive). For every intermediate
 * stage we render the backend `message` plus a percent when it's a
 * meaningful 1–99 (0 and 100 are noise at the stage boundaries).
 */
export function installProgressLabel(
  progress: TileInstallProgress | null | undefined,
): string | null {
  if (!progress) return null;
  // Terminal stages are handled by the row's status, not the spinner.
  if (progress.stage === 'done' || progress.stage === 'failed') return null;
  const msg = (progress.message ?? '').trim();
  const base = msg.length > 0 ? msg : 'Installing';
  if (progress.percent > 0 && progress.percent < 100) {
    return `${base}… ${progress.percent}%`;
  }
  return `${base}…`;
}

/**
 * Resolve which display branch the tile should render.
 *
 * Inputs:
 * - `entry`: the catalog manifest row (carries `kind`, version, license).
 * - `installRow`: the per-project install row from `module_installs`,
 *   or null if not installed yet.
 * - `isInstalling`: true when an install/update RPC is in flight for
 *   THIS module (store's `installingId === entry.id`). Takes priority
 *   over every other state so the spinner is honest.
 * - `progress` (v0.2.67): the store's latest `installProgress[entry.id]`
 *   for this module, used to render a live label on the `installing`
 *   branch. Optional — omitted → bare spinner.
 *
 * Ordering is significant — see the comments above each branch.
 */
export function resolveTileDisplay(
  entry: ModuleCatalogEntry,
  installRow: ModuleInstallRow | null,
  isInstalling: boolean,
  progress?: TileInstallProgress | null,
): TileDisplay {
  // Branch 0 — bundled / orchestrator-shipped catalog kinds.
  // These predate the install pipeline; they never go through
  // module_installs and never carry a status.
  if (entry.kind === 'bundled') {
    return { kind: 'bundled' };
  }
  if (entry.kind === 'subcomponent') {
    return {
      kind: 'included',
      parent_id: entry.parent_id,
      cta_route: entry.cta_route,
    };
  }

  // Branch 1 — an install/retry is mid-flight. Honour the spinner
  // regardless of the row's current status (the row may still say
  // 'error' from the previous attempt until the Rust side commits).
  //
  // v0.2.67: if a terminal `failed` stage already arrived, fall through
  // to the errored branch immediately (the row's status will catch up,
  // but the user sees the failure now rather than a hanging spinner).
  if (isInstalling && !(progress && progress.failed)) {
    return { kind: 'installing', progress: installProgressLabel(progress) };
  }

  // Branch 2 — no row yet → available for install.
  if (!installRow) {
    return {
      kind: 'available',
      needs_license: !!entry.license_required && !entry.is_licensed,
    };
  }

  // Branch 3 — error / broken. Retry + Uninstall + last_error.
  if (installRow.status === 'error' || installRow.status === 'broken') {
    return {
      kind: 'errored',
      status: installRow.status,
      install_row: installRow,
      last_error: installRow.last_error,
    };
  }

  // Branch 4 — installing status from the DB but no in-flight RPC.
  // Treat as "installing" too so the spinner stays put even if the
  // store-side flag races against an event from another window.
  if (installRow.status === 'installing') {
    return { kind: 'installing', progress: installProgressLabel(progress) };
  }

  // Branch 5 — installed / running / stopped → installed (happy path).
  return {
    kind: 'installed',
    install_row: installRow,
    can_update: semverLess(installRow.module_version, entry.version),
  };
}

/**
 * Truncate a `last_error` payload for inline display next to the
 * Retry button. The full string is still available for the
 * `<details>` expand path; this is just for the at-a-glance summary.
 *
 * 120 chars matches the typical tile width at 1× zoom without
 * wrapping over two lines. Falls back to the original string when
 * shorter than the budget.
 */
export const LAST_ERROR_TRUNCATE_BUDGET = 120;

export function truncateLastError(
  msg: string | null | undefined,
  budget: number = LAST_ERROR_TRUNCATE_BUDGET,
): { display: string; truncated: boolean } {
  if (!msg) return { display: '', truncated: false };
  const trimmed = msg.replace(/\s+/g, ' ').trim();
  if (trimmed.length <= budget) {
    return { display: trimmed, truncated: false };
  }
  return {
    display: trimmed.slice(0, budget) + '…',
    truncated: true,
  };
}

/**
 * Map a `ModuleStatus` string to a short human-readable label for the
 * tile's error badge. Kept here rather than in the .svelte file so
 * future status additions only need one site to update.
 */
export function statusBadgeLabel(status: ModuleStatus): string {
  switch (status) {
    case 'installing':
      return 'Installing';
    case 'installed':
      return 'Installed';
    case 'running':
      return 'Running';
    case 'stopped':
      return 'Stopped';
    case 'error':
      return 'Install failed';
    case 'broken':
      return 'Files missing';
  }
}

/**
 * Catalog-card action contract — single source of truth for "which action
 * button does an actionable module-catalog `kind` expose, and which store
 * method performs it".
 *
 * Why this exists: the `/modules` ModuleCatalog tile wired per-status
 * buttons (Retry / Update / Start) inline, but the Home Library grid
 * (routes/+page.svelte) and the right detail panel (RightSidebar.svelte)
 * rendered the SAME status badge ("Reinstall needed", "Update available")
 * as a dead, non-interactive label — so a user seeing "RL Reranker —
 * Reinstall needed" on Home had no way to act on it there. This helper
 * lets every surface derive the same {label, method} from the catalog
 * `kind`, so the action button is consistent everywhere and a future
 * `kind` can't regress one surface silently.
 *
 * NOT Pro-gated: an actionable kind (broken/error/update_available) is by
 * definition an already-installed module that already passed the license
 * gate at install time, so reinstall/retry/update needs no fresh tier
 * check (and `install_module_for_project` / `update_module_for_project`
 * enforce tier server-side anyway as a backstop). The only requirement is
 * that a project is selected, because install/update are per-project.
 * The license gate stays where it belongs — on the not-yet-installed
 * `available` kind, handled separately by the install flow.
 *
 * Returns `null` for kinds that have no catalog-level action (bundled,
 * installed-and-current, subcomponent, coming_soon, available — the last
 * goes through the install/activate flow, not this repair path).
 */
export type ModuleCatalogActionMethod = 'install' | 'update';

export interface ModuleCatalogAction {
  /** Button label shown to the user. */
  label: string;
  /** `modules` store method to invoke (both take (projectId, moduleId)). */
  method: ModuleCatalogActionMethod;
}

export function moduleActionForKind(
  kind: string | null | undefined,
): ModuleCatalogAction | null {
  switch (kind) {
    case 'broken':
      // Reconciler found the on-disk manifest missing — same UPSERT-safe
      // command as a retry, different verb the user understands.
      return { label: 'Reinstall', method: 'install' };
    case 'error':
      return { label: 'Retry install', method: 'install' };
    case 'update_available':
      return { label: 'Update', method: 'update' };
    default:
      return null;
  }
}

/**
 * Detect whether a just-attempted install/update actually failed.
 *
 * Why this exists (2026-06-06, found via live test): the backend
 * `install_module_for_project` / `update_module_for_project` commands
 * resolve with a row whose `status` is still `'installed'` and
 * `last_error` is `null` even when the container START failed afterwards
 * (e.g. RL reranker: docker run exit 125, unauthorized). The real error
 * only materialises one tick later, when `loadCatalog()` recomputes the
 * catalog entry's `kind` to `'error'`/`'broken'`. So inspecting the
 * resolved row alone (the previous fix) misses the failure on Windows/
 * Docker — the row is clean. Callers must instead RELOAD the catalog and
 * pass the fresh entries here.
 *
 * @param moduleId       the module that was just installed/updated
 * @param catalog        the freshly reloaded catalog entries (after loadCatalog)
 * @param installedRows  the freshly reloaded installed rows (carry last_error)
 * @returns a human-readable error message if the module ended up in an
 *          error/broken state, otherwise `null` (success).
 */
export function detectModuleErrorAfterAction(
  moduleId: string,
  catalog: ReadonlyArray<{ id: string; kind: string }>,
  installedRows: ReadonlyArray<{ module_id: string; last_error: string | null; status?: string }>,
): string | null {
  const entry = catalog.find((m) => m.id === moduleId);
  const row = installedRows.find((r) => r.module_id === moduleId);
  const kindIsError = entry?.kind === 'error' || entry?.kind === 'broken';
  const rowIsError =
    row?.status === 'error' || row?.status === 'broken' || !!row?.last_error;
  if (!kindIsError && !rowIsError) return null;
  return (
    row?.last_error ??
    `install did not complete (status: ${entry?.kind ?? row?.status ?? 'unknown'})`
  );
}
