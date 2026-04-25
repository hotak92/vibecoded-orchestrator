// Module catalog + install state.
//
// Catalog comes from `list_module_catalog` which scans both
// ~/.vct/modules (already installed) and ~/.vct/bundled_manifests.
// Install rows per project come from `list_installed_modules`.
//
// Install progress: the Rust side emits a single `module://install-complete`
// event; intermediate progress is not surfaced today (see install.rs).
// We model install as a single async call with start/end states.

import { writable, derived } from 'svelte/store';
import { invoke, listen, tauriAvailable } from '$lib/tauri';
import type {
  ModuleCatalogEntry,
  ModuleInstallRow,
  ModuleInstallCompleteEvent,
  ModuleStatusView,
} from '$lib/types/launcher';

interface ModulesState {
  catalog: ModuleCatalogEntry[];
  installed: ModuleInstallRow[]; // for currently-selected project
  installingId: string | null;
  loading: boolean;
  error: string | null;
}

function createModulesStore() {
  const { subscribe, update } = writable<ModulesState>({
    catalog: [],
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
  }

  return {
    subscribe,

    async loadCatalog(): Promise<void> {
      if (!tauriAvailable()) return;
      update((s) => ({ ...s, loading: true, error: null }));
      try {
        const catalog = await invoke<ModuleCatalogEntry[]>('list_module_catalog');
        update((s) => ({ ...s, catalog, loading: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          loading: false,
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
