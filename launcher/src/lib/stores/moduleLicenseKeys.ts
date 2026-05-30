// v0.2.40 L1: per-paid-module license key store.
//
// This store backs the License Manager modal (`LicenseManagerModal.svelte`).
// One row per paid module + the reserved `__orchestrator__` root slot.
// The raw key value NEVER lives in JS — only the redacted prefix the
// Rust side returns via `LicenseKeySummary`. Activation calls go
// straight through to the Rust `set_module_license_key` command.
//
// NOT the same store as:
//   - `license.ts` (singular) — orchestrator-tier root key (legacy flow).
//                                Kept for backward compat with the
//                                ActivationModal.
//   - `licenses.ts` (plural)  — localStorage-backed LS consumer apps
//                                portfolio (Transcrypt, Arzillibus, …).
//                                Talks directly to Lemon Squeezy from
//                                the browser; UNRELATED to the
//                                orchestrator+modules licensing flow.
//
// Per the primary licensing review:
//   .claude/context/reviews/v0240-pre-push-2026-05-30/primary-licensing.md
// — don't repurpose the plural store; build the multi-key support
// alongside the singular store's Rust IPC path. That's what this file
// does.

import { writable } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';
import type {
  LicenseKeySummary,
  ModuleLicenseValidationResult,
  LicenseKeyValidationRow,
} from '$lib/types/launcher';

interface ModuleLicenseKeysState {
  /** Every per-paid-module entry plus the legacy `__orchestrator__`
   *  slot. Order: orchestrator slot first (ASCII-sort puts the
   *  double-underscore prefix at the top), then per-module entries
   *  alphabetically. */
  keys: LicenseKeySummary[];
  /** Module id whose action (set / validate / clear) is in flight, or
   *  null when idle. The UI uses this to disable the per-row buttons
   *  during the round-trip. */
  busyModuleId: string | null;
  /** Initial-load spinner indicator. */
  loading: boolean;
  /** Last-action error message (cleared by the next successful call). */
  error: string | null;
}

const initial: ModuleLicenseKeysState = {
  keys: [],
  busyModuleId: null,
  loading: false,
  error: null,
};

