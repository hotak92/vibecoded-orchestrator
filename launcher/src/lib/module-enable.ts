// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.91 decision #23 (F-3 / F-4) — decision logic for the module-enable
// controls, per-project AND host-wide.
//
// Everything here is pure: no `invoke`, no Svelte, no DOM. The repo has no
// jsdom, so `.svelte` files are not unit-testable — this module is where the
// controls' real decisions live so they CAN be tested (same split as
// `dual-flags.ts`, `deferral-ledger.ts`, `update-all-progress.ts`).
//
// ## Two mechanisms, routed by the module's SCOPE
//
// "Is this module on for this project" has TWO backends, and which one
// answers depends on `install.scope` in the module's manifest:
//
//   * `per_project` → `module_installs.enabled`, written by
//     `set_module_enabled_v2`. One install row per project; the row's column
//     IS the gate. Unchanged by #23.
//   * `global`      → `module_settings.enabled_for_project`, resolved by the
//     three-tier cascade `Db::module_effective_enabled`. One install row for
//     the whole host, so a per-project column would have nothing to live on.
//
// That split is the documented design (`db/settings.rs`'s
// MODULE_ENABLED_FOR_PROJECT_KEY docstring). What was broken is that the GUI
// did not honour it: the tile's "Enabled" checkbox wrote
// `module_installs.enabled` for EVERY module, and for a global-scope module
// the gate the MCP consults never reads that column. Unchecking it on the RL
// tile flipped a checkbox, toasted success, and left reranking exactly as it
// was — a silent no-op with positive feedback (F-3).
//
// ## Why the global-scope control is a THREE-way choice
//
// The cascade has three tiers: an explicit per-project row (which wins IN
// BOTH DIRECTIONS), then a host-wide default row, then fail-open `true`. So
// "no row" and "row set to false" are different states with different
// futures, and a checkbox rendered from the EFFECTIVE value cannot tell them
// apart. The third position ("Use host-wide default") DELETES the row, which
// is also the only way back to inheriting once a user has clicked anything —
// the dead end F-4 found at BOTH tiers.
//
// Vocabulary is borrowed verbatim from `dual-flags.ts` on purpose: two panels
// in one app that mean the same thing must not word it differently.

/** Mirror of the Rust `ModuleEnableSource` wire shape. */
export type ModuleEnableSource = 'project' | 'global_default' | 'system_default';

/** Mirror of the Rust `ModuleEnableState` wire shape. */
export interface ModuleEnableState {
  /** Explicit per-project row, or `null` when inheriting. */
  explicit: boolean | null;
  /** Host-wide default row, or `null` when none is set. */
  global_default: boolean | null;
  /** What the hub resolver serves. */
  effective: boolean;
  source: ModuleEnableSource;
}

/** The three positions of the per-project segmented control. */
export type ModuleTriChoice = 'inherit' | 'on' | 'off';

/**
 * Which mechanism a module's per-project control must drive.
 *
 * `''` (legacy payloads from pre-v0.2.49 launchers) is treated as
 * `per_project`, matching the renderer's existing back-compat rule for
 * `install_scope`.
 */
export type ModuleInstallScope = 'per_project' | 'global' | '';

export function enableMechanismFor(
  scope: ModuleInstallScope | undefined,
): 'install_row' | 'project_setting' {
  return scope === 'global' ? 'project_setting' : 'install_row';
}

/**
 * Whether a catalog tile should render the per-project CASCADE control.
 *
 * The subtlety that decides where the control has to be rendered: a
 * global-scope module has ONE install row with `project_id IS NULL`, and
 * `list_installed_modules(project_id)` selects `project_id = ?`. So for the
 * ordinary case — module installed once for the host, viewed from a project
 * that never had its own row — the tile has NO install row and renders as
 * "available" with an `Installed globally` badge. Gating the control on the
 * tile's `installed` branch alone would leave it invisible in exactly the
 * situation it exists for.
 *
 * The catalog `kind` is the HOST-WIDE install signal (the same one
 * `module-per-project-display` reads for "installed anywhere"), so it is the
 * right input here. `broken` is excluded: a broken install has nothing to
 * gate, and that tile offers Reinstall instead.
 */
export function showsProjectSettingControl(entry: {
  install_scope?: ModuleInstallScope;
  kind?: string;
}): boolean {
  if (enableMechanismFor(entry.install_scope) !== 'project_setting') return false;
  return entry.kind === 'installed' || entry.kind === 'update_available';
}

/**
 * Whether a tile that HAS a per-project install row must still use the
 * cascade control rather than the `module_installs.enabled` checkbox.
 *
 * Keyed on SCOPE alone, deliberately. The catalog `kind` can disagree with a
 * project's own row (a host-wide `broken` beside a locally-installed row, a
 * catalog refresh mid-flight); if that ever happens for a global-scope
 * module, falling back to the checkbox would restore the exact placebo F-3
 * describes — a control that writes a column the gate never reads. The
 * mechanism follows the module's scope, never the tile's render state.
 */
