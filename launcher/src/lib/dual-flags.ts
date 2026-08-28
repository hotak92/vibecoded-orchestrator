// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.91 WP-L (plan decision #22) — decision logic for the dual
// embedding / RL-logging flags panel.
//
// Everything here is pure: no `invoke`, no Svelte, no DOM. The repo has no
// jsdom, so `.svelte` files are not unit-testable — this module is where the
// panel's real decisions live so they CAN be tested (same split as
// `deferral-ledger.ts`, `mcp-maintenance-logic.ts`, `update-all-progress.ts`).
// `DualWriteFlagsPanel.svelte` is markup over it.
//
// ## Why the per-project control is a THREE-way choice
//
// Each flag now resolves through three tiers: an explicit per-project row
// (which wins IN BOTH DIRECTIONS), then a host-wide default, then `false`.
// So "no row" and "row set to false" are DIFFERENT states with different
// futures, and a checkbox cannot express that: a box rendered from the
// EFFECTIVE value, checked because of a host-wide default, is
// indistinguishable from a per-project choice. That is the lying toggle this
// module exists to prevent. The third option ("Use host-wide default")
// DELETES the row, which is also the only way back to inheriting once a user
// has clicked anything.
//
// ## Vocabulary (fixed — do not paraphrase)
//
// The Preferences page already carries global toggles labelled
// "— GLOBAL (all projects)" whose semantics are a HARD OVERRIDE. WP-L's
// global tier is the opposite: a default that any explicit per-project
// choice beats. Two globals with inverted precedence on one page is a
// misreading waiting to happen, so:
//
//   * this tier is always "host-wide default" (never "GLOBAL", never
//     "install-wide", never "master switch");
//   * an inheriting project badges `host default`;
//   * an explicit project choice badges `this project`.
//
// Every scope-dependent string below derives from the `scope` argument, not
// from which page the component happens to be mounted on — the decision-#6
// rider that keeps two mounts of one component from lying about each other.

/** The three flags, by their backend wire names. */
export type DualFlagKey = 'write_all_slots' | 'rl_log' | 'arctic_secondary';

/** Provenance of a flag's STORED INTENT (not of the clamp — see `clamped`). */
export type DualFlagSource = 'project' | 'install_default' | 'system_default';

/** Mirror of the Rust `DualFlagState` wire shape. */
export interface DualFlagState {
  /** Explicit per-project row, or `null` when inheriting. */
  explicit: boolean | null;
  /** The host-wide default (`false` when no app_state row exists). */
  install_default: boolean;
  /** What consumers see, AFTER the log⟹write clamp. */
  effective: boolean;
  source: DualFlagSource;
  /**
   * `true` when the log⟹write dependency forced `effective` below what the
   * row/default alone would have produced. Only ever true for `rl_log`.
   */
  clamped: boolean;
}

/** Mirror of the Rust `DualFlagsState` wire shape. */
export interface DualFlagsState {
  write_all_slots: DualFlagState;
  rl_log: DualFlagState;
  arctic_secondary: DualFlagState;
}

/** Mirror of the Rust `DualFlagGlobalDefaults` wire shape. */
export interface DualFlagGlobalDefaults {
  write_all_slots: boolean;
  rl_log: boolean;
  arctic_secondary: boolean;
}

/** Which tier this panel mount edits. */
export type DualScope = 'project' | 'global';

/** The three positions of the per-project segmented control. */
export type TriChoice = 'inherit' | 'on' | 'off';

export interface DualFlagMeta {
  key: DualFlagKey;
  label: string;
  /** The env var the flag projects to — shown so the copy is verifiable. */
  envVar: string;
  description: string;
}

/**
 * Flag metadata, in the order the panel renders them. Dual-write first
 * because the RL log depends on it.
 */
export const DUAL_FLAGS: readonly DualFlagMeta[] = [
  {
    key: 'write_all_slots',
    label: 'Write embeddings to all named-vector slots',
    envVar: 'DUAL_EMBEDDING_WRITE_ALL_SLOTS',
    description:
      'The indexer writes every configured embedding slot (e.g. a secondary ' +
      'openai slot alongside the primary qwen3 slot) instead of just the ' +
      'active one. Costs extra embed calls per node.',
  },
  {
    key: 'rl_log',
    label: 'Log RL events under the secondary slot',
    envVar: 'DUAL_RL_LOG_ENABLED',
    description:
      'Retrieval-training events are also recorded against the secondary ' +
      'slot, so a reranker can later be trained on it. Requires dual-write ' +
      'above — there is no secondary slot to log into otherwise.',
  },
  {
    key: 'arctic_secondary',
    label: 'Write a secondary arctic embedding slot',
    envVar: 'DUAL_EMBEDDING_ARCTIC_SECONDARY',
    description:
      'The indexer also writes an arctic slot alongside the active one (a ' +
      'qwen3-active install can collect an arctic corpus for later ' +
      'reranking). Independent of the two above. Costs extra embed calls.',
  },
] as const;

/** Read one flag out of the resolved triple. */
export function flagState(
  flags: DualFlagsState,
  key: DualFlagKey,
): DualFlagState {
  return flags[key];
}

/**
 * A `scope="project"` mount without a project id can never resolve a project:
 * `load()` would call nothing and the panel would sit on "Loading…" forever.
 * No shipped mount does this, so this is insurance that renders an explicit
 * sentence instead of an indefinite spinner. (Same guard shape as
 * `deferral-ledger.ts::mountConfigError`.)
 */
export function mountConfigError(
  scope: DualScope,
  projectId: string | null | undefined,
): string | null {
  if (scope === 'project' && !projectId) {
    return (
      'This panel was mounted for a project but no project was given, so ' +
      'there is nothing to read or write. This is a wiring bug — the ' +
      'host-wide defaults live under Preferences.'
    );
  }
  return null;
}

