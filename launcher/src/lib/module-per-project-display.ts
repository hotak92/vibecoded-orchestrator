// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Bug D (v0.2.49) — Per-project status badge for catalog tiles.
//
// Why this exists
// ───────────────
// Pre-fix, the Modules tab made paid/installed modules look like they
// "disappeared" when the user switched to a project where they weren't
// installed. The catalog list itself was already sourced from the
// global `mState.catalog` (which carries the Rust-aggregated `kind`
// field — "this module is installed SOMEWHERE on the host"), but each
// TILE consulted only the per-current-project install row when
// deciding what to render. So a tile for a module installed in Project
// X but currently viewing Project Y would render as `kind='available'`
// with just an "Install" button — visually identical to a module never
// installed anywhere.
//
// User report (Fabio, 2026-06-07, V5 handoff §2):
//   "when another project is selected modules disappear. They
//    shouldn't, as they should be present/available for every
//    installed project and this should be clear in the GUI too."
//
// The list-source bug is a non-issue — `mState.catalog` is already
// global. What was missing was an explicit per-tile signal that
// communicates the per-project status independently from the install
// CTA shape.
//
// What this helper does
// ─────────────────────
// Given a catalog entry + the install row for the CURRENT project (or
// null if no row), this helper returns a discriminated union describing
// which badge the tile should render in the card header. The badge is
// purely informational — it does NOT change which CTA renders in the
// card footer (that stays driven by `resolveTileDisplay`).
//
// Why a pure helper (no DOM, no Tauri)
// ────────────────────────────────────
// Same rationale as `module-status-display.ts` (v0.2.35 Agent J):
// pure / dependency-free so the tile renderer can stay declarative
// and so we can unit-test the per-project gating exhaustively at the
// helper level without standing up a Svelte runtime + jsdom.
//
// The catalog `kind` carries the aggregate-install signal
// ────────────────────────────────────────────────────────
// Rust-side `lookup_install_state` (commands/modules.rs::702) walks
// every install row across every project and aggregates the result
// into the catalog entry's `kind`. So:
//
//   kind === 'installed'         → installed in at least one project
//                                  (possibly the current one — check
//                                  the per-project row to disambiguate)
//   kind === 'broken'            → installed somewhere, on-disk artifact
//                                  missing (reconciler-detected)
//   kind === 'update_available'  → installed in at least one project,
//                                  catalog has a newer version
//   kind === 'installed' (with current-project row null) →
//                                  installed in OTHER project(s) only
//
// `bundled` and `subcomponent` are not install-pipeline concerns —
// they're orchestrator-shipped components and surface their own
// dedicated badges.
//
// install_scope='global' branch (v0.2.49 amendment, post-cherry-pick)
// ──────────────────────────────────────────────────────────────────
// The original Bug D commit (c2d7215e) flagged but did NOT consume
// `install.scope`: for a `scope='global'` module installed once on
// the host, every project's `list_installed_modules(projectId)`
// returned no row, so the badge would render `installed-elsewhere`
// on every project — wrong, since "installed once = available to
// all" is global-scope's contract.
//
// RL chat landed the field on `ModuleCatalogEntry` (commit d0f7c03d:
// `feat(catalog): expose install.scope on ModuleCatalogEntry +
// L0Install`) which now flows through to the TS type as
// `install_scope?: 'per_project' | 'global' | ''`. The amendment
// here: when the catalog says `installedAnywhere` AND
// `install_scope === 'global'`, return the new `enabled-globally`
// badge variant instead of `installed-elsewhere`. The semantic:
// "available in every project; opt out via Stream B's per-project
// enable toggle." Pre-v0.2.49 launchers send empty `install_scope`;
// the renderer treats `""` ≡ `"per_project"` so legacy modules keep
// the original `installed-elsewhere` behavior.
//
// Resolution path documented in vct-coordination 2026-06-07 +
// HANDOFF-TO-MAIN-VCO-CHAT-2026-06-07-V5 + RL chat msg 181 ack.

import type { ModuleCatalogEntry, ModuleInstallRow } from '$lib/types/launcher';

/**
 * Per-project status badge variants. The renderer maps each variant
 * to a short label + a color class; the label text itself is built
 * here so the renderer stays purely declarative.
 *
 * Variants:
 *   - `bundled`              — orchestrator-shipped, no install pipeline.
 *   - `included`             — subcomponent of a parent module
 *                              (e.g. KG ships with the orchestrator).
 *   - `enabled-here`         — installed in current project, enabled.
 *   - `disabled-here`        — installed in current project, toggle off.
 *   - `installed-here`       — installed in current project, status not
 *                              `installed`/`running`/`stopped` (installing
 *                              or error/broken). The install row exists
 *                              but we don't drill into status here; the
 *                              body tile already surfaces the status
 *                              badge for those cases.
 *   - `installed-elsewhere`  — catalog says installed (in some project)
 *                              but no row for THIS project. THIS is the
 *                              variant that makes the "module didn't
 *                              disappear, it's just not in this project
 *                              yet" semantic explicit. Only emitted for
 *                              `install_scope='per_project'` (and legacy
 *                              empty-string) modules.
 *   - `enabled-globally`     — catalog says installed AND
 *                              `install_scope='global'`. The module is
 *                              installed ONCE on the host and is
 *                              available to every project; per-project
 *                              opt-out is via Stream B's enable toggle
 *                              (which surfaces as `enabled-here` /
 *                              `disabled-here` when a per-project
 *                              `module_enabled` row exists). v0.2.49.
 *   - `not-installed`        — catalog `kind='available'`, no row.
 */
