// Per-secret-entry helper store.
//
// The secrets backend is intentionally CRUD-by-key — there's no list
// endpoint. The UI tracks an in-memory map of (project_id, module_id, scope, key)
// → { is_set, preview }. Components seed this map by registering known
// secret keys and calling refresh().

import { writable, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';

export type SecretScope = 'per_project' | 'shared' | 'global';

export interface SecretEntry {
  project_id: string; // ignored when scope=='global'
  module_id: string;
  scope: SecretScope;
  key: string;
  sensitive: boolean;
  is_set: boolean;
  preview: string | null;
}

function entryKey(e: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key'>): string {
  const proj = e.scope === 'global' ? '_global_' : e.project_id;
  return `${proj}::${e.scope}::${e.module_id}::${e.key}`;
}

interface SecretsState {
  entries: Map<string, SecretEntry>;
  busy: boolean;
  error: string | null;
}

function createSecretsStore() {
  const { subscribe, update } = writable<SecretsState>({
    entries: new Map(),
    busy: false,
    error: null,
  });

  return {
    subscribe,

    /** Register a known secret key for the UI to track + render.
     * Idempotent. Does not call the backend. Use refresh() to fetch
     * is_set / preview state. */
    register(entry: Omit<SecretEntry, 'is_set' | 'preview'>) {
      update((s) => {
        const k = entryKey(entry);
        if (s.entries.has(k)) return s;
        const map = new Map(s.entries);
        map.set(k, { ...entry, is_set: false, preview: null });
        return { ...s, entries: map };
      });
    },

    /** Pull is_set + (optionally) masked preview for one entry. */
    async refresh(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>): Promise<void> {
      if (!tauriAvailable()) return;
      const args = {
        projectId: entry.scope === 'global' ? '_global_' : entry.project_id,
        moduleId: entry.module_id,
        scope: entry.scope,
        key: entry.key,
      };
      try {
        const isSet = await invoke<boolean>('is_secret_set', args);
        let preview: string | null = null;
        if (isSet && !entry.sensitive) {
          try {
            preview = await invoke<string | null>('get_secret_preview', {
              ...args,
              sensitive: false,
            });
          } catch {
            preview = null;
          }
        }
        update((s) => {
          const k = entryKey(entry);
          const existing = s.entries.get(k);
          if (!existing) return s;
          const map = new Map(s.entries);
          map.set(k, { ...existing, is_set: isSet, preview });
          return { ...s, entries: map };
        });
      } catch (e) {
        update((s) => ({
          ...s,
          error: e instanceof Error ? e.message : String(e),
        }));
      }
    },

    /** Refresh all currently registered entries. Used after a project
     * switch so badges in module cards reflect the new scope. */
    async refreshAll(): Promise<void> {
      const all = Array.from(get({ subscribe }).entries.values());
      for (const e of all) {
        await this.refresh(e);
      }
    },

    async setValue(
      entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>,
      value: string,
    ): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, busy: true, error: null }));
      try {
        await invoke<void>('set_secret_v2', {
          projectId: entry.scope === 'global' ? '_global_' : entry.project_id,
          moduleId: entry.module_id,
          scope: entry.scope,
          key: entry.key,
          value,
          validationRegex: null,
          sensitive: entry.sensitive,
        });
        await this.refresh(entry);
        update((s) => ({ ...s, busy: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          busy: false,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    async clearValue(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, busy: true, error: null }));
      try {
        await invoke<void>('clear_secret_v2', {
          projectId: entry.scope === 'global' ? '_global_' : entry.project_id,
          moduleId: entry.module_id,
          scope: entry.scope,
          key: entry.key,
        });
        await this.refresh(entry);
        update((s) => ({ ...s, busy: false }));
      } catch (e) {
        update((s) => ({
          ...s,
          busy: false,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

export const secrets = createSecretsStore();
export { entryKey };
