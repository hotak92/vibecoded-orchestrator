// SPDX-License-Identifier: AGPL-3.0-or-later
//
// v0.2.40 H2 — RL settings flag → human-readable summary.
//
// Pure helpers extracted from `RlRerankerStatusPanel.svelte` so we can
// unit-test them without DOM. The wrapper just shows the result; the
// logic that turns the three boolean flags into a short status sentence
// (e.g. "local model active", "frozen", "contributing to global model")
// lives here.
//
// The three flags persist via `set_rl_use_global`,
// `set_rl_online_training_disabled`, `set_rl_global_training_source_flag`
// and read via the new (v0.2.40 H2) `get_rl_*` Tauri commands. The
// widget owns the dashboard's weights state; this module owns the
// flag-state interpretation.

export interface RlFlagSummary {
  /** One-line copy describing the per-project training mode. */
  trainingMode: string;
  /** One-line copy describing the global-corpus opt-in state. */
  globalSource: string;
  /** Stable id-style values for testing / CSS hooks. */
  trainingModeKey:
    | 'local-active'
    | 'read-only-global'
    | 'frozen'
    | 'frozen-and-global';
  globalSourceKey: 'contributing' | 'not-contributing';
}

/**
 * Translate the three RL flag booleans into user-facing copy.
 *
 * The semantics encoded here mirror the module manifest's tooltip text
 * (`paid-modules/vct-rl-reranker/vct-module.json`'s `gui.config_tab`).
 * Keep in sync if the tooltips ever change.
 *
 * Precedence:
 *  - `rl_online_training_disabled` wins ("frozen") over `rl_use_global`
 *    because frozen is the strictly stronger constraint (no writes AND
 *    log-only).
 *  - `rl_use_global` alone means "read-only global mode" (writes ignored
 *    but the project still serves the global checkpoint locally).
 *  - Neither flag set means online training is active for this project.
 *
 * The `globalSource` line is independent of the other two: a project can
 * be frozen AND contribute its event log to the global corpus.
 */
export function summarizeRlFlags(
  useGlobal: boolean,
  onlineDisabled: boolean,
  globalSourceFlag: boolean,
): RlFlagSummary {
  let trainingModeKey: RlFlagSummary['trainingModeKey'];
  let trainingMode: string;

  if (onlineDisabled && useGlobal) {
    trainingModeKey = 'frozen-and-global';
    trainingMode = 'Frozen — read-only global model, no event writes.';
  } else if (onlineDisabled) {
    trainingModeKey = 'frozen';
    trainingMode = 'Frozen — local model unchanged, events logged only.';
  } else if (useGlobal) {
    trainingModeKey = 'read-only-global';
    trainingMode = 'Read-only — global checkpoint served, no local updates.';
  } else {
    trainingModeKey = 'local-active';
    trainingMode = 'Online training active — events update the local model.';
  }

  const globalSourceKey: RlFlagSummary['globalSourceKey'] = globalSourceFlag
    ? 'contributing'
    : 'not-contributing';
  const globalSource = globalSourceFlag
    ? 'Contributing data to the global model retraining corpus.'
    : 'Not contributing to the global model retraining corpus.';

  return { trainingMode, trainingModeKey, globalSource, globalSourceKey };
}
