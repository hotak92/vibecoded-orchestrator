// Tauri-backed license tier store.
//
// Replaces the localStorage-backed `licenses` store flow that talked to
// Lemon Squeezy directly. The Rust backend wraps the Supabase
// /validate-tier edge function and persists the tier cache in
// ~/.vct/launcher.db; the actual license key is held in the OS keychain.
//
// The legacy `lib/stores/licenses.ts` is kept around so the per-app
// activation flow (Transcrypt etc.) doesn't break. This store is
// orthogonal — it gates the *orchestrator* tier (free/pro/mao/enterprise)
// which determines whether paid orchestrator modules can be installed.

import { writable } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import type { TierCacheView } from '$lib/types/launcher';

interface LicenseState {
  cache: TierCacheView | null;
  loading: boolean;
  activating: boolean;
  error: string | null;
}

const initial: LicenseState = {
  cache: null,
  loading: false,
  activating: false,
  error: null,
};

function createLicenseStore() {
  const { subscribe, update } = writable<LicenseState>(initial);

  return {
    subscribe,

    async load(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        const cache = await invoke<TierCacheView>('license_get_tier');
        update((s) => ({ ...s, cache, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    async refresh(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        const cache = await invoke<TierCacheView>('license_refresh');
        update((s) => ({ ...s, cache, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /** Activate a license key. Persists to the OS keychain (via Rust),
     * then immediately calls /validate-tier and updates the cache. */
    async activate(key: string): Promise<boolean> {
      if (!tauriAvailable()) return false;
      const trimmed = key.trim();
      if (!trimmed) {
        update((s) => ({ ...s, error: 'License key cannot be empty' }));
        return false;
      }
      update((s) => ({ ...s, activating: true, error: null }));
      try {
        const cache = await invoke<TierCacheView>('license_activate', {
          licenseKey: trimmed,
        });
        update((s) => ({ ...s, cache, activating: false }));
        // Surface backend-reported errors even on a successful round-trip
        // (e.g. tier="free" + last_error="invalid key").
        if (cache.last_error && cache.orchestrator_tier === 'free') {
          update((s) => ({ ...s, error: cache.last_error }));
          return false;
        }
        return true;
      } catch (e) {
        update((s) => ({
          ...s,
          activating: false,
          error: e instanceof Error ? e.message : String(e),
        }));
        return false;
      }
    },

    async deactivate(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        await invoke<void>('license_deactivate');
        const cache = await invoke<TierCacheView>('license_get_tier');
        update((s) => ({ ...s, cache, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const license = createLicenseStore();

/** Format the grace-period remaining as a short human string. */
export function formatGrace(ms: number | null): string {
  if (!ms || ms <= 0) return '';
  const days = Math.floor(ms / (24 * 3600 * 1000));
  const hours = Math.floor((ms % (24 * 3600 * 1000)) / (3600 * 1000));
  if (days > 0) return `${days}d ${hours}h`;
  return `${hours}h`;
}
