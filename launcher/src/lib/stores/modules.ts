// Module catalog + install state.
//
// v0.2.33 (Agent B, L0a): `list_module_catalog` now returns a
// `CatalogResponse` envelope instead of the bare `ModuleCatalogEntry[]`.
// We unwrap `.modules` into the existing `catalog` slot; the new
// `l0_status`, `parse_errors`, and `dev_affordance_hint` fields land in
// the store for Agent E's banner/toast surfaces.
//
// Install rows per project come from `list_installed_modules`.
//
// Install progress: the Rust side emits a single `module://install-complete`
// event; intermediate progress is not surfaced today (see install.rs).
// We model install as a single async call with start/end states.

import { writable, derived } from 'svelte/store';
import { invoke, listen, tauriAvailable } from '$lib/tauri';
import { toast } from '$lib/stores/toast';
import type {
  CatalogResponse,
  DevAffordanceHint,
  L0Status,
  ManifestParseError,
  ModuleCatalogEntry,
  ModuleInstallRow,
  ModuleInstallCompleteEvent,
  ModuleStatusView,
} from '$lib/types/launcher';

/**
 * v0.2.35 (Agent N): subset of the InstallProgress payload the Rust
 * `installer_engine` emits on `module://install-progress`. We only
 * consume the variant-fallback stage here; the rest of the progress
 * channel is unused today (see the legacy comment above — install is
 * modelled as a single async call).
 *
 * Shape mirrors `InstallStage` (snake_case via serde rename) — the only
 * stage this store reacts to is `variant_fallback`.
 */
interface ModuleInstallProgressEvent {
  project_id: string;
  module_id: string;
  stage: string;
  step_index: number;
  step_total: number;
  percent: number;
  message: string;
}

interface ModulesState {
  catalog: ModuleCatalogEntry[];
  /** v0.2.33: L0 fetch outcome; populated by the catalog load path. */
  l0Status: L0Status | null;
  /** v0.2.33: per-manifest parse errors surfaced by the catalog build. */
  parseErrors: ManifestParseError[];
  /** v0.2.33: dev-affordance hint (review §10.c). */
  devAffordanceHint: DevAffordanceHint | null;
  installed: ModuleInstallRow[]; // for currently-selected project
  installingId: string | null;
  loading: boolean;
  error: string | null;
}