/** Which segment of the tri-state control is active for a resolved flag. */
export function triChoiceFor(state: DualFlagState): TriChoice {
  if (state.explicit === null) return 'inherit';
  return state.explicit ? 'on' : 'off';
}

/**
 * The value the backend setter takes for a chosen segment. `null` means
 * DELETE the per-project row (back to inheriting) — without it the control is
 * a one-way door.
 */
export function triChoiceToValue(choice: TriChoice): boolean | null {
  if (choice === 'inherit') return null;
  return choice === 'on';
}

/** Labels for the three segments. Fixed vocabulary; do not paraphrase. */
export const TRI_CHOICE_LABELS: Readonly<Record<TriChoice, string>> = {
  inherit: 'Use host-wide default',
  on: 'On',
  off: 'Off',
};

export interface DualFlagBadge {
  label: string;
  /**
   * `user` = teal ("explicitly chosen here"), `auto` = purple ("inherited").
   * Matches `ActiveEmbeddingPicker`'s hue mapping, which is the semantically
   * closest shipped surface.
   */
  kind: 'user' | 'auto';
}

/**
 * Provenance badge for a per-project row. `null` on the global mount — there
 * is no tier above it to inherit from, so a badge there would be noise.
 */
export function badgeFor(
  scope: DualScope,
  state: DualFlagState,
): DualFlagBadge | null {
  if (scope !== 'project') return null;
  if (state.source === 'project') return { label: 'this project', kind: 'user' };
  return { label: 'host default', kind: 'auto' };
}

/**
 * The one-line "what is actually in force, and why" statement under each
 * control. Never says only "On"/"Off": a value without its cause is what
 * makes an inherited default read as a local choice.
 */
export function effectiveLine(
  scope: DualScope,
  state: DualFlagState,
): string {
  if (scope === 'global') {
    return state.effective
      ? 'On for every project that has not made its own choice.'
      : 'Off for every project that has not made its own choice.';
  }
  if (state.clamped) {
    // Only reachable for rl_log, and only when its prerequisite resolved off.
    return 'Off — RL dual-logging needs dual-write, which is off for this project.';
  }
  if (state.source === 'project') {
    return state.effective ? 'On — set for this project.' : 'Off — set for this project.';
  }
  if (state.source === 'install_default') {
    return state.effective
      ? 'On — following the host-wide default.'
      : 'Off — following the host-wide default.';
  }
  return 'Off — nothing set for this project, and no host-wide default.';
}

/** Panel heading. Derived from the scope, never from the mount site. */
export function panelTitle(scope: DualScope): string {
  return scope === 'global'
    ? 'Dual embedding / RL logging — host-wide defaults'
    : 'Dual embedding / RL logging (this project)';
}

/** Panel intro paragraph. */
export function panelIntro(scope: DualScope): string {
  if (scope === 'global') {
    return (
      'Applies to every project that has not made its own choice. A project ' +
      'that has chosen keeps its choice — including an explicit off while ' +
      'the default here is on. All three default OFF.'
    );
  }
  return (
    "This project's own choices, stored in the launcher database (not a " +
    'bundled file) — they survive bundle / orchestrator updates. Leave a ' +
    'flag on "Use host-wide default" to follow the install-wide setting in ' +
    'Preferences instead. All three default OFF.'
  );
}

/**
 * The cascade footnote. Two sentences, because the dual-log prerequisite is
 * the part users get wrong.
 */
export function cascadeFootnote(scope: DualScope): string[] {
  const dependency =
    'RL dual-logging requires dual-write. Turning the log on turns dual-write ' +
    'on; turning dual-write off turns the log off.';
  if (scope === 'global') {
    return [
      'A project that has made its own choice keeps it — including an ' +
        'explicit off while this default is on. Projects that have never ' +
        'chosen follow the defaults above. When neither exists, the flag is off.',
      dependency + ' The same holds for these defaults.',
      'Changing a default here re-projects every registered project’s ' +
        '.claude/env, so inheriting projects pick it up immediately.',
    ];
  }
  return [
    'A choice here wins over the host-wide default in both directions — ' +
      'including an explicit off while the host-wide default is on.',
    dependency,
  ];
}

/**
 * Extra note under the RL-log row when picking "On" would also switch
 * dual-write on. The backend does this (it force-enables the prerequisite),
 * so the row must NOT be disabled — the old panel disabled the checkbox in
 * exactly the state where its own tooltip promised the auto-enable (F-6).
 *
 * Note this reads the RESOLVED dual-write value: under a host-wide
 * `write = true` the log is legitimately reachable with no per-project rows
 * at all.
 */
export function rlLogPrerequisiteNote(
  scope: DualScope,
  dualWriteOn: boolean,
): string | null {
  if (dualWriteOn) return null;
  return scope === 'global'
    ? 'Dual-write is off host-wide. Turning this on turns the dual-write default on too.'
    : 'Dual-write is off for this project. Turning this on turns dual-write on too.';
}

/**
 * Toast text after a host-wide default write. The panel must say what
 * happened to the OTHER projects — a control whose displayed state is ahead
 * of the effective state is the same class of dishonesty as a lying toggle.
 */
export function globalSaveSummary(
  refreshed: number,
  warnings: number,
): string {
  const base = `Host-wide default saved. Re-projected ${refreshed} project${
    refreshed === 1 ? '' : 's'
  }`;
  return warnings > 0 ? `${base} (${warnings} warning${warnings === 1 ? '' : 's'}).` : `${base}.`;
}
