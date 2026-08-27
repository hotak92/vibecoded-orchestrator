// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.91 WP-I (decision #6) — the deferral ledger's data access.
//
// SCOPE SPLIT, restated because it is the whole design:
//
//   * the ORCHESTRATOR-ROOT ledger is install-wide, so it is a STORE — the
//     MenuBar badge, the Preferences → Updates panel and the MCP maintenance
//     page all read the same loaded copy and cannot show three different
//     numbers for one file;
//   * a PROJECT's ledger belongs to that project's Settings panel and nowhere
//     else, so it is a plain loader with no shared state. There is deliberately
//     no map-of-projects store: a cache keyed by project would make it possible
//     to render one project's entries while another is selected, which is the
//     exact ambiguity the UX rider forbids.
//
// Nothing here ever merges the two. `badgeCount` is applied per view.

import { writable, get } from 'svelte/store';

import { invoke, safeInvoke } from '$lib/tauri';
import type {
  DeferralLedgerView,
  DismissOutcome,
  LedgerScope,
} from '$lib/deferral-ledger';

export interface RootLedgerState {
  view: DeferralLedgerView | null;
  loading: boolean;
  /** Set only when the READ itself failed (no clone resolved, IPC error). */
  error: string | null;
  /** True once a load has completed at least once (success or failure). */
  loaded: boolean;
}

const initial: RootLedgerState = {
  view: null,
  loading: false,
  error: null,
  loaded: false,
};

function createDeferralsStore() {
  const { subscribe, update, set } = writable<RootLedgerState>({ ...initial });

  return {
    subscribe,

    /**
     * Load the ORCHESTRATOR-ROOT ledger.
     *
     * Soft read (`safeInvoke`): in browser preview, or when the launcher runs
     * outside a clone, this resolves to `null` and the badge simply does not
     * render. A ledger that cannot be read must never be shown as "all clear",
     * which is why `view` stays null rather than becoming an empty view.
     */
    async refreshRoot(): Promise<DeferralLedgerView | null> {
      update((s) => ({ ...s, loading: true }));
      const view = await safeInvoke<DeferralLedgerView>('deferral_ledger_for_root');
      update((s) => ({
        ...s,
        view,
        loading: false,
        loaded: true,
        error: view ? null : 'orchestrator-root ledger unavailable',
      }));
      return view;
    },

    /** Current root view without subscribing. */
    rootView(): DeferralLedgerView | null {
      return get({ subscribe }).view;
    },

    reset() {
      set({ ...initial });
    },
  };
}

export const deferrals = createDeferralsStore();

/**
 * Load ONE project's ledger. Not stored — see the scope note at the top.
 *
 * Strict `invoke` because the caller is a mounted panel that wants to show the
 * error (an unknown project id is a bug worth surfacing, not a silent blank).
 */
export async function loadProjectLedger(
  projectId: string,
): Promise<DeferralLedgerView> {
  return invoke<DeferralLedgerView>('deferral_ledger_for_project', { projectId });
}

/**
 * Dismiss ONE entry in ONE scope.
 *
 * `scope` is passed explicitly and `projectId` only accompanies the project
 * scope — the backend refuses a project dismissal without one rather than
 * guessing a folder. Callers refresh their own view afterwards; this function
 * deliberately does not, so a project panel's dismissal never triggers a root
 * reload (and vice versa).
 */
export async function dismissDeferral(
  scope: LedgerScope,
  conditionId: string,
  projectId?: string,
): Promise<DismissOutcome> {
  return invoke<DismissOutcome>('dismiss_deferral_entry', {
    scope,
    projectId: scope === 'project' ? (projectId ?? null) : null,
    conditionId,
  });
}