function createModulesStore() {
  const { subscribe, update } = writable<ModulesState>({
    catalog: [],
    l0Status: null,
    parseErrors: [],
    devAffordanceHint: null,
    installed: [],
    installingId: null,
    loading: false,
    error: null,
  });

  // Wire the install-complete event once.
  if (tauriAvailable()) {
    listen<ModuleInstallCompleteEvent>('module://install-complete', () => {
      update((s) => ({ ...s, installingId: null }));
    });

    // v0.2.35 (Agent N): surface a toast when the install's chosen
    // GPU variant wasn't available on the registry and the backend
    // silently fell back to the cpu variant. The install continues
    // normally — this is purely informational so the user understands
    // why their CUDA variant didn't land.
    //
    // Non-blocking by design: pre-v0.2.35 the same scenario produced a
    // cryptic `denied`/404 hard-fail with no way to act. The fallback
    // now Just Works for the common case (publisher hasn't built
    // `-cuda` for this release yet) and the toast tells the user what
    // happened. If they want the cuda variant they can re-install once
    // the publisher ships it.
    listen<ModuleInstallProgressEvent>('module://install-progress', (e) => {
      if (e.payload.stage === 'variant_fallback') {
        toast.info(e.payload.message);
      }
    });
  }

  // Extracted as a closure so `forceRefresh` can call it directly
  // without going through `this` (which can be unbound when consumers
  // destructure the store API).
  async function loadCatalogImpl(): Promise<void> {
    if (!tauriAvailable()) return;
    update((s) => ({ ...s, loading: true, error: null }));
    try {
      const response = await invoke<CatalogResponse>('list_module_catalog');
      update((s) => ({
        ...s,
        catalog: response.modules,
        l0Status: response.l0_status,
        parseErrors: response.parse_errors,
        devAffordanceHint: response.dev_affordance_hint,
        loading: false,
      }));
    } catch (e) {
      update((s) => ({
        ...s,
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      }));
    }
  }

  return {
    subscribe,

    loadCatalog: loadCatalogImpl,

    /**
     * v0.2.34 (Agent C): force a fresh L0 fetch, bypassing the DB-backed
     * 15-min TTL. Wired to the always-visible `↻` button on the Modules
     * tab. Returns true on success (cache rewritten), false on failure
     * (existing store state preserved, error stored on the store for
     * the caller to surface as a toast).
     *
     * Why a separate method from `loadCatalog`:
     *   - `loadCatalog` invokes `list_module_catalog`, which honours
     *     the DB-backed cache (great for first-paint, wrong for
     *     manual refresh).
     *   - `refresh_module_catalog` (Rust) bypasses the cache, rewrites
     *     it with the fresh fetch; then `loadCatalogImpl` re-reads
     *     the authoritative envelope through `list_module_catalog`
     *     so all the L0Status / parseErrors / devAffordanceHint
     *     fields stay derived consistently with the standard path.
     *
     * The two-step (refresh → reload) keeps the store-update logic
     * single-source-of-truth inside `loadCatalogImpl`.
     */
    async forceRefresh(): Promise<boolean> {
      if (!tauriAvailable()) return false;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        // First: bypass-TTL fetch + cache rewrite. We don't read the
        // returned envelope here — loadCatalogImpl below re-reads it
        // through the standard `list_module_catalog` path so all the
        // envelope-derived store fields stay consistent.
        await invoke<unknown>('refresh_module_catalog');
        // Second: pull the freshly-cached envelope through the
        // canonical store-loading path.
        await loadCatalogImpl();
        return true;
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
          error: e instanceof Error ? e.message : String(e),
        }));
        return false;
      }
    },

    /**
     * v0.2.33: dismiss the dev-affordance toast. Persisted to launcher.db
     * so subsequent sessions don't re-surface it.
     */
    async dismissDevAffordance(): Promise<void> {
      if (!tauriAvailable()) return;
      try {
        await invoke<void>('dismiss_dev_affordance_hint');
        update((s) => ({ ...s, devAffordanceHint: null }));
      } catch (e) {
        update((s) => ({
          ...s,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    async loadInstalled(projectId: string): Promise<void> {
      if (!tauriAvailable()) return;
      try {
        const installed = await invoke<ModuleInstallRow[]>('list_installed_modules', {
          projectId,
        });
        update((s) => ({ ...s, installed }));
      } catch (e) {
        update((s) => ({
          ...s,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    async install(projectId: string, moduleId: string): Promise<ModuleInstallRow> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, installingId: moduleId, error: null }));
      try {
        const row = await invoke<ModuleInstallRow>('install_module_for_project', {
          projectId,
          moduleId,
        });
        update((s) => ({
          ...s,
          installed: [...s.installed.filter((r) => r.module_id !== moduleId), row],
          installingId: null,
        }));
        return row;
      } catch (e) {
        update((s) => ({
          ...s,
          installingId: null,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    async update(projectId: string, moduleId: string): Promise<ModuleInstallRow> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, installingId: moduleId, error: null }));
      try {
        const row = await invoke<ModuleInstallRow>('update_module_for_project', {
          projectId,
          moduleId,
        });
        update((s) => ({
          ...s,
          installed: [...s.installed.filter((r) => r.module_id !== moduleId), row],
          installingId: null,
        }));
        return row;
      } catch (e) {
        update((s) => ({
          ...s,
          installingId: null,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    async uninstall(projectId: string, moduleId: string, purgeData: boolean): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      try {
        await invoke<void>('uninstall_module_v2', { projectId, moduleId, purgeData });
        update((s) => ({
          ...s,
          installed: s.installed.filter((r) => r.module_id !== moduleId),
        }));
      } catch (e) {
        update((s) => ({
          ...s,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    async setEnabled(projectId: string, moduleId: string, enabled: boolean): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      try {
        await invoke<void>('set_module_enabled_v2', { projectId, moduleId, enabled });
        update((s) => ({
          ...s,
          installed: s.installed.map((r) =>
            r.module_id === moduleId ? { ...r, enabled } : r,
          ),
        }));
      } catch (e) {
        update((s) => ({
          ...s,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    async getStatus(projectId: string, moduleId: string): Promise<ModuleStatusView | null> {
      if (!tauriAvailable()) return null;
      return await invoke<ModuleStatusView | null>('module_status_v2', {
        projectId,
        moduleId,
      });
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const modules = createModulesStore();

/** Set of installed module ids for the currently loaded project. */
export const installedIds = derived(modules, ($m) =>
  new Set($m.installed.map((r) => r.module_id)),
);
