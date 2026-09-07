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

/** Tri-state presence of a key in ONE store. Mirrors the Rust
 * `secrets_file_store::Presence`.
 *
 * `'unknown'` is load-bearing, not a filler: a locked keychain or an
 * unreadable secrets directory must never render as `'absent'`. "We could
 * not look" and "it is not there" ask the user for opposite actions —
 * unlock the store vs type the value in. */
export type StorePresence = 'present' | 'absent' | 'unknown';

/** Which store the runtime resolver would actually serve a value from.
 * Mirrors the Rust `WinningStore`. */
export type WinningStore = 'keychain' | 'file_store' | 'no_store';

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
  /** v0.3.0: tier 1 — the OS keychain, the launcher's own store. */
  keychain: StorePresence;
  /** v0.3.0: tier 2 — `$VCT_SECRETS_DIR` (default `~/.vct-secrets`),
   * which `vct`, `agent_secrets.get` and `vct_secrets_resolve.sh` all fall
   * back to when the keychain misses. `'absent'` for global scope: the
   * file store has no global namespace. */
  file_store: StorePresence;
  /** v0.3.0: `true` when both stores hold this key with DIFFERENT values —
   * the divergent-copy state CLAUDE.md warns about. `false` when they
   * agree, `null` when the comparison could not be made. Only the boolean
   * ever crosses the IPC boundary; the values do not, and neither does a
   * digest of them. */
  values_diverge: boolean | null;
  /** v0.3.0: path of the file-store copy, when one exists. Contains the
   * KEY name (already visible in this panel) and never a value. */
  file_store_path: string | null;
  /** v0.3.0: tier 2, SECOND leg — would `shared/<key>` satisfy this row?
   *
   * `file_store` above reports on ONE directory (this row's own
   * namespace). The resolvers do not stop there: after
   * `projects/<NAME>/<key>` misses they read `shared/<key>`. A per-project
   * key held only in `shared/` therefore resolves for every consumer while
   * `file_store` is honestly `'absent'` — and a badge reading only those
   * two tiers printed "not set" for it.
   *
   * `'absent'` when the project carries `~/.vct-secrets/projects/<NAME>/
   * .no-shared-fallback`: the file exists, but this project's resolvers
   * skip it, so claiming it satisfies the key would describe another
   * project's resolution. `'absent'` for the shared and global scopes by
   * definition — a shared row's `file_store` IS the `shared/` probe, and
   * the global scope carries no project identity to gate a marker on. */
  shared_file_store: StorePresence;
  /** v0.3.0: path of the SHARED file-store copy, when one would serve this
   * row. KEY name and path only — never a value. Lets Remove name the file
   * that keeps resolving after the keychain entry is gone. */
  shared_file_store_path: string | null;
  /** v0.3.0: which STORE the winning value comes from. */
  winning_store: WinningStore;
  /** v0.3.0: `false` for a row synthesised purely from a file-store file
   * the launcher has never been told about. Remove deletes a keychain
   * entry + a launcher row and CANNOT delete a file — so the panel hides
   * Remove for these rather than claiming a deletion that never happens. */
  has_launcher_row: boolean;
  /** v0.3.0: the VIEWING project has "Disable shared secrets for this
   * project" on, so the keychain's user-shared bucket is not resolved for
   * it. Mirrors the Rust `StoreReport::shared_read_disabled`.
   *
   * Only ever `true` on a `shared`-scope row: the gate drops that bucket
   * and nothing else. A per-project row's `shared/` fall-through leg is
   * gated at the source, inside `shared_file_store`'s marker probe.
   *
   * This is DISPLAY state. It is deliberately absent from `is_set`, which
   * answers the launcher's permission gate — a different question with a
   * different remedy (Reactivate the key, versus turn the project-wide
   * toggle back off). */
  shared_read_disabled: boolean;
  /** v0.3.0: the VIEWING project holds `.no-shared-fallback`, so tier 2's
   * `shared/` directory is not read for it either.
   *
   * Separate from `shared_read_disabled` because one toggle writes both and
   * the second write is best-effort: when the marker write fails the user
   * is warned that the file tier is still ungated, and in that state the
   * shared FILE keeps serving this project. Deriving the badge from the DB
   * flag alone would then claim a key does not reach the user when it
   * does. */
  shared_file_fallback_disabled: boolean;
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

/** What the row's presence badge says.
 *
 * `lifecycleOf` above answers a KEYCHAIN question and drives the BUTTONS
 * (there is nothing to Update or Unset when the keychain is empty). It is
 * the wrong input for the badge: a key held only in the tier-2 file store
 * has no keychain value, so `lifecycleOf` returns `'empty'` and the panel
 * used to print "not set" for a key that every consumer resolves. Users
 * shown that re-entered the value in the GUI and forked it across two
 * stores — exactly the failure CLAUDE.md warns about. */
