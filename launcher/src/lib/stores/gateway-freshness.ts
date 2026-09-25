// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools
//
// v0.2.97 — the store behind `GatewayRestartModal.svelte`. A thin shell over
// `$lib/gateway-freshness` (where the decisions live, pure and tested); this
// file only binds it to Svelte, the Tauri bridge and `localStorage`.
//
// Asked at launcher start (both update surfaces end in a launcher restart,
// so the new process is the first moment the new code is on disk AND a GUI is
// up) and again after an in-process update that did not restart the launcher.

import { get, writable } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import {
  createFreshnessController,
  INITIAL_FRESHNESS_STATE,
  type FreshnessState,
  type KeyValueStore,
} from '$lib/gateway-freshness';

function browserStorage(): KeyValueStore | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

function createGatewayFreshnessStore() {
  const state = writable<FreshnessState>(INITIAL_FRESHNESS_STATE);
  const controller = createFreshnessController({
    invoke,
    storage: browserStorage(),
    set: state.set,
    get: () => get(state),
  });
  return {
    subscribe: state.subscribe,
    /** No-op in browser mode: there is no gateway to ask about. */
    async check(): Promise<void> {
      if (!tauriAvailable()) return;
      await controller.check();
    },
    continueRestart: controller.continueRestart,
    dismiss: controller.dismiss,
  };
}

export const gatewayFreshness = createGatewayFreshnessStore();
