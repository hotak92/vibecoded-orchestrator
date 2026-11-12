// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.100: the "RL-Scored Retrieval" switch (MCP dashboard, Features tab).
//
// What it is: the GLOBAL DEFAULT of the per-project RL reranker toggle. It is
// the host-wide `enabled_for_project` row of module `vct-rl-reranker`
// (`module_set_global_enabled` / `module_is_global_enabled`); a project with
// no explicit row inherits it. v0.2.106: no licence gate sits in front of it
// any more (owner 2026-11-11, "the RL reranker is no more paid") — the lock
// below is its only gate.
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

// v0.2.106 (owner rulings 2026-11-12): the lock is lifted and RL scoring is
// OPT-IN per project — per-project online learning, served only while the
// project's model beats plain search on its own held-out queries. This is the
// ONE home of the sentence that tells the user what turning it on does; every
// RL control (the per-project Modules control, the host-wide default switch,
// the "enough training events" toast) renders it from here.
export const RL_OPT_IN_EXPLAINER =
  "Off by default. When on, the reranker learns from this project's own citations " +
  '(the search results your answers actually use) and reorders its searches only once ' +
  "this project's model beats plain search on its own check — its most recent queries, " +
  'held out of training. Until then, and whenever it stops passing, results keep their ' +
  'plain search order. You can turn it off any time.';

/** The opt-in explainer for `moduleId`'s enable control, or null for other modules. */
export function rlOptInExplainer(moduleId: string): string | null {
  return moduleId === RL_RERANKER_MODULE_ID ? RL_OPT_IN_EXPLAINER : null;
}

/** The toast shown once enough training events exist to make turning it on worthwhile. */
export function rlAutoEnableToast(eventCount: number, threshold: number): string {
  return (
    `RL Reranker: ${eventCount}/${threshold} training events collected. It is off ` +
    "until you turn it on for a project (Modules → RL Reranker); it then learns from that " +
    "project's own citations and reranks only once it beats plain search."
  );
}

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
    disabled: false,
    notice: null,
    storedNote: null,
  };
}
