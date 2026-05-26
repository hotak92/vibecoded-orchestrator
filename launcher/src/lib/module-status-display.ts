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
// unintuitive — especially in the dogfooding flow where an install
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
  | { kind: 'installing' }
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
 * Resolve which display branch the tile should render.
 *
 * Inputs:
 * - `entry`: the catalog manifest row (carries `kind`, version, license).
 * - `installRow`: the per-project install row from `module_installs`,
 *   or null if not installed yet.
 * - `isInstalling`: true when an install/update RPC is in flight for
 *   THIS module (store's `installingId === entry.id`). Takes priority
 *   over every other state so the spinner is honest.
 *
 * Ordering is significant — see the comments above each branch.
 */
export function resolveTileDisplay(
  entry: ModuleCatalogEntry,
  installRow: ModuleInstallRow | null,
  isInstalling: boolean,
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
  if (isInstalling) {
    return { kind: 'installing' };
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
    return { kind: 'installing' };
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
