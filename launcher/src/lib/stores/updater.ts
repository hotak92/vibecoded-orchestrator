// Orchestrator update detection + dismissal state.
//
// `check_for_updates` is already called by the orchestrator store on every
// `checkStatus()`. This store layers on top: tracks the version detected
// and the timestamp the user last saw a notification, so we don't re-toast
// on every render.

import { writable, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import { orchestrator } from './orchestrator';

const SEEN_KEY = 'vct.update.seen_version';

interface UpdaterState {
  available: boolean;
  /** The version we detected as available — kept opaque (Rust doesn't
   * expose the new version yet, only a boolean). When the boolean
   * transitions false→true we re-show. */
  lastSeenVersion: string | null;
  updating: boolean;
  error: string | null;
  dismissed: boolean;
}

function loadSeen(): string | null {
  if (typeof localStorage === 'undefined') return null;
  return localStorage.getItem(SEEN_KEY);
}

function saveSeen(v: string | null) {
  if (typeof localStorage === 'undefined') return;
  if (v) localStorage.setItem(SEEN_KEY, v);
  else localStorage.removeItem(SEEN_KEY);
}

function createUpdaterStore() {
  const { subscribe, update } = writable<UpdaterState>({
    available: false,
    lastSeenVersion: loadSeen(),
    updating: false,
    error: null,
    dismissed: false,
  });

  return {
    subscribe,

    /** Pull update status from the orchestrator store. Re-shows the toast
     * if the underlying version changed since the last dismissal. */
    syncFromOrchestrator() {
      const o = get(orchestrator);
      const installed = o.status === 'installed' || o.status === 'updating';
      if (!installed) {
        update((s) => ({ ...s, available: false }));
        return;
      }
      if (o.updateAvailable) {
        // We don't have a "new version string" from the backend today, so
        // we use the *current* version as a marker: if it differs from
        // last seen, re-show. After the user dismisses or updates, we mark
        // this version as seen.
        const marker = o.version || '';
        update((s) => ({
          ...s,
          available: true,
          dismissed: s.lastSeenVersion === marker ? s.dismissed : false,
        }));
      } else {
        update((s) => ({ ...s, available: false, dismissed: false }));
      }
    },

    dismiss() {
      const o = get(orchestrator);
      saveSeen(o.version || '');
      update((s) => ({
        ...s,
        dismissed: true,
        lastSeenVersion: o.version || '',
      }));
    },

    async runUpdate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, updating: true, error: null }));
      try {
        await orchestrator.update_orchestrator();
        const o = get(orchestrator);
        saveSeen(o.version || '');
        update((s) => ({
          ...s,
          updating: false,
          available: false,
          dismissed: true,
          lastSeenVersion: o.version || '',
        }));
        // Re-check to catch any same-version-but-still-newer case.
        await orchestrator.checkStatus();
      } catch (e) {
        update((s) => ({
          ...s,
          updating: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const updater = createUpdaterStore();