export type SecretBadge =
  | 'set'
  | 'unset'
  | 'file-store'
  | 'shared-file-store'
  | 'shared-opted-out'
  | 'unknown'
  | 'not-set';

/** The fields the badge is a pure function of. Spelled out so a caller
 * cannot pass a half-built row and get a confident answer. */
export type BadgeInput = Pick<
  SecretEntry,
  | 'is_set'
  | 'keychain'
  | 'file_store'
  | 'shared_file_store'
  | 'shared_read_disabled'
  | 'shared_file_fallback_disabled'
>;

/**
 * The badge answers ONE question: **what would a consumer get for this key
 * right now?** It walks the sanctioned resolvers' own tier order and stops
 * at the first store that would serve, so every state it can return names
 * either a store that answers or a reason nothing does.
 *
 * Two things it is NOT:
 *
 *  * It is not `lifecycleOf`. That one asks a KEYCHAIN question and drives
 *    the BUTTONS (there is nothing to Update or Unset when the keychain is
 *    empty). Deriving the badge from it printed "not set" for keys held in
 *    the tier-2 file store, which every consumer resolves — and users
 *    re-typed those into the GUI, forking the value across two stores
 *    exactly as `CLAUDE.md` warns.
 *  * It is not the permission gate. `is_set` IS that gate and keeps its
 *    exact meaning (keychain value × the per-(secret × requester) active
 *    flag); the badge merely READS it. The dependency must never run the
 *    other way: folding a display fact into `is_set` would silently widen
 *    what `is_secret_set`, the hub and module code all ask.
 *
 * The keychain leg is gated on `is_set` rather than on `is_active`
 * deliberately. `is_active` is this launcher's OWN row; `is_set` is the
 * cross-launcher answer every consumer actually gets. A key paused by a
 * sibling launcher has `is_active === true` and `is_set === false`, and
 * badging it "set" would promise a value no consumer receives.
 */
export function badgeOf(e: BadgeInput): SecretBadge {
  // The whole SHARED tier can be switched off for the viewing project.
  // Two gates, because the toggle writes two things and the second write
  // is best-effort — see the field docs on `SecretEntry`.
  const keychainGated = e.shared_read_disabled === true;
  const fileGated = e.shared_file_fallback_disabled === true;

  // Tier 1 — the keychain, when it holds a value AND readers are ungated.
  if (e.keychain === 'present' && e.is_set && !keychainGated) return 'set';
  // Tier 1 absent, unreadable, PAUSED, or gated off; tier 2 present: the
  // key RESOLVES, because nothing about the file store consults the
  // launcher's active flag — the resolvers fall through to it on
  // `key_not_active` by documented design (`agent_secrets.get`,
  // `vct_secrets_resolve.sh`). This must outrank both the paused branch and
  // the 'unknown' branch below: neither a pause nor an unreadable keychain
  // makes a readable file-store copy any less real.
  if (e.file_store === 'present' && !fileGated) return 'file-store';
  // Tier 2's SECOND leg, in the resolvers' own order: `projects/<NAME>/`
  // is exhausted before `shared/` is read, so this branch sits below the
  // one above and above 'unknown' for the same reason it does. Marker-gated
  // upstream, so a project that opted out never lands here.
  //
  // It is a DISTINCT badge, not a reuse of 'file-store', because the two
  // ask different things of the user: deleting the shared copy breaks
  // every project that leans on it, and the row would give no hint of that
  // if it read as if the file were the project's own.
  if (e.shared_file_store === 'present') return 'shared-file-store';

  // Nothing serves. The remaining states differ only in WHY, and each one
  // asks the user for a different thing — so they must not share a badge.

  // A store we could not read cannot be reported as an absence. A store
  // that is GATED OFF for this project is exempt: whatever it holds is
  // irrelevant here, so its unreadability changes no answer.
  if (
    (e.keychain === 'unknown' && !keychainGated) ||
    (e.file_store === 'unknown' && !fileGated) ||
    e.shared_file_store === 'unknown'
  ) {
    return 'unknown';
  }
  // This project has opted out of a shared store that is not KNOWN to be
  // empty. Neither 'set' (nothing reaches here) nor 'not-set' (the store
  // was never consulted, and one checkbox puts it back in play) — and the
  // fix is neither Reactivate nor re-entering a value, so it gets its own
  // state. `!== 'absent'` rather than `=== 'present'`: an unreadable
  // keychain that this project would not read anyway must not fall through
  // to a confident "not set" further down.
  if (
    (keychainGated && e.keychain !== 'absent') ||
    (fileGated && e.file_store !== 'absent')
  ) {
    return 'shared-opted-out';
  }
  // The keychain holds a value, readers are gated by the active flag, and
  // no other store serves. Only reachable once both tier-2 legs are known
  // to be ABSENT, which is what makes "readers see it as unset" true.
  if (e.keychain === 'present') return 'unset';
  return 'not-set';
}

