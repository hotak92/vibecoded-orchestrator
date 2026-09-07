<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { projects as projectsStore } from '$lib/stores/projects';
  import { secrets as secretsStore } from '$lib/stores/secrets';
  import {
    badgeForRef,
    entryOf,
    BADGE_LABEL,
    BADGE_TITLE,
  } from './secret-ref-status';
  import type { ProjectSecretRef } from '$lib/types/project-state';

  let { projectId }: { projectId: string } = $props();

  let refs = $state<ProjectSecretRef[]>([]);
  let loading = $state(true);

  // GAP-2 (2026-07-14): per-project bulk "disable shared secrets" toggle.
  // Mirror of the IdentityTab shared-KG read gate. When ON, this project
  // omits the user-shared secrets bucket from its resolved /env pairs.
  let sharedSecretsReadDisabled = $state(false);
  let togglingSharedSecrets = $state(false);

  // v0.2.46 V47-C followup (landed with V47-G-final): "Migrate from .env"
  // surface. Lets the user re-run V47-C's keychain migration from the
  // launcher GUI, without re-running install.py. The actual migration
  // round-trip happens via the vct-hub /api/v1/secrets/migrate endpoint
  // (registered by V47-C). The Tauri command `migrate_env_secrets_from_dotenv`
  // is the launcher-side wrapper; status icons reflect the result.
  let migrating = $state(false);
  // Last migration result, shown inline as a small banner.
  type MigrationResult = {
    ok: boolean;
    migrated: string[];
    failed: string[];
    error?: string;
    // GAP-1 (2026-07-14): where the keys landed — "shared" | "per_project".
    // Optional so an older Rust build (no `scope`) still parses during dev.
    scope?: string;
  };
  let lastMigration = $state<MigrationResult | null>(null);

  // ─── "Set?" column: a LIVE two-store probe, not a stored column ───────
  //
  // `ProjectSecretRef.is_set` is a snapshot the WRITER left behind — the
  // hub's .env migration passes `Some(true)` at registration time
  // (`vct-hub/src/secrets_api.rs`) and nothing ever revises it. Rendering
  // it meant the column said "set" for a key the user had since deleted,
  // and "missing" for one that resolves fine from the tier-2 file store.
  //
  // The derivation lives in `./secret-ref-status` because this repo's
  // vitest runs headless with no svelte component runner, so logic inside
  // a `.svelte` file cannot be tested. See that module's header for the
  // full rationale and the invariants it must not fork.

  /** Probe both stores for each listed ref via the shared `secrets` store
   * (one `get_secret_status_v2` round-trip per key — the same command the
   * sibling SecretsPanel uses, so the two surfaces cannot disagree). */
  async function refreshStatuses(list: ProjectSecretRef[]) {
    // Declare whose view this is BEFORE probing. A `keychain-shared` ref is
    // owned by the `_user_shared_` sentinel, which names no reader, so
    // without this the probe answers about the all-readers `*` row: neither
    // this project's pause of a shared key nor its "Disable shared secrets"
    // checkbox below could reach the column beside it.
    secretsStore.setRequesterProject(projectId);
    for (const r of list) {
      const e = entryOf(r, projectId);
      secretsStore.register(e);
      await secretsStore.refresh(e);
    }
  }

  async function load() {
    loading = true;
    try {
      refs = await invoke<ProjectSecretRef[]>('list_project_secret_refs', { projectId });
      // Load the GAP-2 toggle state alongside the refs.
      sharedSecretsReadDisabled = await projectsStore.getSharedSecretsReadDisabled(projectId);
      // Probe both stores per ref so the "Set?" column reports what is
      // actually there rather than what the writer once recorded.
      await refreshStatuses(refs);
    } catch (e) { toast.error(e); }
    finally { loading = false; }
  }

  async function toggleSharedSecrets() {
    if (togglingSharedSecrets) return;
    const next = !sharedSecretsReadDisabled;
    togglingSharedSecrets = true;
    // Optimistic flip; revert on error (IdentityTab toggle pattern).
    sharedSecretsReadDisabled = next;
    try {
      sharedSecretsReadDisabled = await projectsStore.setSharedSecretsReadDisabled(projectId, next);
      // This toggle is not cosmetic for the column beside it: the backend
      // writes/removes `~/.vct-secrets/projects/<NAME>/.no-shared-fallback`,
      // and that marker is exactly what decides whether a key satisfied
      // only by `~/.vct-secrets/shared/` still resolves here. Without this
      // re-probe every such row keeps its pre-toggle badge until the tab is
      // reloaded — the user turns the gate on and is still told the shared
      // value serves them (or off, and is still told it does not).
      await refreshStatuses(refs);
    } catch (e) {
      sharedSecretsReadDisabled = !next;
      toast.error(e);
    } finally {
      togglingSharedSecrets = false;
    }
  }

  function deepLinkSet(_key: string) {
    // v0.2.23 F2 wave 2b (2026-05-21): the SettingsPanel popover (which
    // had a Secrets sub-tab) was merged into /preferences. The Secrets
    // sub-tab was a duplicate of the dedicated /preferences/secrets
    // route, so it was dropped and we navigate directly here.
    void goto('/preferences/secrets');
  }

  async function del(key: string) {
    if (!confirm(`Delete the secret REFERENCE for "${key}"? The secret VALUE in keychain is untouched.`)) return;
    try {
      await invoke('delete_project_secret_ref', { projectId, secretKey: key });
      await load();
    } catch (e) { toast.error(e); }
  }

  /**
   * V47-C followup: trigger the keychain migration for secret-shaped keys
   * present in the project's .env file. The launcher invokes the
   * `migrate_env_secrets_from_dotenv` Tauri command which forwards the
   * keys to vct-hub's `/api/v1/secrets/migrate` endpoint.
   *
   * If the command isn't yet registered in this build (V47-G-final ships
   * the Svelte surface ahead of the Rust command on some platforms), we
   * fall back to a deep-link to the CLI guidance — the underlying hub
   * endpoint already exists from V47-C, so power users can invoke it via
   * `python install.py --update --apply-deferred` even without the GUI
   * wrapper.
   */
  async function migrateFromDotEnv() {
    if (migrating) return;
    if (!confirm(
      'Audit this project\'s .env for secret-shaped keys and migrate them ' +
      'to the OS keychain? The .env file will be rewritten to remove the ' +
      'migrated values; the keychain becomes the source of truth.'
    )) return;

    migrating = true;
    lastMigration = null;
    try {
      const res = await invoke<MigrationResult>(
        'migrate_env_secrets_from_dotenv',
        { projectId },
      );
      lastMigration = res;
      if (res.ok && res.migrated.length > 0) {
        toast.success(`Migrated ${res.migrated.length} secret(s) to the keychain.`);
        await load();
      } else if (res.ok && res.migrated.length === 0) {
        toast.info('No secret-shaped keys found in .env (or all were already migrated).');
      } else {
        toast.error(res.error || 'Migration failed; see status panel for details.');
      }
    } catch (e) {
      // Command not registered yet (Tauri build doesn't have this yet) —
      // surface the CLI fallback path so the user isn't stuck.
      const msg = e instanceof Error ? e.message : String(e);
      lastMigration = {
        ok: false,
        migrated: [],
        failed: [],
        error: `Tauri command unavailable in this build: ${msg}. ` +
               `Run \`python install.py --update --apply-deferred\` from a ` +
               `terminal at the project root to perform the migration.`,
      };
      toast.error('Migration command not available — see panel for CLI fallback.');
    } finally {
      migrating = false;
    }
  }

  onMount(load);
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Secret references</h3>
    <div class="ps-tab-actions">
      <button
        class="ps-btn-secondary"
        onclick={migrateFromDotEnv}
        disabled={migrating}
        title="Audit .env for secret-shaped keys and migrate them to the OS keychain (V47-C)"
      >
        {migrating ? 'Migrating…' : 'Migrate from .env'}
      </button>
      <button class="ps-btn-primary" onclick={() => deepLinkSet('')}>Open secrets panel</button>
    </div>
  </header>
  <p class="ps-hint">
    These rows describe WHERE the launcher looks for each secret. The actual values live in the OS keychain or
    <code>~/.vct-secrets/</code> — never here. Use the secrets panel to set values.
  </p>

  {#if lastMigration}
    <div class="ps-migration-result" class:ps-migration-ok={lastMigration.ok}>
      {#if lastMigration.ok && lastMigration.migrated.length > 0}
        <strong>Migrated {lastMigration.migrated.length} secret(s)</strong>
        {#if lastMigration.scope === 'per_project'}
          to <strong>this project's</strong> keychain scope:
        {:else if lastMigration.scope === 'shared'}
          to the <strong>shared</strong> keychain scope (orchestrator root):
        {:else}
          :
        {/if}
        {lastMigration.migrated.join(', ')}
        {#if lastMigration.failed.length > 0}
          <br/><strong>Failed:</strong> {lastMigration.failed.join(', ')}
        {/if}
      {:else if lastMigration.ok}
        <em>No secret-shaped keys found in .env (or all already migrated).</em>
      {:else}
        <strong>Migration failed:</strong> {lastMigration.error || 'unknown error'}
      {/if}
    </div>
  {/if}

  <div class="ps-shared-secrets-block">
    <label class="ps-shared-secrets-toggle">
      <input
        type="checkbox"
        checked={sharedSecretsReadDisabled}
        disabled={togglingSharedSecrets}
        onchange={toggleSharedSecrets}
      />
      <span>Disable shared secrets for this project</span>
    </label>
    <p class="ps-hint">
      When on, this project does NOT resolve shared secrets. The two stores
      are gated with deliberately different reach, because only one of them
      can tell the buckets apart: the <strong>keychain</strong> drops the
      "Shared (this user)" bucket only — per-project and module-declared
      secrets are untouched; the <strong>file store</strong>
      (<code>~/.vct-secrets/</code>) skips its whole <code>shared/</code>
      directory for this project, <code>github_pat</code> included, since
      it is one flat namespace with no such distinction.
      <code>git push</code> is unaffected either way
      (<code>git-credential-vct</code> reads the file directly). Mirrors
      the shared-KG read gate on the Identity tab.
    </p>
  </div>

  {#if loading}
    <p class="ps-empty">Loading…</p>
  {:else if refs.length === 0}
    <p class="ps-empty">No secret references registered for this project.</p>
  {:else}
    <table class="ps-table">
      <thead>
        <tr><th>KEY</th><th>Resolution</th><th>Required for</th><th>Set?</th><th></th></tr>
      </thead>
      <tbody>
        {#each refs as r (r.secret_key)}
          {@const badge = badgeForRef(r, projectId, $secretsStore.entries)}
          <tr>
            <td><code>{r.secret_key}</code></td>
            <td>
              <span class="ps-tag">{r.resolution}</span>
              {#if r.file_path}<small>{r.file_path}</small>{/if}
              {#if r.env_name}<small>${r.env_name}</small>{/if}
            </td>
            <td>{(r.required_for ?? []).join(', ') || '—'}</td>
            <td>
              <span
                class="ps-status ps-status-{badge}"
                title={BADGE_TITLE[badge]}
              >{BADGE_LABEL[badge]}</span>
            </td>
            <td>
              <button class="ps-btn-link-pos" onclick={() => deepLinkSet(r.secret_key)}>Set value</button>
              <button class="ps-btn-link" onclick={() => del(r.secret_key)}>Delete ref</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<style>
  .ps-tab { padding: 16px; }
  .ps-tab-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .ps-tab-header h3 { font-size: 16px; margin: 0; }
  .ps-hint { font-size: 11px; color: #888; margin: 0 0 12px; }
  .ps-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 6px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); vertical-align: middle; }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-table small { display: block; color: #666; font-size: 10px; }
  .ps-tag { font-size: 10px; padding: 1px 6px; border-radius: 8px; background: rgba(123,95,255,0.15); color: #c4b3ff; }
  .ps-status { font-size: 10px; padding: 1px 6px; border-radius: 8px; cursor: help; }
  .ps-status-set { background: rgba(0,191,166,0.2); color: #0fc; }
  /* Resolves, just not from the keychain — teal like `set`, because the
     user's question is "will my tools find it?" and the answer is yes. */
  .ps-status-file-store { background: rgba(0,191,166,0.14); color: #7fe6d8; }
  /* The shared fall-through leg. Same teal family as the file-store badge
     (both mean "this resolves"), one step dimmer so the two are
     distinguishable at a glance — the difference matters: deleting a
     SHARED copy breaks other projects too. */
  .ps-status-shared-file-store { background: rgba(0,191,166,0.10); color: #6fd3c6; }
  /* Paused: present in the keychain but withheld by the active flag. */
  .ps-status-unset { background: rgba(255,196,99,0.15); color: #fc9; }
  /* "We could not look" gets its own neutral treatment — never the red
     that means "absent", which would invite a needless re-entry. */
  .ps-status-unknown { background: rgba(255,255,255,0.08); color: #aaa; }
  /* The shared tier is switched off for this project. Neutral, never the
     red of "absent": the value is there and the checkbox above restores
     it — nothing is missing and nothing needs re-entering. */
  .ps-status-shared-opted-out { background: rgba(255,255,255,0.05); color: #9aa; border: 1px solid rgba(255,255,255,0.18); }
  /* The ref names a file / env var neither store covers, and no copy was
     found in the stores we DID check. Neutral for the same reason: this
     is "not measured", not "not there". */
  .ps-status-declared-elsewhere { background: rgba(255,255,255,0.05); color: #9aa; border: 1px dashed rgba(255,255,255,0.28); }
  .ps-status-not-set { background: rgba(255,99,99,0.15); color: #f99; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-secondary { background: rgba(123,95,255,0.18); border: 1px solid rgba(123,95,255,0.4); color: #c4b3ff; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 500; margin-right: 6px; }
  .ps-btn-secondary:hover:not(:disabled) { background: rgba(123,95,255,0.28); }
  .ps-btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; margin-left: 6px; }
  .ps-btn-link:hover { text-decoration: underline; }
  .ps-btn-link-pos { background: none; border: none; color: #0fc; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link-pos:hover { text-decoration: underline; }
  .ps-tab-actions { display: flex; gap: 6px; align-items: center; }
  .ps-migration-result { background: rgba(255,99,99,0.08); border: 1px solid rgba(255,99,99,0.25); border-radius: 4px; padding: 8px 12px; margin: 8px 0 12px; font-size: 11px; color: #f99; }
  .ps-migration-result.ps-migration-ok { background: rgba(0,191,166,0.08); border-color: rgba(0,191,166,0.25); color: #0fc; }
  .ps-migration-result em { color: #aaa; font-style: italic; }
  .ps-shared-secrets-block { margin: 4px 0 14px; padding: 10px 12px; background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 4px; }
  .ps-shared-secrets-toggle { display: flex; align-items: center; gap: 8px; font-size: 12px; cursor: pointer; }
  .ps-shared-secrets-toggle input { cursor: pointer; }
  .ps-shared-secrets-block .ps-hint { margin: 6px 0 0; }
</style>