export type PerProjectBadgeKind =
  | 'bundled'
  | 'included'
  | 'enabled-here'
  | 'disabled-here'
  | 'installed-here'
  | 'installed-elsewhere'
  | 'enabled-globally'
  | 'not-installed';

export interface PerProjectBadge {
  kind: PerProjectBadgeKind;
  /** Short human label (e.g. "Enabled here", "Installed elsewhere"). */
  label: string;
  /**
   * Tooltip copy explaining the badge meaning. Kept here (rather than
   * in the renderer) so a single label change touches one site.
   */
  tooltip: string;
}

/**
 * Resolve the per-project status badge for a tile.
 *
 * Inputs:
 *   - `entry`        — the catalog manifest row.
 *   - `installRow`   — the install row for the CURRENT project, or
 *                      `null` if no row exists.
 *
 * Returns one variant of `PerProjectBadge`. Ordering of branches is
 * significant — `bundled` / `subcomponent` short-circuit first
 * because they're not install-pipeline concerns.
 *
 * Defensive: when `entry` itself is missing fields (e.g. a partially-
 * populated test fixture), the helper falls back to `not-installed`
 * rather than throwing. The renderer can always trust the return.
 */
export function resolvePerProjectBadge(
  entry: ModuleCatalogEntry,
  installRow: ModuleInstallRow | null,
): PerProjectBadge {
  // Branch 0 — bundled / subcomponent: orchestrator-shipped.
  if (entry.kind === 'bundled') {
    return {
      kind: 'bundled',
      label: 'Bundled',
      tooltip: 'Ships with the orchestrator — always available in every project.',
    };
  }
  if (entry.kind === 'subcomponent') {
    return {
      kind: 'included',
      label: 'Included',
      tooltip: 'Ships with another module — managed via its parent.',
    };
  }

  // Branch 1 — install row exists for the current project. Discriminate
  // by enabled state so the user can see the per-project toggle position
  // at a glance.
  if (installRow) {
    // Only flip to enabled/disabled labels for the steady-state statuses
    // (installed/running/stopped). For installing/error/broken the body
    // tile already surfaces the lifecycle state — we just say "installed
    // here" so the badge is consistent across the same project.
    if (
      installRow.status === 'installed' ||
      installRow.status === 'running' ||
      installRow.status === 'stopped'
    ) {
      if (installRow.enabled) {
        return {
          kind: 'enabled-here',
          label: 'Enabled here',
          tooltip: 'Installed in this project and currently enabled.',
        };
      }
      return {
        kind: 'disabled-here',
        label: 'Disabled here',
        tooltip:
          'Installed in this project but the per-project enable toggle is off.',
      };
    }
    return {
      kind: 'installed-here',
      label: 'Installed here',
      tooltip:
        'Installed in this project. See the install state below for current status.',
    };
  }

  // Branch 2 — no row in this project. The catalog kind tells us
  // whether the module is installed elsewhere (`installed`,
  // `update_available`, `broken`) or genuinely not installed anywhere
  // (`available`, `coming_soon`).
  const installedAnywhere =
    entry.kind === 'installed' ||
    entry.kind === 'update_available' ||
    entry.kind === 'broken';
  if (installedAnywhere) {
    // v0.2.49 install_scope branch: a `global` module is installed
    // once for the whole host. "No row for this project" doesn't mean
    // "installed elsewhere" — it means "available to every project,
    // not yet opted in via Stream B's enable toggle." Empty-string /
    // missing install_scope is treated as `per_project` per the wire
    // shape's back-compat contract (legacy launchers pre-v0.2.49).
    if (entry.install_scope === 'global') {
      return {
        kind: 'enabled-globally',
        label: 'Installed globally',
        tooltip:
          'This module is installed once on this machine and available to every project. Per-project opt-out is on the module dashboard.',
      };
    }
    return {
      kind: 'installed-elsewhere',
      label: 'Installed elsewhere',
      tooltip:
        'This module is installed in another project on this machine. Click Install to add it to the current project too.',
    };
  }

  return {
    kind: 'not-installed',
    label: 'Not installed',
    tooltip:
      entry.kind === 'coming_soon'
        ? 'Announced — not yet shipped. Watch the catalog for updates.'
        : 'Not installed in any project on this machine.',
  };
}

/**
 * CSS class suffix for the badge variant. Kept centralised so the
 * `.svelte` renderer can spread one class binding and the visual
 * styling stays consistent across the badge family.
 *
 * The class name follows the BEM-ish suffix convention used by the
 * other badge variants in `ModuleCatalog.svelte` (`tier-badge`,
 * `status-badge-bundled`, `status-badge-included`).
 */
export function perProjectBadgeClass(kind: PerProjectBadgeKind): string {
  switch (kind) {
    case 'bundled':
      return 'pp-badge-bundled';
    case 'included':
      return 'pp-badge-included';
    case 'enabled-here':
      return 'pp-badge-enabled';
    case 'disabled-here':
      return 'pp-badge-disabled';
    case 'installed-here':
      return 'pp-badge-installed';
    case 'installed-elsewhere':
      return 'pp-badge-elsewhere';
    case 'enabled-globally':
      return 'pp-badge-global';
    case 'not-installed':
      return 'pp-badge-none';
  }
}
