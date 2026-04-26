<script lang="ts">
  import { onMount } from 'svelte';
  import { invoke } from '$lib/tauri';
  import { toast } from '$lib/stores/toast';
  import type { ProjectAgent } from '$lib/types/project-state';
  import Dropdown from '$lib/components/Dropdown.svelte';

  const SOURCE_OPTIONS = [
    { value: 'user', label: 'user' },
    { value: 'bundled', label: 'bundled' },
    { value: 'paid-module', label: 'paid-module' },
    { value: 'project', label: 'project' },
  ];

  let { projectId }: { projectId: string } = $props();

  let agents = $state<ProjectAgent[]>([]);
  let loading = $state(true);

  // Register form
  let showAdd = $state(false);
  let newName = $state('');
  let newSource = $state<'bundled' | 'user' | 'paid-module' | 'project'>('user');
  let newModel = $state('');
  let newSourceModule = $state('');

  async function load() {
    loading = true;
    try {
      agents = await invoke<ProjectAgent[]>('list_project_agents', { projectId });
    } catch (e) {
      toast.error(e);
    } finally {
      loading = false;
    }
  }

  async function toggle(name: string, enabled: boolean) {
    try {
      await invoke('set_project_agent_enabled', { projectId, agentName: name, enabled });
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  async function unregister(name: string) {
    if (!confirm(`Unregister agent "${name}"? The .md file is NOT removed; only the registry row is dropped.`)) return;
    try {
      await invoke('unregister_project_agent', { projectId, agentName: name });
      toast.success('Unregistered');
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  async function register() {
    if (!newName.trim()) {
      toast.error('Name required');
      return;
    }
    try {
      await invoke('register_project_agent', {
        projectId,
        req: {
          agent_name: newName.trim(),
          source: newSource,
          source_module: newSourceModule.trim() || null,
          model: newModel.trim() || null,
          file_path: null,
          config: {},
        },
      });
      toast.success('Registered');
      newName = ''; newModel = ''; newSourceModule = '';
      showAdd = false;
      await load();
    } catch (e) {
      toast.error(e);
    }
  }

  onMount(load);
  $effect(() => { if (projectId) void load(); });
</script>

<section class="ps-tab">
  <header class="ps-tab-header">
    <h3>Agents</h3>
    <button class="ps-btn-primary" onclick={() => (showAdd = !showAdd)}>
      {showAdd ? 'Cancel' : '+ Register'}
    </button>
  </header>

  {#if showAdd}
    <div class="ps-form">
      <div class="ps-form-grid">
        <label><span>Name</span><input bind:value={newName} placeholder="my-agent" /></label>
        <label><span>Source</span>
          <Dropdown options={SOURCE_OPTIONS} bind:value={newSource} />
        </label>
        <label><span>Source module</span><input bind:value={newSourceModule} placeholder="optional" /></label>
        <label><span>Model</span><input bind:value={newModel} placeholder="claude/sonnet" /></label>
      </div>
      <button class="ps-btn-primary" onclick={register}>Register</button>
      <p class="ps-hint">Toggling agent enabled flag NEVER touches the user filesystem; only the DB registry row.</p>
    </div>
  {/if}

  {#if loading}
    <p class="ps-loading">Loading…</p>
  {:else if agents.length === 0}
    <p class="ps-empty">No agents registered.</p>
  {:else}
    <table class="ps-table">
      <thead><tr><th>Name</th><th>Source</th><th>Model</th><th>Enabled</th><th></th></tr></thead>
      <tbody>
        {#each agents as a (a.agent_name)}
          <tr>
            <td><code>{a.agent_name}</code></td>
            <td><span class="ps-tag ps-tag-{a.source}">{a.source}</span></td>
            <td>{a.model ?? '—'}</td>
            <td>
              <label class="ps-tooltip" title="Toggles registry only — .md file untouched">
                <input
                  type="checkbox"
                  checked={a.enabled}
                  onchange={(e) => toggle(a.agent_name, (e.target as HTMLInputElement).checked)}
                />
              </label>
            </td>
            <td><button class="ps-btn-link" onclick={() => unregister(a.agent_name)}>Unregister</button></td>
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
  .ps-form { background: rgba(255,255,255,0.04); padding: 12px; border-radius: 6px; margin-bottom: 16px; }
  .ps-form-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 10px; }
  .ps-form-grid label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: #888; }
  .ps-form-grid input, .ps-form-grid select {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12); color: inherit;
    padding: 5px 8px; border-radius: 4px; font-size: 12px;
  }
  .ps-loading, .ps-empty { color: #888; padding: 24px; text-align: center; }
  .ps-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .ps-table th { text-align: left; padding: 6px 8px; color: #888; font-weight: 500; border-bottom: 1px solid rgba(255,255,255,0.08); }
  .ps-table td { padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .ps-table code { font-family: ui-monospace, monospace; font-size: 11px; }
  .ps-tag {
    font-size: 10px; padding: 1px 6px; border-radius: 8px;
    background: rgba(255,255,255,0.08); color: #ccc;
  }
  .ps-tag-bundled { background: rgba(0,191,166,0.15); color: #0fc; }
  .ps-tag-user { background: rgba(123,95,255,0.15); color: #c4b3ff; }
  .ps-tag-paid-module { background: rgba(255,200,70,0.15); color: #fc6; }
  .ps-tag-project { background: rgba(58,163,255,0.15); color: #6cf; }
  .ps-btn-primary {
    background: rgb(0,191,166); border: none; color: #000;
    padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 12px; font-weight: 600;
  }
  .ps-btn-link {
    background: none; border: none; color: #f99; cursor: pointer; font-size: 11px;
    padding: 0;
  }
  .ps-btn-link:hover { text-decoration: underline; }
  .ps-tooltip { cursor: help; }
  .ps-hint { font-size: 11px; color: #888; margin: 6px 0 0; }
</style>