function createModuleLicenseKeysStore() {
  const { subscribe, update } = writable<ModuleLicenseKeysState>(initial);

  return {
    subscribe,

    /** Load every per-module key row. Idempotent — the Rust side will
     *  synthesise the legacy `__orchestrator__` row on the first call
     *  after upgrade if no row exists and a legacy keychain entry is
     *  present. */
    async load(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        const keys = await invoke<LicenseKeySummary[]>('list_license_keys');
        update((s) => ({ ...s, keys, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /** Activate (or rotate) the license key for a single paid module.
     *
     *  Persists to the OS keychain at the per-module username slot
     *  (`license_key__<module_id>`), upserts the metadata row, audits.
     *  Does NOT validate the key against the server in the same call —
     *  the UI runs `validate(module_id)` immediately after when the
     *  user clicks "Save & Validate". Keeping the two calls separate
     *  lets headless tooling persist a key without paying the network
     *  round-trip. */
    async setKey(moduleId: string, licenseKey: string): Promise<boolean> {
      if (!tauriAvailable()) return false;
      const trimmed = licenseKey.trim();
      if (!trimmed) {
        update((s) => ({ ...s, error: 'License key cannot be empty' }));
        return false;
      }
      update((s) => ({ ...s, busyModuleId: moduleId, error: null }));
      try {
        const updated = await invoke<LicenseKeySummary>('set_module_license_key', {
          moduleId,
          licenseKey: trimmed,
        });
        // Merge the updated row into the existing keys list (replace
        // if present, append otherwise) so the GUI re-renders without
        // a full reload.
        update((s) => {
          const next = s.keys.filter((k) => k.module_id !== moduleId);
          next.push(updated);
          next.sort((a, b) => a.module_id.localeCompare(b.module_id));
          return { ...s, keys: next, busyModuleId: null };
        });
        return true;
      } catch (e) {
        update((s) => ({
          ...s,
          busyModuleId: null,
          error: e instanceof Error ? e.message : String(e),
        }));
        return false;
      }
    },

    /** Validate a single module's key against `/validate-tier`.
     *  Returns the wire result so the caller can render a precise
     *  status badge ("Active: pro" / "Invalid" / "Network failure
     *  (stale)" / etc.). Soft-fail: a network failure leaves the
     *  cached tier in place and reports `stale=true` rather than
     *  throwing. */
    async validate(moduleId: string): Promise<ModuleLicenseValidationResult | null> {
      if (!tauriAvailable()) return null;
      update((s) => ({ ...s, busyModuleId: moduleId, error: null }));
      try {
        const result = await invoke<ModuleLicenseValidationResult>(
          'validate_module_license',
          { moduleId },
        );
        // Re-load the row list so the validated_at / tier columns
        // reflect the round-trip outcome.
        const keys = await invoke<LicenseKeySummary[]>('list_license_keys');
        update((s) => ({ ...s, keys, busyModuleId: null }));
        // Surface validation failures (definitive — not stale) as the
        // current error so the row's badge has context. Stale-network
        // failures are reported via the returned `stale` field
        // instead so the GUI can render a yellow warning rather than
        // a red error.
        if (!result.valid && !result.stale) {
          update((s) => ({ ...s, error: result.error ?? 'Validation failed' }));
        }
        return result;
      } catch (e) {
        update((s) => ({
          ...s,
          busyModuleId: null,
          error: e instanceof Error ? e.message : String(e),
        }));
        return null;
      }
    },

    /** Clear (deactivate) a per-module license. Removes the keychain
     *  entry, drops the metadata row + audit history, clears the
     *  matching `tier_cache.module_licenses` overlay entry. The
     *  orchestrator-root slot clearance degrades the user to free
     *  tier the same way the legacy ActivationModal's Deactivate
     *  button does. */
    async clear(moduleId: string): Promise<boolean> {
      if (!tauriAvailable()) return false;
      update((s) => ({ ...s, busyModuleId: moduleId, error: null }));
      try {
        await invoke<void>('clear_module_license_key', { moduleId });
        const keys = await invoke<LicenseKeySummary[]>('list_license_keys');
        update((s) => ({ ...s, keys, busyModuleId: null }));
        return true;
      } catch (e) {
        update((s) => ({
          ...s,
          busyModuleId: null,
          error: e instanceof Error ? e.message : String(e),
        }));
        return false;
      }
    },

    /** Fetch the recent validation timeline for one module. Used by
     *  the License Manager modal's per-row expansion panel. Returns
     *  an empty array on any failure (the timeline is advisory). */
    async recentValidations(
      moduleId: string,
      limit = 10,
    ): Promise<LicenseKeyValidationRow[]> {
      if (!tauriAvailable()) return [];
      try {
        return await invoke<LicenseKeyValidationRow[]>(
          'list_module_license_validations',
          { moduleId, limit },
        );
      } catch (e) {
        console.debug('[moduleLicenseKeys] timeline fetch failed:', e);
        return [];
      }
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const moduleLicenseKeys = createModuleLicenseKeysStore();

/** Format a Unix-ms timestamp as a short human-readable date. Used by
 *  the modal's "Last validated" column. Returns "—" for null/0. */
export function formatTimestamp(ms: number | null): string {
  if (!ms || ms === 0) return '—';
  try {
    return new Date(ms).toLocaleString();
  } catch {
    return '—';
  }
}

/** Render the per-row status badge label. Mapping mirrors the badge
 *  shown by `ActivationModal` for the orchestrator tier — keeps the
 *  visual language consistent across the two licensing surfaces. */
export function statusBadge(row: { tier: string | null; last_validation_error: string | null }):
  | { label: string; severity: 'ok' | 'warn' | 'err' | 'neutral' } {
  if (row.last_validation_error) {
    if (row.tier) {
      // Cached tier still present but last attempt errored — soft warn.
      return { label: `Active (${row.tier}, last check failed)`, severity: 'warn' };
    }
    return { label: 'Invalid', severity: 'err' };
  }
  if (row.tier) {
    return { label: `Active: ${row.tier}`, severity: 'ok' };
  }
  return { label: 'Not validated', severity: 'neutral' };
}