/** True when both stores hold the key — the fork risk is live, and saving
 * from this panel writes only the keychain copy.
 *
 * Deliberately keychain × the row's OWN file-store namespace, and NOT
 * extended to `shared_file_store`. A keychain value colliding with a
 * `shared/<key>` file is a CROSS-SCOPE collision, and the panel already
 * carries it as such: `list_user_secret_keys_v2` builds its row set from
 * the union of the launcher's rows and the file store's files, so the
 * shared file produces a shared-scope ROW, both rows come back
 * `is_shadowed`, and each renders a badge naming the winner and the loser.
 * A second warning on the same fact would be redundant, and it would be
 * WORSE than redundant here: "⚠ also in file store" invites deleting the
 * other copy, which for a shared file breaks every other project that
 * leans on it — the one thing the shadow badge and the `shared-file-store`
 * tooltip both say out loud. Same-row, same-scope forks are this badge;
 * cross-scope conflicts are the shadow badge. */
export function isForked(e: Pick<SecretEntry, 'keychain' | 'file_store'>): boolean {
  return e.keychain === 'present' && e.file_store === 'present';
}

function entryKey(e: Pick<SecretEntry, 'project_id' | 'module_id' | 'scope' | 'key'>): string {
  const proj = e.scope === 'global' ? '_global_' : e.project_id;
  return `${proj}::${e.scope}::${e.module_id}::${e.key}`;
}

interface SecretsState {
  entries: Map<string, SecretEntry>;
  busy: boolean;
  error: string | null;
  /** Whose point of view the mounted surface is rendering.
   *
   * Every per-key probe is asked "…for WHICH reader?", and for the shared
   * and global scopes the answer is NOT derivable from the entry: those
   * rows are owned by the `_user_shared_` / `_global_` sentinels, which are
   * not projects. Without this, `get_secret_status_v2` answered about the
   * sentinel — i.e. the all-readers `*` row — while the panel was showing
   * one specific project's view, so neither that project's pause of a
   * shared key nor its "Disable shared secrets" toggle could reach the
   * badge.
   *
   * `null` until a surface declares one; the backend then falls back to the
   * owner, which is the pre-v0.3.0 behaviour. */
  requesterProjectId: string | null;
}

