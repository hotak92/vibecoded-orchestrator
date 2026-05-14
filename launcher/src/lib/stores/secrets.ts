// Per-secret-entry helper store.
//
// The secrets backend is intentionally CRUD-by-key — there's no list
// endpoint. The UI tracks an in-memory map of (project_id, module_id, scope, key)
// → { is_set, preview }. Components seed this map by registering known
// secret keys and calling refresh().
//
// 0.2.x backlog #3 (2026-05-10): user-bucket entries can also be hydrated
// from the backend in bulk via `loadFromBackend(projectId)` — surfaces
// `is_shadowed` + `winning_scope` so the SecretsPanel can render the
// shared-tab key-collision badge.

import { writable, get } from 'svelte/store';
import { invoke, tauriAvailable } from '$lib/tauri';

export type SecretScope = 'per_project' | 'shared' | 'global';

export interface SecretEntry {
  project_id: string; // ignored when scope=='global'
  module_id: string;
  scope: SecretScope;
  key: string;
  sensitive: boolean;
  /** Visible to readers: true ⇔ keychain has value AND is_active. */
  is_set: boolean;
  /** Active flag in launcher.db. False after Unset, true after Set or
   * Reactivate. Used by the UI to choose between Set vs Reactivate
   * buttons — the value-input row only opens when the keychain is empty
   * or the user explicitly chose "Set as new value". */
  is_active: boolean;
  /** Whether the keychain still holds a value. Combined with `is_active`,
   * the UI distinguishes three lifecycle states:
   *   active=true,  saved=true  → ACTIVE   (badge "set", buttons Update/Unset/Remove)
   *   active=false, saved=true  → INACTIVE (badge "unset", buttons Reactivate/Set as new/Remove)
   *   active=true,  saved=false → EMPTY    (badge "not set", buttons Set/Remove)
   * (The fourth combination — active=false, saved=false — would mean a
   *  ghost row; we treat it as EMPTY.) */
  has_saved_value: boolean;
  preview: string | null;
  /** 0.2.x backlog #3: true when the same KEY name exists at another
   * scope in this project's view of the user-bucket. The resolver's
   * read-time precedence is `per_project > shared > global`; when this
   * row's `scope !== winning_scope`, the row is being shadowed by a
   * higher-precedence row. Both the winner and the loser of a collision
   * carry `is_shadowed: true` so the user sees the conflict from any
   * tab they happen to be looking at. */
  is_shadowed: boolean;
  /** 0.2.x backlog #3: which scope's value the resolver actually serves
   * for `(project_id, key)`. Equals `scope` when this row is the winner.
   * Only meaningful when `is_shadowed === true`. */
  winning_scope: SecretScope;
}

/** Lifecycle states the UI renders. Derived from is_active +
 * has_saved_value at the call site rather than stored, so the store
 * stays the single source of truth. */
