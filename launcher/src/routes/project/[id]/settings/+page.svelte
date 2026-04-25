<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import Toast from '$lib/components/Toast.svelte';
  import type { ProjectView } from '$lib/types/launcher';

  let projectId = $derived($page.params.id);
  let project = $state<ProjectView | null>(null);
  let newName = $state('');
  let saving = $state(false);

  // Env vars: stored in module install rows? We don't have a single-project
  // env API yet — expose via the project_state secret_refs path for guidance
  // and link to the secrets panel for actual values.
  let envEntries = $state<Array<{ key: string; value: string }>>([]);
  let newEnvKey = $state('');
  let newEnvValue = $state('');

  async function load() {
    try {
      project = await invoke<ProjectView>('get_project_v2', { id: projectId });
      newName = project?.name ?? '';
    } catch (e) {
      toast.error(e);
    }
  }

  async function rename() {
    if (!newName.trim() || !project) return;
    saving = true;
    try {
      const updated = await invoke<ProjectView>('rename_project_v2', {
        id: project.id,
        newName: newName.trim(),
      });
      project = updated;
      toast.success('Renamed');
    } catch (e) {
      toast.error(e);
    } finally {
      saving = false;
    }
  }

  function addEnv() {
    if (!newEnvKey.trim()) return;
    envEntries = [...envEntries, { key: newEnvKey.trim().toUpperCase(), value: newEnvValue }];
    newEnvKey = '';
    newEnvValue = '';
  }
  function removeEnv(idx: number) {
    envEntries = envEntries.filter((_, i) => i !== idx);
  }

  onMount(load);
</script>

<div class="ps-page">
  <header class="ps-header">
    <button class="ps-back" onclick={() => goto(`/project/${projectId}`)}>← Back to project</button>
    <h1>Settings — {project?.name ?? '…'}</h1>
  </header>

  {#if !project}
    <p class="ps-empty">Loading…</p>
  {:else}
    <main class="ps-main">
      <section class="ps-section">
        <h2>Metadata</h2>
        <div class="ps-grid">
          <label><span>Name</span><input bind:value={newName} /></label>
          <div class="ps-meta">
            <p><span>Folder:</span> <code>{project.folder_path}</code></p>
            <p><span>Host:</span> <code>{project.host}</code></p>
            <p><span>Modules:</span> {project.module_count}</p>
            <p><span>Created:</span> {new Date(project.created_at).toLocaleString()}</p>
          </div>
        </div>
        <button class="ps-btn-primary" onclick={rename} disabled={saving || newName === project.name}>
          {saving ? 'Saving…' : 'Save name'}
        </button>
      </section>

      <section class="ps-section">
        <h2>Project env vars (notes only)</h2>
        <p class="ps-hint">
          Values are stored in <code>~/.vct-secrets/</code> or the OS keychain — not here. This list is a
          reminder of what your agents expect. Use the Secrets panel to set actual values.
        </p>
        <table class="ps-table">
          <thead><tr><th>KEY</th><th>Notes / placeholder</th><th></th></tr></thead>
          <tbody>
            {#each envEntries as e, i (e.key)}
              <tr>
                <td><code>{e.key}</code></td>
                <td><input bind:value={envEntries[i].value} /></td>
                <td><button class="ps-btn-link" onclick={() => removeEnv(i)}>Remove</button></td>
              </tr>
            {/each}
            <tr>
              <td><input bind:value={newEnvKey} placeholder="MY_VAR" /></td>
              <td><input bind:value={newEnvValue} placeholder="optional notes" /></td>
              <td><button class="ps-btn-primary" onclick={addEnv}>Add</button></td>
            </tr>
          </tbody>
        </table>
      </section>
    </main>
  {/if}
</div>

<Toast />

<style>
  .ps-page { min-height: 100vh; background: var(--color-bg, #0e0e16); color: var(--color-light, #e8e8ee); }
  .ps-header { display: flex; align-items: center; gap: 12px; padding: 10px 24px; border-bottom: 1px solid rgba(255,255,255,0.06); }
  .ps-header h1 { font-size: 16px; margin: 0; }
  .ps-back { background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1); color: inherit; padding: 4px 10px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .ps-empty { padding: 40px; text-align: center; color: #888; }
  .ps-main { max-width: 720px; margin: 0 auto; padding: 16px; }
  .ps-section { background: rgba(255,255,255,0.03); padding: 14px; border-radius: 6px; margin-bottom: 14px; }
  .ps-section h2 { font-size: 13px; margin: 0 0 8px; color: #c4b3ff; }
  .ps-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; align-items: start; }
  .ps-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-grid input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 8px; border-radius: 4px; font-size: 13px;
  }
  .ps-meta { font-size: 12px; line-height: 1.7; color: #ccc; }
  .ps-meta p { margin: 0; }
  .ps-meta span { color: #888; display: inline-block; min-width: 90px; }
  .ps-meta code { background: rgba(0,0,0,0.3); padding: 1px 6px; border-radius: 3px; font-family: ui-monospace, monospace; }
  .ps-hint { font-size: 11px; color: #888; margin: 0 0 10px; line-height: 1.5; }
  .ps-hint code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 3px; }
  .ps-btn-primary { background: rgb(0,191,166); border: none; color: #000; padding: 5px 14px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600; }
  .ps-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 4px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; }
  .ps-table input {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 3px 6px; border-radius: 3px; font-size: 12px; width: 100%;
  }
  .ps-btn-link { background: none; border: none; color: #f99; cursor: pointer; font-size: 11px; padding: 0; }
  .ps-btn-link:hover { text-decoration: underline; }
</style>