export function installedTileUsesCascade(entry: {
  install_scope?: ModuleInstallScope;
}): boolean {
  return enableMechanismFor(entry.install_scope) === 'project_setting';
}

/** Labels for the three segments. Same words as the dual-flags panel. */
export const MODULE_TRI_CHOICE_LABELS: Readonly<Record<ModuleTriChoice, string>> = {
  inherit: 'Use host-wide default',
  on: 'On',
  off: 'Off',
};

/** Which segment is active for a resolved state. */
export function moduleTriChoiceFor(state: ModuleEnableState): ModuleTriChoice {
  if (state.explicit === null) return 'inherit';
  return state.explicit ? 'on' : 'off';
}

/**
 * The value the backend setter takes for a chosen segment. `null` means
 * DELETE the per-project row (back to inheriting).
 */
export function moduleTriChoiceToValue(choice: ModuleTriChoice): boolean | null {
  if (choice === 'inherit') return null;
  return choice === 'on';
}

export interface ModuleEnableBadge {
  label: string;
  /** `user` = chosen here (teal), `auto` = inherited (purple). */
  kind: 'user' | 'auto';
}

/** Provenance badge for a per-project control. */
export function moduleBadgeFor(state: ModuleEnableState): ModuleEnableBadge {
  return state.source === 'project'
    ? { label: 'this project', kind: 'user' }
    : { label: 'host default', kind: 'auto' };
}

/**
 * The one-line "what is in force, and why" statement under the per-project
 * control. Never says only "On"/"Off": a value without its cause is what
 * makes an inherited default read as a local choice.
 */
export function moduleEffectiveLine(state: ModuleEnableState): string {
  if (state.source === 'project') {
    return state.effective
      ? 'On — set for this project.'
      : 'Off — set for this project.';
  }
  if (state.source === 'global_default') {
    return state.effective
      ? 'On — following the host-wide default.'
      : 'Off — following the host-wide default.';
  }
  return 'On — nothing set for this project and no host-wide default (modules default to on).';
}

/**
 * The host-wide panel's own state line. `null` = no override row, which is a
 * real, persistent state (every module on a fresh install), NOT a transient
 * loading value — so it gets a named third position rather than "neither
 * button highlighted".
 */
export function globalDefaultLine(globalEnabled: boolean | null): string {
  if (globalEnabled === null) {
    return 'No host-wide choice set — modules are on unless a project says otherwise.';
  }
  return globalEnabled
    ? 'On for every project that has not made its own choice.'
    : 'Off for every project that has not made its own choice.';
}

/** The three positions of the host-wide control. */
export type GlobalTriChoice = 'default' | 'on' | 'off';

export const GLOBAL_TRI_CHOICE_LABELS: Readonly<Record<GlobalTriChoice, string>> = {
  default: 'Use system default',
  on: 'Enabled',
  off: 'Disabled',
};

export function globalTriChoiceFor(globalEnabled: boolean | null): GlobalTriChoice {
  if (globalEnabled === null) return 'default';
  return globalEnabled ? 'on' : 'off';
}

/** `null` means CLEAR the host-wide row (F-4's way back at this tier). */
export function globalTriChoiceToValue(choice: GlobalTriChoice): boolean | null {
  if (choice === 'default') return null;
  return choice === 'on';
}

// ─── Dormant modules — say what is TRUE today (USER rider, decision #23) ──
//
// A toggle whose "On" position promises behaviour the product does not
// perform yet is the same dishonesty as a toggle that writes the wrong
// table: the user reads "On" as "this is happening". The RL reranker's
// switch is real and its plumbing works end-to-end, but nothing reranks
// today because no trained model has been produced — so every surface that
// renders the switch also states that.
//
// This is a PRODUCT-STATE fact, not a computed one: there is no "has a
// trained model" probe to read, and inventing one that guesses would be
// worse than saying plainly what is true. Delete the entry the moment a
// trained model ships — a stale dormancy notice is its own lie.
export const DORMANT_MODULE_NOTICES: Readonly<Record<string, string>> = {
  'vct-rl-reranker':
    'Reranking is not live yet: no trained model has been produced, so search ' +
    'results are unaffected whichever way this is set. The switch controls ' +
    'whether the reranker WOULD be consulted once a model exists. Training-event ' +
    'collection is separate and continues either way.',
};

/**
 * The dormancy notice for a module, or `null` when the module is not in the
 * dormant set. Rendered next to the control, never instead of it — the
 * setting is real and persists; it is the EFFECT that is pending.
 */
export function dormantNotice(moduleId: string): string | null {
  return DORMANT_MODULE_NOTICES[moduleId] ?? null;
}

/** Short badge form for a tile, where a paragraph does not fit. */
export function dormantBadgeLabel(moduleId: string): string | null {
  return dormantNotice(moduleId) === null ? null : 'not reranking yet';
}
