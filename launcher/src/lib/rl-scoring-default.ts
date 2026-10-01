// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: the "RL-Scored Retrieval" switch (MCP dashboard, Features tab).
//
// What it is: the GLOBAL DEFAULT of the per-project RL reranker toggle. It is
// the host-wide `enabled_for_project` row of module `vct-rl-reranker`
// (`module_set_global_enabled` / `module_is_global_enabled`); a project with
// no explicit row inherits it, and the hub resolver that decides RL scoring
// reads it (`Db::module_effective_enabled`). The licence gate is unchanged
// and sits in front of it. Before this, the switch wrote a key of
// `~/.vct/orchestrator.json` that nothing read.
//
// What it is NOT: a switch on data collection. Event logging is gated by its
// own flags (RL_LOCAL_LOGGING_DISABLED*) and never consults this one.
//
// OWNER (2026-10-01): "wire it, but for now we are keeping the RL module off
// because we still didn't train the neural network". So the switch is wired
// end to end but rendered DISABLED, and the default is OFF. Flip
// `RL_SCORING_SWITCH_LOCKED` when a trained model ships.

export const RL_RERANKER_MODULE_ID = 'vct-rl-reranker';

/** True while RL scoring stays inactive because no model has been trained. */
export const RL_SCORING_SWITCH_LOCKED = true;

/** Copy shown next to the locked switch. */
export const RL_SCORING_LOCKED_NOTICE =
  'RL scoring stays inactive until the model is trained. Data collection continues regardless.';

export interface RlScoringSwitchView {
  checked: boolean;
  disabled: boolean;
  /** Why the control is disabled, or null when it is usable. */
  notice: string | null;
}

/**
 * The state to render. `globalEnabled` is `module_is_global_enabled`'s answer:
 * `null` (no host-wide row) resolves to OFF, the shipped default. A missing
 * Pro licence disables the switch too but keeps its own (upgrade) hint.
 */
export function rlScoringSwitchView(
  globalEnabled: boolean | null | undefined,
  hasRlRetrieval: boolean,
  locked: boolean = RL_SCORING_SWITCH_LOCKED,
): RlScoringSwitchView {
  return {
    checked: globalEnabled === true,
    disabled: locked || !hasRlRetrieval,
    notice: locked ? RL_SCORING_LOCKED_NOTICE : null,
  };
}
