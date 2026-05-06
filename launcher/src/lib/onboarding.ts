// Onboarding-completion state.
//
// Source of truth: launcher.db `app_state` table, key `onboarding.complete`.
// Backed by the Tauri commands in `commands/app_state_cmd.rs`. This is
// the Bug 14 fix (PR coming): the onboarding flag used to live in WebView
// localStorage which is shared across all launchers with the same Tauri
// app identifier — so a launcher run with `VCT_STATE_DIR=$HOME/.vct-dev/`
// and a launcher with default `~/.vct/` would never see distinct onboarding
// states. Moving to launcher.db makes the dev/prod split actually isolate.
//
// Migration semantics for existing users: on the first read after this
// ships, if the DB has no row AND localStorage has the legacy
// `vct.onboarding_complete=true` key, copy it once and remove the
// localStorage key. Existing users do NOT get prompted to onboard again.
//
// In browser mode (no Tauri), the helpers fall back to localStorage so
// `vite dev` doesn't break.

import { invoke, isTauriRuntime } from './tauri';

const KEY = 'onboarding.complete';
const LEGACY_LS_KEY = 'vct.onboarding_complete';

let _cache: boolean | null = null;
let _cacheLoaded = false;

/**
 * Returns true when the user has completed onboarding (don't show wizard).
 * Returns false when they haven't (do show wizard).
 *
 * Performs a one-shot localStorage→DB upgrade on the first read so users
 * who completed onboarding before this fix landed don't get re-prompted.
 */
export async function isOnboardingComplete(): Promise<boolean> {
  if (_cacheLoaded && _cache !== null) return _cache;

  if (!isTauriRuntime()) {
    // Browser dev mode — keep using localStorage.
    try {
      const v = localStorage.getItem(LEGACY_LS_KEY);
      _cache = v === 'true';
      _cacheLoaded = true;
      return _cache;
    } catch {
      _cache = false;
      _cacheLoaded = true;
      return false;
    }
  }

  try {
    const dbVal = await invoke<boolean | null>('app_state_get_bool', { key: KEY });

    if (dbVal !== null) {
      _cache = dbVal;
      _cacheLoaded = true;
      return dbVal;
    }

    // DB has no row — check the legacy localStorage key for one-shot upgrade.
    let legacyVal: string | null = null;
    try {
      legacyVal = localStorage.getItem(LEGACY_LS_KEY);
    } catch {
      legacyVal = null;
    }

    if (legacyVal === 'true') {
      // Copy → DB, then remove the legacy key so future reads come straight
      // from the DB and the dev/prod split actually isolates from now on.
      try {
        await invoke('app_state_set_bool', { key: KEY, value: true });
      } catch (e) {
        // Non-fatal — user will just see the wizard again on next launch.
        // Don't surface an error toast for this.
        console.warn('[onboarding] migrate localStorage→DB failed:', e);
      }
      try {
        localStorage.removeItem(LEGACY_LS_KEY);
      } catch {
        // ignore
      }
      _cache = true;
      _cacheLoaded = true;
      return true;
    }

    _cache = false;
    _cacheLoaded = true;
    return false;
  } catch (e) {
    // Tauri call failed unexpectedly — be conservative and treat as "not
    // onboarded" so the user sees the wizard rather than a silent no-op.
    console.warn('[onboarding] isOnboardingComplete failed:', e);
    _cache = false;
    _cacheLoaded = true;
    return false;
  }
}

/**
 * Mark onboarding as complete. Called by the wizard's finish/skip paths.
 * Writes to the DB (or localStorage in browser mode) and updates the cache.
 */
export async function markOnboardingComplete(): Promise<void> {
  _cache = true;
  _cacheLoaded = true;

  if (!isTauriRuntime()) {
    try { localStorage.setItem(LEGACY_LS_KEY, 'true'); } catch { /* ignore */ }
    return;
  }

  try {
    await invoke('app_state_set_bool', { key: KEY, value: true });
  } catch (e) {
    console.warn('[onboarding] markOnboardingComplete failed:', e);
    // Don't throw — completion should be best-effort. Worst case the
    // user sees the wizard again next launch.
  }
}

/**
 * Mark onboarding as NOT complete. Called by the SettingsPanel "Run setup
 * wizard" button so the wizard re-fires on the next layout mount.
 *
 * Also cleans up the legacy localStorage key if it lingers — so a forced
 * re-run actually re-runs and isn't immediately satisfied by the legacy
 * cache.
 */
export async function clearOnboardingComplete(): Promise<void> {
  _cache = false;
  _cacheLoaded = true;

  // Clear legacy localStorage either way, so a "Run setup wizard" click
  // doesn't get short-circuited by stale localStorage on next read.
  try { localStorage.removeItem(LEGACY_LS_KEY); } catch { /* ignore */ }

  if (!isTauriRuntime()) return;

  try {
    await invoke('app_state_set_bool', { key: KEY, value: false });
  } catch (e) {
    console.warn('[onboarding] clearOnboardingComplete failed:', e);
  }
}

/**
 * Test-only / SSR-safe reset of the in-memory cache. Production code does
 * not need to call this.
 */
export function _resetCacheForTests(): void {
  _cache = null;
  _cacheLoaded = false;
}
