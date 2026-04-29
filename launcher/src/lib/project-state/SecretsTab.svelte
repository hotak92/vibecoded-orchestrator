<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import { ui } from '$lib/stores/ui';
  import type { ProjectSecretRef } from '$lib/types/project-state';

  let { projectId }: { projectId: string } = $props();

  let refs = $state<ProjectSecretRef[]>([]);
  let loading = $state(true);

  async function load() {
    loading = true;
    try {
      refs = await invoke<ProjectSecretRef[]>('list_project_secret_refs', { projectId });
    } catch (e) { toast.error(e); }
    finally { loading = false; }
  }

  function deepLinkSet(_key: string) {
    // 2026-04-29: previously dispatched a 'vct-open-secrets' window event
    // that no listener consumed (dead code — confirmed by repo-wide grep).
    // Open Settings → Secrets directly via the ui store, which
    // SettingsPanel reads on mount via $ui.settingsInitialSection.
    ui.openSettings('secrets');
  }

  async function del(key: string) {
    if (!confirm(`Delete the secret REFERENCE for "${key}"? The secret VALUE in keychain is untouched.`)) return;
    try {
      await invoke('delete_project_secret_ref', { projectId, secretKey: key });
      await load();
    } catch (e) { toast.error(e); }
  }

  onMount(load);
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Secret references</h3>
    <button class="ps-btn-primary" onclick={() => deepLinkSet('')}>Open secrets panel</button>
  </header>
  <p class="ps-hint">
    These rows describe WHERE the launcher looks for each secret. The actual values live in the OS keychain or
    <code>~/.vct-secrets/</code> — never here. Use the secrets panel to set values.
  </p>

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
          <tr>
            <td><code>{r.secret_key}</code></td>
            <td>
              <span class="ps-tag">{r.resolution}</span>
              {#if r.file_path}<small>{r.file_path}</small>{/if}
              {#if r.env_name}<small>${r.env_name}</small>{/if}
            </td>
            <td>{(r.required_for ?? []).join(', ') || '—'}</td>
            <td>
              {#if r.is_set}
                <span class="ps-status ps-status-set">set</span>
              {:else}
                <span class="ps-status ps-status-unset">missing</span>
              {/if}
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
  .ps-status { font-size: 10px; padding: 1px 6px; border-radius: 8px; }
  .ps-status-set { background: rgba(0,191,166,0.2); color: #0fc; }
  .ps-status-unset { background: rgba(255,99,99,0.15); color: #f99; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; margin-left: 6px; }
  .ps-btn-link:hover { text-decoration: underline; }
  .ps-btn-link-pos { background: none; border: none; color: #0fc; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link-pos:hover { text-decoration: underline; }
</style>