export type SecretLifecycle = 'active' | 'inactive' | 'empty';
export function lifecycleOf(e: Pick<SecretEntry, 'is_active' | 'has_saved_value'>): SecretLifecycle {
  if (e.is_active && e.has_saved_value) return 'active';
  if (!e.is_active && e.has_saved_value) return 'inactive';
  return 'empty';
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
     * Idempotent. Does not call the backend. Use refresh() to fetch the
     * current lifecycle state. */
    register(entry: Omit<SecretEntry, 'is_set' | 'is_active' | 'has_saved_value' | 'preview' | 'is_shadowed' | 'winning_scope'>) {
      update((s) => {
        const k = entryKey(entry);
        if (s.entries.has(k)) return s;
        const map = new Map(s.entries);
        map.set(k, {
          ...entry,
          is_set: false,
          is_active: true,
          has_saved_value: false,
          preview: null,
          is_shadowed: false,
          winning_scope: entry.scope,
        });
        return { ...s, entries: map };
      });
    },

    /** Pull lifecycle status (is_set, is_active, has_saved_value) +
     * optional masked preview for one entry. Single round-trip via
     * `get_secret_status_v2`. */
    async refresh(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>): Promise<void> {
      if (!tauriAvailable()) return;
      const args = {
        projectId: resolveProjectId(entry),
        moduleId: entry.module_id,
        scope: entry.scope,
        key: entry.key,
      };
      try {
        const status = await invoke<{
          is_set: boolean;
          is_active: boolean;
          has_saved_value: boolean;
        }>('get_secret_status_v2', args);
        let preview: string | null = null;
        // Only fetch the preview if the entry is fully readable (active
        // + non-sensitive + value present). The backend gates the
        // preview on active=true anyway, but skipping the call here
        // saves a round-trip for inactive / sensitive entries.
        if (status.is_set && !entry.sensitive) {
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
          map.set(k, {
            ...existing,
            is_set: status.is_set,
            is_active: status.is_active,
            has_saved_value: status.has_saved_value,
            preview,
          });
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

    /**
     * 0.2.x backlog #3: enumerate every user-bucket secret KEY the
     * launcher has observed for `projectId`'s view (its own per_project
     * bucket + shared + global), populating the store with the response.
     *
     * Backed by the new `list_user_secret_keys_v2` Tauri command. Each
     * row carries `is_shadowed` + `winning_scope` so the SecretsPanel
     * can render the shared-tab key-collision badge without computing
     * collisions client-side.
     *
     * Idempotent: re-running merges new rows with the existing store
     * (preserves any not-yet-saved register() calls from the add-form
     * flow). Existing entries are updated with the latest is_set /
     * is_active / has_saved_value / shadow status from the backend.
     *
     * Module-bucket entries (e.g. licensing's VIBECODED_LICENSE_KEY)
     * are NOT enumerated — this command targets only the user emit
     * bucket the SecretsPanel writes to. The SecretsPanel still seeds
     * the licensing global key separately via `register()`.
     */
    async loadFromBackend(projectId: string): Promise<void> {
      if (!tauriAvailable()) return;
      try {
        interface UserSecretKeyRow {
          scope: SecretScope;
          project_id: string;
          module_id: string;
          key: string;
          is_set: boolean;
          is_active: boolean;
          has_saved_value: boolean;
          is_shadowed: boolean;
          winning_scope: SecretScope;
        }
        const rows = await invoke<UserSecretKeyRow[]>('list_user_secret_keys_v2', {
          projectId,
        });
        update((s) => {
          const map = new Map(s.entries);
          for (const r of rows) {
            const partial = {
              project_id: r.project_id,
              module_id: r.module_id,
              scope: r.scope,
              key: r.key,
            };
            const k = entryKey(partial);
            const existing = map.get(k);
            map.set(k, {
              project_id: r.project_id,
              module_id: r.module_id,
              scope: r.scope,
              key: r.key,
              // The backend doesn't track sensitive-ness (it's a UI hint set
              // at add time). Default true on first observation; preserve
              // the existing flag if the entry was already registered.
              sensitive: existing?.sensitive ?? true,
              is_set: r.is_set,
              is_active: r.is_active,
              has_saved_value: r.has_saved_value,
              preview: existing?.preview ?? null,
              is_shadowed: r.is_shadowed,
              winning_scope: r.winning_scope,
            });
          }
          return { ...s, entries: map };
        });
      } catch (e) {
        update((s) => ({
          ...s,
          error: e instanceof Error ? e.message : String(e),
        }));
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
          projectId: resolveProjectId(entry),
          moduleId: entry.module_id,
          scope: entry.scope,
          key: entry.key,
          value,
          validationRegex: null,
          sensitive: entry.sensitive,
        });
        // PR-3 Commit 5 (2026-05-06): bridge SecretsPanel ↔ SecretsTab.
        // When a value is set in the SecretsPanel for the per-project
        // scope, also register a ref row in `project_secret_refs` so the
        // per-project SecretsTab actually populates. Pre-PR-3 the two
        // stores were unconnected — `set_secret_v2` wrote the keychain
        // but never registered the ref, leaving the per-project tab
        // showing zero refs even after the user had set the value
        // (see secrets-and-access-matrix-audit-2026-05-06.md §6).
        if (entry.scope === 'per_project') {
          try {
            await invoke<void>('set_project_secret_ref', {
              projectId: entry.project_id,
              req: {
                secret_key: entry.key,
                // Per-project keychain entry written by `set_secret_v2`
                // above lives at `vct.<project_id>.<module_id>.<key>`.
                resolution: 'keychain-per-project',
                file_path: null,
                env_name: null,
                source_module: entry.module_id,
                required_for: [],
                description: '',
                is_set: true,
              },
            });
          } catch (refErr) {
            // Non-fatal: the keychain write succeeded; the ref-row
            // failure means the per-project tab won't show this entry.
            // Surface as a warning rather than rolling back the
            // keychain write (which the user explicitly asked for).
            console.warn('set_project_secret_ref failed (per-project tab may not reflect this entry)', refErr);
          }
        }
        await this.refresh(entry);
        // NOTE: callers that need the 0.2.x backlog #3 shadow-status
        // badges updated after a mutation should call
        // `loadFromBackend(viewProjectId)` separately. We don't auto-call
        // here because shared/global mutations come in with a sentinel
        // `project_id` and we'd have to plumb the user's current
        // view-project through the entry to do it correctly.
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

    /** Unset (Lifecycle B): mark the entry INACTIVE while keeping the
     * VALUE in the OS keychain. The launcher's read API then refuses to
     * return the value until the user calls `reactivateValue`. Use case:
     * rotating tokens — pause an old one while validating a new one,
     * with no re-typing required to resume. */
    async unsetValue(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, busy: true, error: null }));
      try {
        // `clear_secret_v2` no longer touches the keychain — it only
        // flips the active flag in launcher.db. The keychain value is
        // preserved so `reactivateValue` is a one-click resume.
        await invoke<void>('clear_secret_v2', {
          projectId: resolveProjectId(entry),
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

    /** Reactivate a previously-Unset entry: flip active back to true.
     * No re-entry required — the value is still in the keychain from
     * before Unset. Pairs with `unsetValue`. */
    async reactivateValue(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, busy: true, error: null }));
      try {
        await invoke<void>('reactivate_secret_v2', {
          projectId: resolveProjectId(entry),
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

    /** Remove: clear the keychain value AND drop the entry from the
     * registry map. The row stops appearing in the panel. Use Unset
     * instead if you want the entry to remain visible (e.g. token
     * rotation). */
    async removeEntry(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key' | 'sensitive'>): Promise<void> {
      if (!tauriAvailable()) throw new Error('Tauri not available');
      update((s) => ({ ...s, busy: true, error: null }));
      try {
        await invoke<void>('remove_secret_v2', {
          projectId: resolveProjectId(entry),
          moduleId: entry.module_id,
          scope: entry.scope,
          key: entry.key,
        });
        update((s) => {
          const k = entryKey(entry);
          if (!s.entries.has(k)) return { ...s, busy: false };
          const map = new Map(s.entries);
          map.delete(k);
          return { ...s, entries: map, busy: false };
        });
      } catch (e) {
        update((s) => ({
          ...s,
          busy: false,
          error: e instanceof Error ? e.message : String(e),
        }));
        throw e;
      }
    },

    /** Drop an entry from the in-memory registry without touching the
     * keychain. Used when re-seeding or pruning stale UI state. */
    forgetEntry(entry: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key'>): void {
      update((s) => {
        const k = entryKey(entry);
        if (!s.entries.has(k)) return s;
        const map = new Map(s.entries);
        map.delete(k);
        return { ...s, entries: map };
      });
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },
  };
}

// Resolve the project_id we send to the backend. Globals use a fixed
// sentinel; shared scope uses a different sentinel (so all "shared"
// secrets land in one user-wide bucket regardless of which project is
// currently selected — see backend `enforce_scope_invariants`); per-
// project uses the entry's project_id (must reference a registered
// project, enforced by backend).
function resolveProjectId(entry: Pick<SecretEntry, 'project_id' | 'scope'>): string {
  if (entry.scope === 'global') return '_global_';
  if (entry.scope === 'shared') return '_user_shared_';
  return entry.project_id;
}

export const secrets = createSecretsStore();
export { entryKey };

// ─── Bug H (v0.2.8): secrets-import client ─────────────────────────────
//
// One-shot import surface for the migration from on-disk secret stores
// (project .env files, ~/.vct-secrets/shared/) into the launcher
// keychain. The value-handling rule is INVIOLABLE: the FE only ever
// holds the KEY and the SOURCE descriptor — never the value. The
// backend reads the value itself when `registerSecretFromSource` is
// called.

export interface ImportableSecretKey {
  /** The secret key (e.g. "GITHUB_TOKEN"). Never contains the value. */
  key: string;
  /** Opaque source descriptor returned by the backend; pass it back
   *  unchanged to `registerSecretFromSource`. Format:
   *  "env_file:<abs_path>" or "vct_secrets_shared:<abs_path>". */
  source: string;
  /** Whether the launcher's shared keychain already has this key.
   *  FE renders an "already imported" badge for true. */
  already_in_keychain: boolean;
}

/** Enumerate importable secret keys from the canonical on-disk sources.
 *  Returns one row per (key, source) pair. The list is deterministic
 *  across calls (sorted by source-priority then filename). */
export async function listImportableSecretKeys(): Promise<ImportableSecretKey[]> {
  if (!tauriAvailable) return [];
  return await invoke<ImportableSecretKey[]>('list_importable_secret_keys', {});
}

/** Register a secret by KEY only. The backend reads the value from the
 *  source itself and writes it to the shared keychain under
 *  `module_id="user"`. Returns void on success; throws on error.
 *  The error message NEVER includes the raw value (backend contract). */
export async function registerSecretFromSource(
  key: string,
  source: string
): Promise<void> {
  if (!tauriAvailable) {
    throw new Error('register_secret_from_source: Tauri unavailable');
  }
  await invoke('register_secret_from_source', { key, source });
}
