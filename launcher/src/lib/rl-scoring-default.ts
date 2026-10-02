// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: the "RL-Scored Retrieval" switch (MCP dashboard, Features tab).
//
// What it is: the GLOBAL DEFAULT of the per-project RL reranker toggle. It is
// the host-wide `enabled_for_project` row of module `vct-rl-reranker`
// (`module_set_global_enabled` / `module_is_global_enabled`); a project with
// no explicit row inherits it. The licence gate is unchanged and sits in
// front of it.
//
// What it is NOT: a switch on data collection. Event logging is gated by its
// own flags (RL_LOCAL_LOGGING_DISABLED*) and never consults this one.
//
// OWNER (2026-10-01): "wire it, but for now we are keeping the RL module off
// because we still didn't train the neural network, so keep it unactive for
// now". That is the RL SCORING LOCK (W5R-02). Its ONE home is
// `vco_lib/rl_scoring_lock.toml`; this file holds no copy of it. The
// dashboard reads it from the backend (`rl_scoring_lock` command) and passes
// it in. While it is set the switch renders the TRUTH: unchecked (scoring is
// off whatever the stored row says), disabled, with the reason, and with the
// stored host-wide choice named as what applies once unlocked.

export const RL_RERANKER_MODULE_ID = 'vct-rl-reranker';

export interface RlScoringSwitchView {
  /** Whether RL scoring is ON by default — never true while locked. */
  checked: boolean;
  disabled: boolean;
  /** Why the control is disabled by the lock, or null when it is not locked. */
  notice: string | null;
  /** While locked: the stored host-wide choice that applies once unlocked. */
  storedNote: string | null;
}

/**
 * The state to render.
 *
 * - `globalEnabled`: `module_is_global_enabled`'s answer; `null` (no
 *   host-wide row) resolves to OFF, the shipped default.
 * - `lockReason`: the `rl_scoring_lock` command's answer. `undefined` means
 *   it has not loaded (or the read failed): render disabled and unchecked
 *   rather than guess that scoring is allowed.
 */
export function rlScoringSwitchView(
  globalEnabled: boolean | null | undefined,
  hasRlRetrieval: boolean,
  lockReason: string | null | undefined,
): RlScoringSwitchView {
  if (lockReason === undefined) {
    return { checked: false, disabled: true, notice: null, storedNote: null };
  }
  if (lockReason !== null) {
    return {
      checked: false,
      disabled: true,
      notice: lockReason,
      storedNote:
        globalEnabled === true
          ? 'Stored host-wide default: On. It is kept and applies once RL scoring is unlocked.'
          : null,
    };
  }
  return {
    checked: globalEnabled === true,
    disabled: !hasRlRetrieval,
    notice: null,
    storedNote: null,
  };
}