function createSecretsStore() {
  const { subscribe, update } = writable<SecretsState>({
    entries: new Map(),
    busy: false,
    error: null,
    requesterProjectId: null,
  });

  /** Read the current reader without subscribing (probes are one-shot). */
  function currentRequester(): string | null {
    return get({ subscribe }).requesterProjectId;
  }

  return {
    subscribe,

    /** Declare whose view is being rendered. Call before probing.
     *
     * `loadFromBackend` sets this implicitly (its argument IS the reader);
     * surfaces that never call it — the per-project Secret-refs tab — must
     * set it explicitly, or their shared-scope rows report the all-readers
     * answer instead of this project's. */
    setRequesterProject(projectId: string | null): void {
      update((s) =>
        s.requesterProjectId === projectId ? s : { ...s, requesterProjectId: projectId },
      );
    },

    /** Register a known secret key for the UI to track + render.
     * Idempotent. Does not call the backend. Use refresh() to fetch the
     * current lifecycle state. */
    register(
      entry: Omit<
        SecretEntry,
        | 'is_set'
        | 'is_active'
        | 'has_saved_value'
        | 'preview'
        | 'is_shadowed'
        | 'winning_scope'
        | 'keychain'
        | 'file_store'
        | 'values_diverge'
        | 'file_store_path'
        | 'shared_file_store'
        | 'shared_file_store_path'
        | 'winning_store'
        | 'has_launcher_row'
        | 'shared_read_disabled'
        | 'shared_file_fallback_disabled'
      >,
    ) {
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
          // Not probed yet. 'unknown' (not 'absent') so a row that is
          // registered but never refreshed cannot flash a confident
          // "not set" for a key that may well resolve.
          keychain: 'unknown',
          file_store: 'unknown',
          shared_file_store: 'unknown',
          values_diverge: null,
          file_store_path: null,
          shared_file_store_path: null,
          winning_store: 'no_store',
          has_launcher_row: true,
          // Not probed yet either. `false` is safe HERE, unlike the
          // presence fields above, because a gate is only ever consulted
          // alongside a `'present'` store and the seeds above are
          // `'unknown'` — so no branch can act on these until a probe has
          // replaced both.
          shared_read_disabled: false,
          shared_file_fallback_disabled: false,
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
        // Owner and reader are the same project for `per_project`, and
        // different for `shared` / `global` — where `projectId` above is a
        // sentinel that names no reader at all.
        requesterProjectId: currentRequester(),
      };
      try {
        const status = await invoke<{
          is_set: boolean;
          is_active: boolean;
          has_saved_value: boolean;
          // v0.3.0: flattened `StoreReport` — where the value actually
          // lives, across BOTH sanctioned stores.
          keychain: StorePresence;
          file_store: StorePresence;
          values_diverge: boolean | null;
          file_store_path: string | null;
          shared_file_store: StorePresence;
          shared_file_store_path: string | null;
          shared_read_disabled: boolean;
          shared_file_fallback_disabled: boolean;
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
            keychain: status.keychain,
            file_store: status.file_store,
            shared_file_store: status.shared_file_store,
            values_diverge: status.values_diverge,
            file_store_path: status.file_store_path,
            shared_file_store_path: status.shared_file_store_path,
            shared_read_disabled: status.shared_read_disabled,
            shared_file_fallback_disabled: status.shared_file_fallback_disabled,
            preview,
          });
          return { ...s, entries: map };
        });
      } catch (e) {
        // The probe did not complete, so we know nothing about either
        // store. Mark both UNKNOWN rather than leaving whatever was in the
        // map: the seeded default is `false`/`absent`-shaped, and rendering
        // that after a failed probe is the "a check that could not run
        // reads as absence" defect.
        update((s) => {
          const k = entryKey(entry);
          const existing = s.entries.get(k);
          const map = new Map(s.entries);
          if (existing) {
            map.set(k, {
              ...existing,
              keychain: 'unknown',
              file_store: 'unknown',
              shared_file_store: 'unknown',
              values_diverge: null,
              // The gates came back in the same failed round-trip, so a
              // retained `true` would be an unverified suppression. `false`
              // suppresses nothing, which — with all three stores now
              // `'unknown'` — can only ever produce `'unknown'`, never a
              // confident negative.
              shared_read_disabled: false,
              shared_file_fallback_disabled: false,
              preview: null,
            });
          }
          return {
            ...s,
            entries: map,
            error: e instanceof Error ? e.message : String(e),
          };
        });
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
     * Module-bucket entries (e.g. licensing's license_key____orchestrator__,
     * canonical per L1.M v0.2.40; was VIBECODED_LICENSE_KEY pre-L1.M)
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
          keychain: StorePresence;
          file_store: StorePresence;
          values_diverge: boolean | null;
          file_store_path: string | null;
          shared_file_store: StorePresence;
          shared_file_store_path: string | null;
          winning_store: WinningStore;
          has_launcher_row: boolean;
          shared_read_disabled: boolean;
          shared_file_fallback_disabled: boolean;
        }
        const rows = await invoke<UserSecretKeyRow[]>('list_user_secret_keys_v2', {
          projectId,
        });
        update((s) => {
          const map = new Map(s.entries);
          // `projectId` IS the reader every row above was computed for, so
          // record it: the per-key `refresh` calls that follow a mutation
          // must ask about the same reader or they overwrite these rows
          // with the sentinel's all-readers answer.
          s = { ...s, requesterProjectId: projectId };
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
              keychain: r.keychain,
              file_store: r.file_store,
              shared_file_store: r.shared_file_store,
              values_diverge: r.values_diverge,
              file_store_path: r.file_store_path,
              shared_file_store_path: r.shared_file_store_path,
              winning_store: r.winning_store,
              has_launcher_row: r.has_launcher_row,
              shared_read_disabled: r.shared_read_disabled,
              shared_file_fallback_disabled: r.shared_file_fallback_disabled,
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
  // NOTE: `tauriAvailable` is a FUNCTION — the pre-v0.3.0 spelling
  // `if (!tauriAvailable)` tested a function reference, which is always
  // truthy, so this guard and the one in `registerSecretFromSource` below
  // never fired. Every other call site in this file already calls it.
  if (!tauriAvailable()) return [];
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
  if (!tauriAvailable()) {
    throw new Error('register_secret_from_source: Tauri unavailable');
  }
  await invoke('register_secret_from_source', { key, source });
}
