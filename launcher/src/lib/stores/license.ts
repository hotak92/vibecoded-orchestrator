// Tauri-backed license tier store.
//
// Replaces the localStorage-backed `licenses` store flow that talked to
// Lemon Squeezy directly. The Rust backend wraps the Supabase
// /validate-tier edge function and persists the tier cache in
// ~/.vct/launcher.db; the actual license key is held in the OS keychain.
//
// The legacy `lib/stores/licenses.ts` (per-app, localStorage-backed,
// client-side LemonSqueezy calls) was DELETED in v0.2.54 Track H — it
// had zero importers and embedded the LS API key in the client bundle.
// This store gates the *orchestrator* tier (free/pro/mao/enterprise/
// admin), which determines whether paid orchestrator modules can be
// installed.

import { writable } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import type { AdminRebindResult, ModuleLicenseRow, TierCacheView } from '$lib/types/launcher';

interface LicenseState {
  cache: TierCacheView | null;
  /** v0.2.32 §D1: per-module license rows for the dialog's new section.
   *  Loaded alongside `cache` by every load/refresh/activate/deactivate
   *  cycle. Empty when no per-module entries are active — the dialog
   *  renders a friendly empty state in that case. */
  moduleLicenses: ModuleLicenseRow[];
  loading: boolean;
  activating: boolean;
  /** v0.2.36: in-flight indicator for the "Rebind to this machine"
   *  affordance. Separate from `loading` so the orchestrator-tier
   *  refresh spinner stays orthogonal to the rebind action. */
  rebinding: boolean;
  error: string | null;
}

const initial: LicenseState = {
  cache: null,
  moduleLicenses: [],
  loading: false,
  activating: false,
  rebinding: false,
  error: null,
};

/** Soft-fail per-module license fetch.
 *
 *  v0.2.32: the rest of the dialog must keep working even if the new
 *  `get_module_licenses` Tauri command isn't yet registered (older
 *  launcher binaries shipped without it). Errors are swallowed and
 *  treated as "no per-module entries" rather than propagated to the
 *  user — this is purely additive UX. */
async function fetchModuleLicensesSafe(): Promise<ModuleLicenseRow[]> {
  if (!tauriAvailable()) return [];
  try {
    return await invoke<ModuleLicenseRow[]>('get_module_licenses');
  } catch (e) {
    // Log once at debug level — not a user-visible error.
    console.debug('[license] get_module_licenses unavailable:', e);
    return [];
  }
}

function createLicenseStore() {
  const { subscribe, update } = writable<LicenseState>(initial);

  return {
    subscribe,

    async load(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        const cache = await invoke<TierCacheView>('license_get_tier');
        const moduleLicenses = await fetchModuleLicensesSafe();
        update((s) => ({ ...s, cache, moduleLicenses, loading: false }));
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
        const moduleLicenses = await fetchModuleLicensesSafe();
        update((s) => ({ ...s, cache, moduleLicenses, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /** v0.2.32 §D1: refresh a single per-module license entry.
     *
     *  Soft-stub: routes through the full `license_refresh` server-side
     *  call (the validate-tier edge function returns the entire
     *  module_licenses map in one shot — no per-module endpoint exists
     *  yet). Reloads the whole row list on success. */
    async refreshModule(moduleId: string): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        await invoke<ModuleLicenseRow | null>('module_license_refresh', { moduleId });
        // Reload both the orchestrator tier (since the full refresh may
        // have changed it) and the module rows.
        const cache = await invoke<TierCacheView>('license_get_tier');
        const moduleLicenses = await fetchModuleLicensesSafe();
        update((s) => ({ ...s, cache, moduleLicenses, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /** v0.2.32 §D1: deactivate a single per-module license entry.
     *
     *  Soft-stub: clears the LOCAL `tier_cache.module_licenses` entry
     *  only — server-side per-module deactivation isn't shipped yet, so
     *  the next full `refresh()` may re-add the entry if the server
     *  still thinks the module is entitled. That's the right behaviour
     *  for a UX-only deactivation. */
    async deactivateModule(moduleId: string): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        await invoke<void>('module_license_deactivate', { moduleId });
        const cache = await invoke<TierCacheView>('license_get_tier');
        const moduleLicenses = await fetchModuleLicensesSafe();
        update((s) => ({ ...s, cache, moduleLicenses, loading: false }));
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
        const moduleLicenses = await fetchModuleLicensesSafe();
        update((s) => ({ ...s, cache, moduleLicenses, activating: false }));
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
        const moduleLicenses = await fetchModuleLicensesSafe();
        update((s) => ({ ...s, cache, moduleLicenses, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /** v0.2.36: rebind the current admin token to this machine.
     *
     *  Calls the Rust `license_rebind_admin_token` command which:
     *    1. reads the license key from the OS keychain (never crosses IPC);
     *    2. computes the current machine_id_hash;
     *    3. POSTs to `/functions/v1/rebind-admin-token`;
     *    4. on success, refreshes the cached tier so /validate-tier
     *       stops returning machine_mismatch.
     *
     *  Returns the wire result so the caller can render a precise toast
     *  (success / specific failure code). On thrown IPC failure
     *  (very rare — only if Tauri isn't available) the store falls back
     *  to a synthesised `error: "ipc_unavailable"` result. */
    async rebindAdminToken(): Promise<AdminRebindResult> {
      if (!tauriAvailable()) {
        return {
          success: false,
          user: null,
          rebound_at: null,
          error: 'ipc_unavailable',
          detail: 'Tauri bridge not initialised; reload the launcher.',
          machine_id_hash: '',
        };
      }
      update((s) => ({ ...s, rebinding: true, error: null }));
      try {
        const result = await invoke<AdminRebindResult>('license_rebind_admin_token');
        // Reload cache + module rows after a successful rebind so the
        // dialog reflects the now-valid tier without a manual Refresh.
        if (result.success) {
          const cache = await invoke<TierCacheView>('license_get_tier');
          const moduleLicenses = await fetchModuleLicensesSafe();
          update((s) => ({ ...s, cache, moduleLicenses, rebinding: false }));
        } else {
          update((s) => ({ ...s, rebinding: false }));
        }
        return result;
      } catch (e) {
        update((s) => ({
          ...s,
          rebinding: false,
          error: e instanceof Error ? e.message : String(e),
        }));
        return {
          success: false,
          user: null,
          rebound_at: null,
          error: 'ipc_failure',
          detail: e instanceof Error ? e.message : String(e),
          machine_id_hash: '',
        };
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
